"""Ядро чекера: подключение к базе, пункты чек-листа, звёздочки и вывод."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psycopg

PASS, FAIL, MANUAL, HALF = "pass", "fail", "manual", "half"


class Fail(Exception):
    """Пункт не засчитан. Текст исключения показывается студенту."""


class Unreachable(Exception):
    """База не отвечает."""


# ---------- Цвета ----------

def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # включает ANSI в консоли Windows
    return True


COLOR = _color_enabled()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def green(t): return c(t, "32")
def red(t): return c(t, "31")
def yellow(t): return c(t, "33")
def blue(t): return c(t, "36")
def dim(t): return c(t, "2")
def bold(t): return c(t, "1")


# ---------- Настройки репозитория ----------

def read_env(path: Path) -> dict[str, str]:
    """Простой разбор .env: KEY=value, комментарии, кавычки."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        env[key.strip().removeprefix("export ").strip()] = value
    return env


def read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    with path.open("rb") as fh:
        return tomllib.load(fh)


# ---------- Пункт чек-листа ----------

@dataclass
class Item:
    """Один пункт базы или одна часть звёздочки. Собирает строки вывода и ответы «глазами»."""

    ctx: "Ctx"
    key: str
    title: str
    lines: list[tuple[str, str]] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    half: bool = False
    status: str = PASS
    reason: str = ""

    def _print(self, kind: str, text: str) -> None:
        marks = {"ok": green("✓"), "bad": red("✗"), "info": dim("·"), "warn": yellow("⚠"), "eye": blue("👁")}
        print(f"      {marks[kind]} {text}")

    def ok(self, text: str) -> None:
        self.lines.append(("ok", text))
        self._print("ok", text)

    def info(self, text: str) -> None:
        self.lines.append(("info", text))
        self._print("info", text)

    def warn(self, text: str) -> None:
        self.ctx.warnings.append(text)
        self.lines.append(("warn", text))
        self._print("warn", text)

    def eye(self, question: str) -> None:
        """То, что видит только человек. Пункт помечается «на проверку глазами», решает преподаватель."""
        self.pending.append(question)
        self.lines.append(("eye", question))
        self._print("eye", question + dim("  (проверит преподаватель)"))


# ---------- Контекст прогона ----------

class Ctx:
    def __init__(self, repo: Path, *, dsn: str | None, verbose: bool, ci: bool):
        self.repo = repo
        self.env = read_env(repo / ".env")
        self.config = read_config(repo / "dbcheck.toml")
        self.verbose = verbose
        self.ci = ci
        self.fresh_done = False
        self.warnings: list[str] = []
        self.state: dict[str, Any] = {}
        self.params = self._params(dsn)
        self._conn: psycopg.Connection | None = None

    # ----- подключение -----

    def _params(self, dsn: str | None) -> dict[str, Any]:
        if dsn:
            return {"conninfo": dsn}
        e = {**self.env, **{k: v for k, v in os.environ.items() if k.startswith(("PG", "POSTGRES_", "DB_"))}}
        return {
            "host": e.get("PGHOST") or e.get("DB_HOST") or "localhost",
            "port": int(e.get("PGPORT") or e.get("DB_PORT") or 5432),
            "user": e.get("PGUSER") or e.get("POSTGRES_USER") or "postgres",
            "password": e.get("PGPASSWORD") or e.get("POSTGRES_PASSWORD") or "",
            "dbname": e.get("PGDATABASE") or e.get("POSTGRES_DB") or e.get("POSTGRES_USER") or "postgres",
        }

    def describe(self) -> str:
        p = self.params
        if "conninfo" in p:
            return re.sub(r"(password=|:)[^@\s]+@", r"\1***@", p["conninfo"])
        return f"{p['user']}@{p['host']}:{p['port']}/{p['dbname']}"

    def connect(self, user: str | None = None, password: str | None = None) -> psycopg.Connection:
        kw = dict(self.params)
        if user is not None:
            if "conninfo" in kw:
                kw["user"] = user
                kw["password"] = password or ""
            else:
                kw.update(user=user, password=password or "")
        conninfo = kw.pop("conninfo", "")
        conn = psycopg.connect(conninfo, connect_timeout=5, application_name="dbcheck", autocommit=True, **kw)
        return conn

    def factory(self) -> Callable[[], psycopg.Connection]:
        return lambda: self.connect()

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            try:
                self._conn = self.connect()
            except psycopg.OperationalError as exc:
                raise Unreachable(str(exc).strip().splitlines()[0]) from exc
        return self._conn

    def wait_db(self, timeout: float = 5) -> str | None:
        """Ждёт, пока база начнёт отвечать. Возвращает текст последней ошибки или None."""
        deadline = time.time() + timeout
        error = "нет ответа"
        while True:
            try:
                self.conn.execute("SELECT 1")
                return None
            except (Unreachable, psycopg.Error) as exc:
                error = str(exc)
                self._conn = None
            if time.time() >= deadline:
                return error
            time.sleep(1)

    def rows(self, sql: str, params: Any = None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def dicts(self, sql: str, params: Any = None) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            if not cur.description:
                return []
            names = [d.name for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def scalar(self, sql: str, params: Any = None) -> Any:
        rows = self.rows(sql, params)
        return rows[0][0] if rows else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    # ----- настройки темы -----

    def cfg(self, section: str, key: str, default: Any = None) -> Any:
        value = self.config.get(section, {}).get(key, default)
        return default if value in ("", None) else value

    # ----- файлы репозитория -----

    def file(self, rel: str) -> Path:
        return self.repo / rel

    def read(self, rel: str) -> str:
        p = self.file(rel)
        return p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""

    def readme(self) -> str:
        return self.read("README.md")

    def db_scripts(self) -> list[Path]:
        folder = self.repo / "db"
        if not folder.is_dir():
            return []
        return sorted(p for p in folder.iterdir() if p.suffix in (".sql", ".sh") and p.is_file())

    def db_sql_text(self) -> str:
        return "\n".join(p.read_text(encoding="utf-8-sig", errors="replace") for p in self.db_scripts())

    def git(self, *args: str) -> str | None:
        if not shutil.which("git"):
            return None
        proc = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else None

    def tracked_files(self) -> list[str] | None:
        out = self.git("ls-files")
        return out.splitlines() if out is not None else None

    def commit(self) -> str:
        return (self.git("rev-parse", "--short", "HEAD") or "").strip() or "нет git"

    def compose_file(self) -> Path | None:
        for name in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
            if (self.repo / name).exists():
                return self.repo / name
        return None

    def docker_ok(self) -> bool:
        if not shutil.which("docker") or self.compose_file() is None:
            return False
        return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0

    def compose(self, *args: str, show: bool = True, check: bool = True, timeout: int = 600) -> str:
        if show:
            print(dim(f"      $ docker compose {' '.join(args)}"))
        proc = subprocess.run(["docker", "compose", *args], cwd=self.repo, capture_output=True,
                              text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise Fail(f"docker compose {' '.join(args)} завершился с ошибкой: {proc.stderr.strip()[-400:]}")
        return proc.stdout + proc.stderr

    def fresh(self) -> None:
        """Пересоздаёт базу с нуля: down -v, затем up --wait. Так проверяется, что всё создаётся скриптами из db/."""
        self.close()
        self._conn = None
        self.compose("down", "-v", "--remove-orphans")
        self.compose("up", "-d", "--wait", timeout=900)
        self.fresh_done = True

    # ----- сценарии -----

    def script(self, rel: str, *, show: bool | None = None):
        """Прогоняет SQL-сценарий и запоминает результат (повторно не гоняет)."""
        from .sqlscript import Printer, run_script
        key = f"script:{rel}"
        if key not in self.state:
            path = self.file(rel)
            if not path.exists():
                self.state[key] = None
            else:
                printer = Printer(enabled=self.verbose if show is None else show, indent="        ")
                self.state[key] = run_script(self.factory(), path, printer)
        return self.state[key]

# ---------- Звёздочки: правила подсчёта ----------

def score_two(parts: list[bool]) -> int:
    """Две части: обе дают 2, одна даёт 1."""
    n = sum(parts)
    return 2 if n == len(parts) else (1 if n else 0)


def score_any(parts: list[bool]) -> int:
    """Одна из частей на выбор (лаба 5 ★★): полностью даёт 2."""
    return 2 if any(parts) else 0


# ---------- Описание лабы ----------

@dataclass
class Star:
    title: str
    scorer: Callable[[list[bool]], int]
    parts: list[tuple[str, Callable[[Ctx, Item], None]]]


@dataclass
class Lab:
    number: int
    title: str
    base: list[tuple[str, str, Callable[[Ctx, Item], None]]]
    stars: list[Star]


@dataclass
class LabResult:
    lab: Lab
    base: list[Item]
    stars: list[tuple[Star, list[Item], int, int]]  # звезда, части, минимум, максимум

    @property
    def base_passed(self) -> int:
        return sum(1 for i in self.base if i.status == PASS)

    @property
    def base_manual(self) -> int:
        return sum(1 for i in self.base if i.status == MANUAL)

    @property
    def base_failed(self) -> int:
        return sum(1 for i in self.base if i.status == FAIL)


def run_item(ctx: Ctx, key: str, title: str, fn: Callable[[Ctx, Item], None], indent: str = "  ") -> Item:
    item = Item(ctx, key, title)
    print(f"\n{indent}{bold(key)}  {bold(title)}")
    try:
        fn(ctx, item)
        if item.half:
            item.status = HALF
        elif item.pending:
            item.status = MANUAL
    except Fail as exc:
        item.status, item.reason = FAIL, str(exc)
        item.lines.append(("bad", str(exc)))
        item._print("bad", str(exc))
    except psycopg.Error as exc:
        msg = f"ошибка базы: {str(exc).strip().splitlines()[0]}"
        item.status, item.reason = FAIL, msg
        item.lines.append(("bad", msg))
        item._print("bad", msg)
    except Unreachable:
        raise
    except Exception as exc:  # ошибка самого чекера не должна ронять весь прогон
        msg = f"чекер не смог проверить пункт ({exc.__class__.__name__}: {exc}), сообщите преподавателю"
        item.status, item.reason = FAIL, msg
        item.lines.append(("bad", msg))
        item._print("bad", msg)
    verdict = {
        PASS: green("✓ засчитано"),
        FAIL: red("✗ не засчитано"),
        MANUAL: blue("👁 автоматическая часть пройдена, остальное проверит преподаватель"),
        HALF: yellow("◐ частично"),
    }[item.status]
    print(f"      {verdict}")
    return item


def run_lab(ctx: Ctx, lab: Lab, *, stars: bool, only: str | None) -> LabResult:
    print("\n" + bold(f"━━ Лаба {lab.number}. {lab.title} ━━"))
    result = LabResult(lab, [], [])
    for key, title, fn in lab.base:
        if only and key != only:
            continue
        result.base.append(run_item(ctx, key, title, fn))
    if stars and not only:
        for star in lab.stars:
            print("\n" + bold(f"  {star.title}"))
            parts = [run_item(ctx, f"{star.title[:2].strip()}.{n}", t, fn, indent="    ")
                     for n, (t, fn) in enumerate(star.parts, 1)]
            if star.scorer is score_any:
                hi = 2 if any(p.status in (PASS, MANUAL) for p in parts) else (1 if any(p.status == HALF for p in parts) else 0)
                lo = 2 if any(p.status == PASS for p in parts) else (1 if any(p.status == HALF for p in parts) else 0)
                chosen = [p.title for p in parts if p.status in (PASS, MANUAL, HALF)]
                if chosen:
                    print(dim(f"      одно на выбор, засчитывается: {chosen[0]}; остальные варианты не нужны"))
            else:
                hi = star.scorer([p.status in (PASS, MANUAL) for p in parts])
                lo = star.scorer([p.status == PASS for p in parts])
            result.stars.append((star, parts, lo, hi))
    return result


# ---------- Помощники для пунктов ----------

def need_script(ctx: Ctx, item: Item, rel: str):
    """Прогоняет сценарий; если файла нет, пункт не засчитан."""
    res = ctx.script(rel)
    if res is None:
        raise Fail(f"нет файла {rel}")
    if res.fatal:
        raise Fail(f"{rel}: {res.fatal}")
    return res


def report_failed_steps(item: Item, rel: str, res) -> list:
    """Печатает шаги сценария, ожидания которых не сбылись."""
    bad = res.failed
    for s in bad[:6]:
        why = "; ".join(w for _, ok, w in s.verdicts if not ok)
        item.info(red(f"{rel} шаг {s.step.key} «{s.step.title[:60]}»: {why}"))
    return bad


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        word = one
    elif 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"
