"""Лаба 4. Транзакции и доступ: откат, гонка в двух окнах, роли, секреты."""

from __future__ import annotations

import re

import psycopg

from . import catalog
from .core import Ctx, Fail, Item, Lab, Star, need_script, report_failed_steps, score_two
from .lab3 import MODIFIES
from .sqlscript import TX_CONTROL, check_value

FILE = "demo/lab4.sql"
RACE = "demo/race.sql"
RACE_FIXED = "demo/race_fixed.sql"
READER, WRITER = "app_reader", "app_writer"


def main_table(ctx: Ctx) -> str:
    name = ctx.cfg("project", "main_table")
    if name:
        return name
    tables = [t for t in catalog.tables(ctx) if not t.service]
    return max(tables, key=lambda t: t.rows).qname if tables else ""


def _race(ctx: Ctx, item: Item, rel: str):
    res = need_script(ctx, item, rel)
    if not res.script.multi:
        raise Fail(f"{rel}: нет шагов «-- [A]» и «-- [B]», сценарий должен идти в двух окнах")
    value = check_value(res)
    if value is None:
        raise Fail(f"{rel}: нет шага «-- [check]», возвращающего один boolean (true, если правило соблюдено)")
    return res, value


# ---------- База ----------

def check_transaction(ctx: Ctx, item: Item) -> None:
    res = need_script(ctx, item, FILE)
    tx_steps = [s for s in res.steps
                if any(TX_CONTROL.match(sql) for _, sql in s.step.statements) and s.error
                and any(e.kind == "error" and ok for e, ok, _ in s.verdicts)]
    if not tx_steps:
        raise Fail(f"в {FILE} нет транзакции BEGIN ... COMMIT, которая падает в середине (с «-- expect: error ...»)")
    item.ok(f"шаг {tx_steps[0].step.key}: ошибка в середине транзакции: {tx_steps[0].error.short()[:80]}")
    same = [s for s in res.steps if any(e.kind == "same" and ok for e, ok, _ in s.verdicts)]
    if not same:
        raise Fail(f"в {FILE} нет шага «-- expect: same N», доказывающего, что данные не изменились")
    item.ok(f"шаг {same[0].step.key}: данные такие же, как до транзакции")
    bad = report_failed_steps(item, FILE, res)
    if bad:
        raise Fail(f"{FILE}: не сбылись ожидания в шагах {', '.join(s.step.key for s in bad)}")
    item.eye("Студент объяснил, почему откатилась и первая (удачная) операция?")


def check_race(ctx: Ctx, item: Item) -> None:
    res, value = _race(ctx, item, RACE)
    windows = sorted({s.step.session for s in res.steps if s.step.session and s.step.session.isupper()})
    item.ok(f"{RACE}: окна {', '.join(windows)}, {len(res.steps)} шагов")
    if value:
        raise Fail(f"{RACE}: [check] вернул true: правило не нарушилось, гонка не воспроизведена")
    item.ok("[check] вернул false: две сессии нарушили правило темы, гонка воспроизведена")


def check_race_fixed(ctx: Ctx, item: Item) -> None:
    res, value = _race(ctx, item, RACE_FIXED)
    if not value:
        raise Fail(f"{RACE_FIXED}: [check] вернул false: правило по-прежнему нарушается")
    item.ok("[check] вернул true: при том же сценарии правило соблюдено")
    blocked = [s for s in res.steps if s.blocked]
    errors = [s for s in res.steps if s.error]
    if blocked:
        item.ok(f"окно {blocked[0].step.session} ждало блокировку (шаг «{blocked[0].step.title[:50]}»)")
    for s in errors[:2]:
        item.ok(f"окно {s.step.session}: {s.error.short()[:90]}")
    if not blocked and not errors:
        item.warn("в исправленном сценарии ни одно окно не ждало и не получило ошибку, проверьте, что гонка та же")
    bad = report_failed_steps(item, RACE_FIXED, res)
    if bad:
        raise Fail(f"{RACE_FIXED}: не сбылись ожидания в шагах {', '.join(s.step.key for s in bad)}")


def _live(ctx: Ctx, role: str, sql: str) -> str | None:
    """Выполняет запрос под ролью с паролем из .env внутри транзакции с откатом. Возвращает SQLSTATE или None."""
    password = ctx.env.get(f"{role.upper()}_PASSWORD")
    conn = ctx.connect(user=role, password=password)
    try:
        conn.execute("BEGIN")
        conn.execute(sql)
        return None
    except psycopg.Error as exc:
        return exc.sqlstate or "?"
    finally:
        try:
            conn.execute("ROLLBACK")
        except psycopg.Error:
            pass
        conn.close()


def check_roles(ctx: Ctx, item: Item) -> None:
    found = catalog.roles(ctx, READER, WRITER)
    missing = [r for r in (READER, WRITER) if r not in found]
    if missing:
        raise Fail(f"нет ролей: {', '.join(missing)}")
    for r in found.values():
        if not r["login"]:
            raise Fail(f"{r['name']} без LOGIN, приложение не сможет под ней войти")
        if r["super"] or r["bypassrls"]:
            raise Fail(f"{r['name']}: суперпользователь или BYPASSRLS, это не минимальные права")
    # Каталог: что роли могут на самом деле (с учётом членства в других ролях)
    reader = catalog.table_privileges(ctx, READER)
    writer = catalog.table_privileges(ctx, WRITER)
    r_write = [p["name"] for p in reader if p["ins"] or p["upd"] or p["del"] or p["trn"]]
    r_tables = [p["name"] for p in reader if p["sel"] and p["kind"] in ("r", "p")]
    r_views = [p["name"] for p in reader if p["sel"] and p["kind"] in ("v", "m")]
    w_write = [p["name"] for p in writer if (p["ins"] or p["upd"] or p["del"] or p["trn"]) and p["kind"] in ("r", "p")]
    if r_write:
        raise Fail(f"{READER} может менять: {', '.join(r_write[:5])}")
    if r_tables:
        raise Fail(f"{READER} читает таблицы напрямую: {', '.join(r_tables[:5])}, нужно только через представления")
    if not r_views:
        raise Fail(f"{READER} не может читать ни одно представление")
    if w_write:
        raise Fail(f"{WRITER} меняет таблицы напрямую: {', '.join(w_write[:5])}, нужно только через функции")
    item.ok(f"{READER}: SELECT на {', '.join(r_views[:5])}; таблиц и записи нет")
    r_funcs = [f for f in catalog.function_privileges(ctx, READER)
               if f["can_execute"] and f["security_definer"] and MODIFIES.search(f["source"] or "")]
    if r_funcs:
        raise Fail(f"{READER} может вызвать {', '.join(f['name'] for f in r_funcs[:3])} (SECURITY DEFINER, меняет данные), "
                   "через неё можно писать. Нужен REVOKE EXECUTE ... FROM PUBLIC")
    w_funcs = [f["name"] for f in catalog.function_privileges(ctx, WRITER) if f["can_execute"] and MODIFIES.search(f["source"] or "")]
    if not w_funcs:
        raise Fail(f"{WRITER} не может вызвать ни одной функции, меняющей данные")
    item.ok(f"{WRITER}: таблицы напрямую не меняет; бизнес-операции: {', '.join(w_funcs[:4])}")
    # Живая проверка: войти под ролями и попробовать лишнее
    table = main_table(ctx)
    if not ctx.env.get("APP_READER_PASSWORD") or not ctx.env.get("APP_WRITER_PASSWORD"):
        item.info("в .env нет APP_READER_PASSWORD и APP_WRITER_PASSWORD, проверка входа под ролями пропущена")
        return
    try:
        view = r_views[0]
        tries = [
            (READER, f"SELECT * FROM {view} LIMIT 1", None, f"читает {view}"),
            (READER, f"DELETE FROM {table} WHERE false", "42501", f"DELETE FROM {table}: permission denied"),
            (READER, f"SELECT * FROM {table} LIMIT 1", "42501", f"SELECT FROM {table}: permission denied"),
            (WRITER, f"DELETE FROM {table} WHERE false", "42501", f"DELETE FROM {table}: permission denied"),
        ]
        call = ctx.cfg("lab4", "writer_call")
        if call:
            tries.append((WRITER, call, None, f"бизнес-операция: {call[:60]}"))
        for role, sql, want, label in tries:
            got = _live(ctx, role, sql)
            if got != want:
                raise Fail(f"вход под {role}: «{sql[:60]}»: {got or 'выполнилось'}, ожидали {want or 'успех'}")
            item.ok(f"вход под {role}: {label}")
    except psycopg.OperationalError as exc:
        raise Fail(f"не удалось войти под ролью с паролем из .env: {str(exc).strip().splitlines()[-1][:120]}")


def check_secrets(ctx: Ctx, item: Item) -> None:
    example = ctx.file(".env.example")
    if not example.exists():
        raise Fail("нет .env.example")
    keys = [k for k in re.findall(r"^\s*([A-Z_]*PASSWORD[A-Z_]*)\s*=", example.read_text(encoding="utf-8"), re.M)]
    if not any(k.startswith("APP_") or "READER" in k or "WRITER" in k for k in keys):
        raise Fail(".env.example: нет паролей ролей приложения (APP_READER_PASSWORD, APP_WRITER_PASSWORD)")
    item.ok(f".env.example: {', '.join(keys)}")
    tracked = ctx.tracked_files()
    if tracked is None:
        gitignore = ctx.read(".gitignore")
        if not re.search(r"^\.env\s*$", gitignore, re.M):
            raise Fail(".env не в .gitignore")
        item.info("не git-репозиторий, проверен только .gitignore")
    else:
        if ".env" in tracked:
            raise Fail(".env лежит в git, пароли опубликованы. Выполните git rm --cached .env и смените пароли")
        item.ok(".env не в git")
        if ctx.git("log", "--all", "--format=%h", "--", ".env"):
            item.warn(".env когда-то был в истории git, пароли оттуда считаются раскрытыми")
    literal = []
    for path in ctx.db_scripts():
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.search(r"PASSWORD\s+'[^']+'", line, re.I) and not line.lstrip().startswith("--"):
                literal.append(f"db/{path.name}:{n}")
    compose = ctx.compose_file()
    if compose:
        for n, line in enumerate(compose.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"\s*-?\s*\w*PASSWORD\w*\s*[:=]\s*(.+)$", line)
            if m and not re.match(r"""^["']?\$\{""", m.group(1).strip()) and not line.lstrip().startswith("#"):
                literal.append(f"{compose.name}:{n}")
    if literal:
        raise Fail(f"пароль записан прямо в файле: {', '.join(literal[:4])}")
    item.ok("паролей в db/ и compose нет, только переменные окружения")


def check_readme_race(ctx: Ctx, item: Item) -> None:
    readme = ctx.readme()
    if "race" not in readme.lower() and "гонк" not in readme.lower():
        raise Fail("в README нет раздела про гонку")
    fix = re.findall(r"FOR UPDATE|SERIALIZABLE|UNIQUE|advisory|блокир", readme, re.I)
    if not fix:
        raise Fail("в README не сказано, чем исправлена гонка (FOR UPDATE, UNIQUE или SERIALIZABLE)")
    item.ok(f"README: гонка и исправление ({', '.join(dict.fromkeys(f.upper() for f in fix))})")
    item.eye("В README понятно описаны шаги гонки и почему исправление работает?")


# ---------- Звёздочки ----------

def star_isolation(ctx: Ctx, item: Item) -> None:
    files = {"READ COMMITTED": "demo/isolation_rc.sql", "REPEATABLE READ": "demo/isolation_rr.sql"}
    for level, rel in files.items():
        res = need_script(ctx, item, rel)
        if not res.script.multi:
            raise Fail(f"{rel}: нужен сценарий в двух окнах ([A] и [B])")
        text = ctx.read(rel).upper()
        if level == "REPEATABLE READ" and level not in text:
            raise Fail(f"{rel}: не видно BEGIN ISOLATION LEVEL REPEATABLE READ")
        values = [e for s in res.steps for e, _, _ in s.verdicts if e.kind == "value"]
        if len(values) < 2:
            raise Fail(f"{rel}: нужны хотя бы два чтения с «-- expect: value ...» (до и после изменения в другом окне)")
        bad = report_failed_steps(item, rel, res)
        if bad:
            raise Fail(f"{rel}: не сбылись ожидания в шагах {', '.join(s.step.key for s in bad)}")
        item.ok(f"{rel}: {level}, все чтения дали ожидаемые значения")
    item.eye("Студент объяснил, почему на READ COMMITTED значение изменилось, а на REPEATABLE READ нет?")


def star_deadlock(ctx: Ctx, item: Item) -> None:
    rel = "demo/deadlock.sql"
    res = need_script(ctx, item, rel)
    hits = [s for s in res.steps if s.error and s.error.sqlstate == "40P01"]
    if not hits:
        raise Fail(f"{rel}: взаимоблокировка не случилась (нет ошибки 40P01 deadlock detected)")
    item.ok(f"окно {hits[0].step.session}: deadlock detected, база отменила одну транзакцию")
    bad = report_failed_steps(item, rel, res)
    if bad:
        raise Fail(f"{rel}: не сбылись ожидания в шагах {', '.join(s.step.key for s in bad)}")


def star_rls(ctx: Ctx, item: Item) -> None:
    tables = [t for t in catalog.rls_tables(ctx) if t["policies"]]
    if not tables:
        raise Fail("нет таблицы с ENABLE ROW LEVEL SECURITY и политикой")
    item.ok("RLS: " + ", ".join(f"{t['table']} ({t['policies']} полит.)" for t in tables))
    res = ctx.script("demo/rls.sql")
    if res is None:
        item.eye("Показал, что под ролью пользователя видны только его строки?")
        return
    bad = report_failed_steps(item, "demo/rls.sql", res)
    if bad or res.fatal:
        raise Fail("demo/rls.sql: не сбылись ожидания")
    item.ok("demo/rls.sql: под ролью пользователя видны только свои строки")


def star_backup(ctx: Ctx, item: Item) -> None:
    scripts = [p for p in (ctx.repo / "scripts").glob("*")] if (ctx.repo / "scripts").is_dir() else []
    text = ctx.readme() + "".join(p.read_text(encoding="utf-8", errors="replace") for p in scripts if p.is_file())
    if not (re.search(r"pg_dump", text) and re.search(r"pg_restore|psql\b.*<", text)):
        raise Fail("не видно резервной копии: pg_dump и pg_restore в README или в scripts/")
    item.ok("pg_dump и pg_restore описаны" + (f" ({', '.join(p.name for p in scripts)})" if scripts else ""))
    item.eye("Показано вживую: pg_dump, восстановление в чистую базу через pg_restore, данные на месте?")


LAB = Lab(
    number=4,
    title="Транзакции и доступ",
    base=[
        ("1", "Ошибка в середине транзакции откатывает всю транзакцию", check_transaction),
        ("2", "demo/race.sql: две сессии нарушают правило темы", check_race),
        ("3", "Гонка исправлена: demo/race_fixed.sql правило не нарушает", check_race_fixed),
        ("4", "Роли app_reader и app_writer, лишние действия запрещены", check_roles),
        ("5", "Пароли в .env, в репозитории только .env.example", check_secrets),
        ("6", "Гонка и исправление описаны в README", check_readme_race),
    ],
    stars=[
        Star("★ изоляция и взаимоблокировка", score_two, [
            ("Неповторяющееся чтение: READ COMMITTED против REPEATABLE READ", star_isolation),
            ("Взаимоблокировка воспроизведена", star_deadlock),
        ]),
        Star("★★ RLS и резервная копия", score_two, [
            ("Row Level Security: пользователь видит только свои строки", star_rls),
            ("pg_dump и pg_restore в чистую базу", star_backup),
        ]),
    ],
)
