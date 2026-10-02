"""SQL-сценарии: разбор файла на шаги, ожидания и прогон в одной или нескольких сессиях.

Формат сценария: обычный SQL, который выполняется в psql. Чекеру нужны только комментарии:

    -- 3. Отрицательный балл: ошибка CHECK         # заголовок шага (номер и что показываем)
    -- expect: error check                         # чего ждём от базы
    INSERT INTO submissions (...) VALUES (...);

Сценарий гонки: те же шаги, но вместо номера имя окна:

    -- [setup] готовим данные          -- [A] окно A        -- [B] окно B
    -- [check] правило цело? (один boolean)                 -- [cleanup] убираем за собой
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psycopg

from .core import blue, bold, dim, green, red, yellow

# ---------- Разбор файла ----------

ERROR_KINDS = {
    "unique": "23505", "check": "23514", "fk": "23503", "foreign": "23503", "notnull": "23502",
    "not_null": "23502", "exclusion": "23P01", "raise": "P0001", "permission": "42501",
    "deadlock": "40P01", "serialization": "40001", "lock": "55P03", "timeout": "57014",
}
ERROR_NAMES = {
    "23505": "UNIQUE", "23514": "CHECK", "23503": "внешний ключ", "23502": "NOT NULL", "23P01": "EXCLUDE",
    "P0001": "RAISE EXCEPTION", "42501": "нет прав", "40P01": "взаимоблокировка", "40001": "сериализация",
    "55P03": "блокировка", "57014": "таймаут",
}
PLAN_ALIASES = {
    "seq": ["Seq Scan"],
    "index": ["Index Scan", "Index Only Scan", "Bitmap Index Scan"],
    "only": ["Index Only Scan"],
    "bitmap": ["Bitmap Heap Scan", "Bitmap Index Scan"],
}
SPECIAL = {"setup", "check", "cleanup"}

HEADER_NUM = re.compile(r"^--\s*(\d+)\s*[.)]\s*(.*)$")
HEADER_TAG = re.compile(r"^--\s*\[\s*([A-Za-z]+)\s*\]\s*(.*)$")
EXPECT = re.compile(r"^--\s*expect\s*:\s*(.+?)\s*$", re.IGNORECASE)
TX_CONTROL = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|ROLLBACK|END|ABORT)\b", re.IGNORECASE)


@dataclass
class Expect:
    kind: str          # ok | error | rows | value | same | plan | blocked
    arg: str = ""
    raw: str = ""

    def describe(self) -> str:
        return self.raw


@dataclass
class Step:
    key: str                     # "1", "2" ... или "A", "B", "setup", "check", "cleanup"
    title: str
    line: int
    statements: list[tuple[int, str]] = field(default_factory=list)
    expects: list[Expect] = field(default_factory=list)
    meta: list[str] = field(default_factory=list)   # пропущенные команды psql (\d и т. п.)

    @property
    def session(self) -> str | None:
        if self.key.isdigit() or self.key in ("0",):
            return None
        return self.key if self.key.lower() not in SPECIAL else self.key.lower()


@dataclass
class Script:
    path: Path
    steps: list[Step]

    @property
    def multi(self) -> bool:
        return any(s.session and s.session not in SPECIAL for s in self.steps)

    @property
    def has_tx_control(self) -> bool:
        return any(TX_CONTROL.match(sql) for s in self.steps for _, sql in s.statements)

    def numbered(self) -> list[Step]:
        return [s for s in self.steps if s.key.isdigit() and s.key != "0"]

    def step(self, key: str) -> Step | None:
        return next((s for s in self.steps if s.key.lower() == key.lower()), None)


def parse_expect(text: str) -> Expect:
    raw = text.strip()
    words = raw.split(None, 1)
    kind = words[0].lower()
    arg = words[1].strip() if len(words) > 1 else ""
    if kind in ("true", "false"):
        return Expect("value", kind, raw)
    if kind not in ("ok", "error", "rows", "value", "same", "plan", "blocked"):
        return Expect("unknown", raw, raw)
    return Expect(kind, arg, raw)


def lex(sql: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]], list[tuple[int, str]]]:
    """Делит текст на операторы. Возвращает (операторы, комментарии верхнего уровня, команды psql).

    Понимает строки '...' и E'...', идентификаторы "...", $$-кавычки, комментарии -- и /* */.
    Комментарий считается «верхнего уровня», только если он стоит между операторами:
    поэтому «-- 1.» внутри тела функции заголовком шага не станет.
    """
    statements: list[tuple[int, str]] = []
    comments: list[tuple[int, str]] = []
    meta: list[tuple[int, str]] = []
    buf: list[str] = []
    start_line = 0
    line = 1
    i, n = 0, len(sql)

    def blank() -> bool:
        return not "".join(buf).strip()

    while i < n:
        ch = sql[i]
        if ch == "\n":
            line += 1
            buf.append(ch)
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            if blank():
                comments.append((line, sql[i:j].strip()))
            i = j
            continue
        if sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    if sql[j] == "\n":
                        line += 1
                    j += 1
            i = j
            buf.append(" ")
            continue
        if ch == "\\" and blank():
            j = sql.find("\n", i)
            j = n if j == -1 else j
            meta.append((line, sql[i:j].strip()))
            i = j
            continue
        if blank() and not ch.isspace():
            start_line = line
        if ch == "'" or (ch in "eE" and sql.startswith("'", i + 1) and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_"))):
            escape = ch in "eE"
            j = i + (2 if escape else 1)
            while j < n:
                if escape and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if sql.startswith("''", j):
                        j += 2
                        continue
                    break
                j += 1
            chunk = sql[i:j + 1]
            line += chunk.count("\n")
            buf.append(chunk)
            i = j + 1
            continue
        if ch == '"':
            j = sql.find('"', i + 1)
            j = n - 1 if j == -1 else j
            chunk = sql[i:j + 1]
            line += chunk.count("\n")
            buf.append(chunk)
            i = j + 1
            continue
        if ch == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == "_")):
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j == -1 else j + len(tag)
                chunk = sql[i:j]
                line += chunk.count("\n")
                buf.append(chunk)
                i = j
                continue
        if ch == ";":
            text = "".join(buf).strip()
            if text:
                statements.append((start_line, text))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    text = "".join(buf).strip()
    if text:
        statements.append((start_line, text))
    return statements, comments, meta


def parse(path: Path) -> Script:
    text = path.read_text(encoding="utf-8-sig")
    statements, comments, meta = lex(text)
    steps: list[Step] = [Step("0", "подготовка", 0)]
    last_header_line = -10
    for line, comment in comments:
        if line == last_header_line + 1 and re.match(r"^--\s{3,}\S", comment) and not EXPECT.match(comment):
            steps[-1].title += " " + comment[2:].strip()
            last_header_line = line
            continue
        if HEADER_NUM.match(comment) or HEADER_TAG.match(comment):
            last_header_line = line
        if m := HEADER_NUM.match(comment):
            steps.append(Step(m.group(1), m.group(2).strip(), line))
        elif m := HEADER_TAG.match(comment):
            steps.append(Step(m.group(1), m.group(2).strip(), line))
        elif m := EXPECT.match(comment):
            owner = max((s for s in steps if s.line <= line), key=lambda s: s.line)
            owner.expects.append(parse_expect(m.group(1)))
    steps.sort(key=lambda s: s.line)

    def owner_of(line: int) -> Step:
        return max((s for s in steps if s.line <= line), key=lambda s: s.line)

    for line, sql in statements:
        owner_of(line).statements.append((line, sql))
    for line, cmd in meta:
        owner_of(line).meta.append(cmd)
    if not steps[0].statements and not steps[0].meta:
        steps.pop(0)
    return Script(path, steps)


# ---------- Результаты ----------

@dataclass
class DbError:
    sqlstate: str
    message: str
    detail: str = ""
    hint: str = ""
    constraint: str = ""

    def short(self) -> str:
        name = ERROR_NAMES.get(self.sqlstate, "")
        label = f"{self.sqlstate} {name}".strip()
        return f"ОШИБКА [{label}] {self.message}"


@dataclass
class StmtResult:
    sql: str
    status: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = -1
    error: DbError | None = None
    ms: float = 0.0

    @property
    def is_dml(self) -> bool:
        return bool(re.match(r"^\s*(WITH\b.*?\)\s*)?(INSERT|UPDATE|DELETE|MERGE)\b", self.sql, re.IGNORECASE | re.DOTALL)) \
            and self.status.split(" ")[0] in ("INSERT", "UPDATE", "DELETE", "MERGE")


@dataclass
class StepResult:
    step: Step
    stmts: list[StmtResult] = field(default_factory=list)
    blocked: bool = False
    notices: list[str] = field(default_factory=list)
    verdicts: list[tuple[Expect, bool, str]] = field(default_factory=list)

    @property
    def error(self) -> DbError | None:
        return next((s.error for s in self.stmts if s.error), None)

    @property
    def last_rows(self) -> StmtResult | None:
        return next((s for s in reversed(self.stmts) if s.columns), None)

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.verdicts)

    @property
    def ms(self) -> float:
        return sum(s.ms for s in self.stmts)


@dataclass
class ScriptResult:
    script: Script
    steps: list[StepResult]
    mode: str
    fatal: str = ""

    @property
    def passed(self) -> bool:
        return not self.fatal and all(s.passed for s in self.steps)

    def get(self, key: str) -> StepResult | None:
        return next((s for s in self.steps if s.step.key.lower() == key.lower()), None)

    @property
    def failed(self) -> list[StepResult]:
        return [s for s in self.steps if not s.passed]


# ---------- Ожидания ----------

def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _norm(text: str) -> str:
    t = text.strip().strip("'\"").lower()
    return {"t": "true", "f": "false"}.get(t, t)


def plan_text(res: StepResult) -> str:
    rows = res.last_rows
    if not rows:
        return ""
    return "\n".join(" ".join(_cell(v) for v in r) for r in rows.rows)


def execution_time(res: StepResult) -> float | None:
    m = re.search(r"Execution Time:\s*([\d.]+)\s*ms", plan_text(res))
    return float(m.group(1)) if m else None


def evaluate(res: StepResult, done: dict[str, StepResult]) -> None:
    expects = res.step.expects or [Expect("ok", "", "ok (по умолчанию)")]
    err = res.error
    # Если ошибка в шаге ожидаема (expect: error ...), остальные ожидания проверяют последний результат
    error_expected = any(e.kind == "error" for e in expects)
    for exp in expects:
        ok, why = True, ""
        if exp.kind == "ok":
            ok = err is None
            why = err.short() if err else ""
        elif exp.kind == "error":
            if err is None:
                ok, why = False, "ожидали ошибку, а база выполнила запрос"
            elif exp.arg:
                want = exp.arg.strip()
                if want.startswith(("'", '"')):
                    ok = want.strip("'\"").lower() in (err.message + " " + err.detail).lower()
                    why = "" if ok else f"текст ошибки другой: {err.message}"
                else:
                    code = ERROR_KINDS.get(want.lower(), want.upper())
                    ok = err.sqlstate == code
                    why = "" if ok else f"ожидали {code} {ERROR_NAMES.get(code, '')}, получили {err.short()}"
        elif exp.kind == "blocked":
            ok, why = res.blocked, "" if res.blocked else "шаг выполнился сразу, а должен был ждать блокировку"
        elif err is not None and not error_expected:
            ok, why = False, err.short()
        elif exp.kind == "rows":
            last = res.last_rows
            if last:
                count = last.rowcount if last.rowcount >= 0 else len(last.rows)
            else:
                dml = [s for s in res.stmts if s.is_dml]
                count = dml[-1].rowcount if dml else 0
            arg = exp.arg.replace(" ", "")
            if not arg:
                ok = count >= 1
            elif arg.startswith(">="):
                ok = count >= int(arg[2:])
            elif arg.startswith(">"):
                ok = count > int(arg[1:])
            else:
                ok = count == int(arg)
            why = "" if ok else f"строк: {count}, ожидали {exp.arg or '≥ 1'}"
        elif exp.kind == "value":
            last = res.last_rows
            got = _cell(last.rows[0][0]) if last and last.rows else "(нет строк)"
            ok = _norm(got) == _norm(exp.arg)
            why = "" if ok else f"получили {got}, ожидали {exp.arg}"
        elif exp.kind == "same":
            other = done.get(exp.arg.strip())
            mine = res.last_rows
            if other is None or other.last_rows is None:
                ok, why = False, f"нет результата шага {exp.arg} для сравнения"
            else:
                ok = mine is not None and mine.rows == other.last_rows.rows
                why = "" if ok else f"результат отличается от шага {exp.arg}"
        elif exp.kind == "plan":
            text = plan_text(res)
            needles = PLAN_ALIASES.get(exp.arg.lower(), [exp.arg])
            ok = any(nd.lower() in text.lower() for nd in needles)
            why = "" if ok else f"в плане нет «{' / '.join(needles)}»"
        else:
            ok, why = False, f"непонятное ожидание «{exp.raw}», формат описан в README чекера"
        res.verdicts.append((exp, ok, why))


# ---------- Печать ----------

SESSION_COLORS = {"A": "36", "B": "35", "C": "33"}


def _fit(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def format_table(stmt: StmtResult, max_rows: int = 8, width: int = 28) -> list[str]:
    cols = stmt.columns
    if len(cols) == 1 and cols[0].upper() == "QUERY PLAN":
        return [_fit(_cell(r[0]), 110) for r in stmt.rows[:40]]
    rows = [[_fit(_cell(v), width) for v in r] for r in stmt.rows[:max_rows]]
    widths = [max([len(c)] + [len(r[k]) for r in rows]) for k, c in enumerate(cols)]
    out = [" │ ".join(c.ljust(w) for c, w in zip(cols, widths)),
           "─┼─".join("─" * w for w in widths)]
    out += [" │ ".join(v.ljust(w) for v, w in zip(r, widths)) for r in rows]
    total = stmt.rowcount if stmt.rowcount >= 0 else len(stmt.rows)
    from .core import plural
    if total > max_rows:
        out.append(f"... всего {plural(total, 'строка', 'строки', 'строк')}, показаны первые {max_rows}")
    else:
        out.append(f"({plural(total, 'строка', 'строки', 'строк')})")
    return out


class Printer:
    def __init__(self, enabled: bool = True, show_sql: bool = False, indent: str = "      "):
        self.enabled = enabled
        self.show_sql = show_sql
        self.indent = indent
        self.lock = threading.Lock()

    def line(self, text: str = "") -> None:
        if self.enabled:
            with self.lock:
                print(self.indent + text)

    def header(self, step: Step) -> None:
        if not self.enabled:
            return
        sess = step.session
        if sess and sess not in SPECIAL:
            from .core import c
            label = c(f"[{sess}]", SESSION_COLORS.get(sess, "1"))
        elif sess:
            label = dim(f"[{sess}]")
        else:
            label = bold(f"{step.key}.")
        self.line(f"{label} {step.title}")
        if self.show_sql:
            for _, sql in step.statements:
                for ln in sql.splitlines()[:12]:
                    self.line(dim("    " + ln))
        for cmd in step.meta:
            self.line(dim(f"    пропущено (команда psql): {cmd}"))

    def result(self, res: StepResult, *, brief: bool = False) -> None:
        if not self.enabled:
            return
        pad = "    "
        for note in res.notices:
            self.line(blue(f"{pad}ℹ {note}"))
        for st in res.stmts:
            if st.error:
                self.line(red(f"{pad}{st.error.short()}"))
                if st.error.detail:
                    self.line(dim(f"{pad}  {' / '.join(st.error.detail.splitlines())}"))
                break
            if st.columns and not brief:
                if st is res.last_rows or len([s for s in res.stmts if s.columns]) <= 3:
                    for ln in format_table(st):
                        self.line(dim(pad) + ln)
            elif not st.columns and st.status and not brief and st.status not in ("BEGIN", "SET", "RESET"):
                self.line(dim(f"{pad}{st.status}"))
        t = execution_time(res)
        if t is not None:
            self.line(yellow(f"{pad}⏱ Execution Time: {t:.3f} ms"))
        for exp, ok, why in res.verdicts:
            if not res.step.expects and ok:
                continue
            mark = green("✓") if ok else red("✗")
            self.line(f"{pad}{mark} ожидали: {exp.describe()}" + (f": {why}" if why else ""))


# ---------- Прогон ----------

ConnFactory = Callable[[], psycopg.Connection]


def _execute(conn: psycopg.Connection, sql: str, max_rows: int = 500) -> StmtResult:
    res = StmtResult(sql)
    t0 = time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)  # без параметров psycopg отправляет текст как есть, % не трогает
            res.status = cur.statusmessage or ""
            res.rowcount = cur.rowcount
            if cur.description:
                res.columns = [d.name for d in cur.description]
                res.rows = cur.fetchmany(max_rows)
    except psycopg.Error as exc:
        diag = exc.diag
        res.error = DbError(exc.sqlstate or "?????", (diag.message_primary or str(exc)).strip(),
                            diag.message_detail or "", diag.message_hint or "", diag.constraint_name or "")
    res.ms = (time.perf_counter() - t0) * 1000
    return res


def _run_step(conn: psycopg.Connection, step: Step, notices: list[str] | None = None) -> StepResult:
    res = StepResult(step)
    if notices is not None:
        notices.clear()
    for _, sql in step.statements:
        res.stmts.append(_execute(conn, sql))
    if notices is not None:
        res.notices = list(notices)
    return res


def _connect(factory: ConnFactory) -> tuple[psycopg.Connection, list[str]]:
    conn = factory()
    conn.autocommit = True
    notices: list[str] = []
    conn.add_notice_handler(lambda d: notices.append(f"{d.severity}: {d.message_primary}"))
    return conn, notices


def run_single(factory: ConnFactory, script: Script, printer: Printer, *, keep: bool = False) -> ScriptResult:
    """Одна сессия. Если в файле нет BEGIN/COMMIT, всё выполняется в транзакции и откатывается в конце
    (каждый шаг в своей точке сохранения), так что прогон ничего не меняет в базе.
    Если управление транзакциями есть, как в psql: каждый оператор сразу, изменения остаются."""
    wrap = not script.has_tx_control and not keep
    mode = ("всё в одной транзакции с откатом в конце, база не меняется" if wrap
            else "как в psql: каждый оператор сразу, BEGIN/COMMIT/ROLLBACK из файла выполняются как написаны")
    try:
        conn, notices = _connect(factory)
    except psycopg.Error as exc:
        return ScriptResult(script, [], mode, f"не удалось подключиться: {exc}")
    done: dict[str, StepResult] = {}
    results: list[StepResult] = []
    try:
        if wrap:
            conn.execute("BEGIN")
        for step in script.steps:
            printer.header(step) if step.key != "0" else None
            if wrap:
                conn.execute("SAVEPOINT dbcheck_step")
            res = _run_step(conn, step, notices)
            if wrap:
                if res.error:
                    conn.execute("ROLLBACK TO SAVEPOINT dbcheck_step")
                conn.execute("RELEASE SAVEPOINT dbcheck_step")
            evaluate(res, done)
            done[step.key] = res
            results.append(res)
            if step.key != "0":
                printer.result(res)
            elif not res.passed:
                printer.line(red(f"подготовка: {res.error.short() if res.error else 'ошибка'}"))
    finally:
        try:
            if wrap:
                conn.execute("ROLLBACK")
        except psycopg.Error:
            pass
        conn.close()
    return ScriptResult(script, results, mode)


def run_multi(factory: ConnFactory, script: Script, printer: Printer, *,
              block_wait: float = 2.0, step_timeout: float = 20.0) -> ScriptResult:
    """Несколько окон (A, B и так далее). У каждого окна своё подключение, шаги идут строго по порядку файла.
    Если шаг не завершился за block_wait секунд, значит он ждёт блокировку: чекер помечает его
    «⏳ ждёт» и идёт дальше, а результат напечатает, когда блокировка снимется."""
    sessions = sorted({s.session for s in script.steps if s.session and s.session not in SPECIAL})
    conns: dict[str, tuple[psycopg.Connection, list[str]]] = {}
    pending: dict[str, tuple[Future, StepResult]] = {}
    results: list[StepResult] = []
    done: dict[str, StepResult] = {}
    mode = f"{len(sessions)} сессии: " + ", ".join(sessions)
    pool = ThreadPoolExecutor(max_workers=max(1, len(sessions)))

    def finish(sess: str, wait: float) -> bool:
        fut, res = pending[sess]
        try:
            filled = fut.result(timeout=wait)
        except FutureTimeout:
            return False
        res.stmts, res.notices = filled.stmts, filled.notices
        evaluate(res, done)
        done[res.step.key] = res
        del pending[sess]
        printer.line(dim(f"↳ [{sess}] дождался блокировки и продолжил: «{res.step.title}»"))
        printer.result(res)
        return True

    def special(step: Step) -> StepResult:
        conn, notices = _connect(factory)
        try:
            res = _run_step(conn, step, notices)
        finally:
            conn.close()
        evaluate(res, done)
        done[step.key] = res
        return res

    try:
        for sess in sessions:
            conns[sess] = _connect(factory)
        for step in script.steps:
            for sess in list(pending):
                finish(sess, 0)
            sess = step.session
            if step.key == "0" or sess in SPECIAL:
                if sess in ("check", "cleanup"):
                    for other in list(pending):
                        if not finish(other, step_timeout):
                            raise RuntimeError(f"окно {other} зависло дольше {step_timeout:.0f} с")
                printer.header(step) if step.key != "0" else None
                res = special(step)
                results.append(res)
                printer.result(res) if step.key != "0" else None
                continue
            if sess in pending and not finish(sess, step_timeout):
                raise RuntimeError(f"окно {sess} зависло дольше {step_timeout:.0f} с")
            printer.header(step)
            conn, notices = conns[sess]
            res = StepResult(step)
            results.append(res)
            fut = pool.submit(_run_step, conn, step, notices)
            try:
                filled = fut.result(timeout=block_wait)
            except FutureTimeout:
                res.blocked = True
                pending[sess] = (fut, res)
                printer.line(yellow(f"    ⏳ [{sess}] ждёт: строка заблокирована другим окном"))
                continue
            res.stmts, res.notices = filled.stmts, filled.notices
            evaluate(res, done)
            done[step.key] = res
            printer.result(res)
        for sess in list(pending):
            if not finish(sess, step_timeout):
                raise RuntimeError(f"окно {sess} зависло дольше {step_timeout:.0f} с")
        fatal = ""
    except (RuntimeError, psycopg.Error) as exc:
        fatal = str(exc)
        printer.line(red(f"✗ {fatal}"))
    finally:
        for conn, _ in conns.values():
            try:
                conn.cancel_safe() if hasattr(conn, "cancel_safe") else conn.cancel()
            except Exception:
                pass
        pool.shutdown(wait=False, cancel_futures=True)
        for conn, _ in conns.values():
            try:
                conn.close()
            except Exception:
                pass
        cleanup = script.step("cleanup")
        if fatal and cleanup is not None and "cleanup" not in done:
            try:
                special(cleanup)
            except psycopg.Error:
                pass
    # Шаги, которые так и не получили вердикт (из-за фатальной ошибки), считаем проваленными
    for res in results:
        if not res.verdicts:
            res.verdicts.append((Expect("ok", "", "шаг завершился"), False, "не выполнен"))
    return ScriptResult(script, results, mode, fatal)


def run_script(factory: ConnFactory, path: Path, printer: Printer | None = None, **kw) -> ScriptResult:
    script = parse(path)
    printer = printer or Printer(enabled=False)
    if script.multi:
        return run_multi(factory, script, printer)
    return run_single(factory, script, printer, **kw)


def check_value(result: ScriptResult) -> bool | None:
    """Ответ шага [check]: True: правило соблюдено, False: нарушено, None: шага нет или он не вернул boolean."""
    res = result.get("check")
    if res is None or res.error or not res.last_rows or not res.last_rows.rows:
        return None
    value = res.last_rows.rows[0][0]
    if isinstance(value, bool):
        return value
    return {"true": True, "t": True, "false": False, "f": False}.get(_cell(value).lower())


def clean(sql: str) -> str:
    """Текст оператора без строковых литералов, для поиска ключевых слов (JOIN, GROUP BY и других)."""
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def step_sql(step: Step) -> str:
    return "\n".join(clean(sql) for _, sql in step.statements)
