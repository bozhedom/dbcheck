"""Точка входа: dbcheck --lab N, dbcheck run <файл.sql>, dbcheck ask --lab N."""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .core import (FAIL, MANUAL, PASS, Ctx, Fail, LabResult, Unreachable, blue, bold, dim, green, red,
                   run_lab, yellow)


def load_lab(n: int):
    return importlib.import_module(f"dbcheck.lab{n}").LAB


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "run":
        sys.exit(run_command(argv[1:]))
    if argv and argv[0] == "ask":
        from .questions import ask_command
        sys.exit(ask_command(argv[1:]))
    sys.exit(check_command(argv))


def add_connection_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", help="строка подключения вместо .env, например postgresql://user:pass@host:5432/db")
    p.add_argument("--repo", default=".", help="корень репозитория (по умолчанию текущая папка)")


# ---------- dbcheck --lab N ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dbcheck",
        description="Проверяет лабы курса «Разработка баз данных» по живой базе PostgreSQL. Ещё команды: "
                    "«dbcheck run файл.sql»: прогнать сценарий; «dbcheck ask --lab N»: вопрос и правка для приёма.",
    )
    p.add_argument("--lab", type=int, choices=range(1, 6), required=True, metavar="N", help="номер лабы, 1-5")
    p.add_argument("--stars", action="store_true", help="проверить ещё ★ и ★★")
    p.add_argument("--all", action="store_true", help="регрессия: базы всех лаб от 1 до N")
    p.add_argument("--only", metavar="K", help="только пункт K базы, 1-6")
    p.add_argument("--fresh", action="store_true",
                   help="пересоздать базу с нуля (docker compose down -v && up) и проверить уже её")
    p.add_argument("--ci", action="store_true", help="режим CI: cp .env.example .env и --fresh")
    p.add_argument("-v", "--verbose", action="store_true", help="печатать ход сценариев demo/*.sql")
    add_connection_args(p)
    p.add_argument("--version", action="version", version=f"dbcheck {__version__}")
    return p


def check_command(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    if args.ci and not (repo / ".env").exists() and (repo / ".env.example").exists():
        shutil.copy(repo / ".env.example", repo / ".env")
        print(dim("      $ cp .env.example .env"))
    ctx = Ctx(repo, dsn=args.dsn, verbose=args.verbose, ci=args.ci)
    numbers = list(range(1, args.lab + 1)) if args.all else [args.lab]

    print(bold(f"dbcheck {__version__}") + dim(f" · лаба {args.lab} · {ctx.describe()} · коммит {ctx.commit()}"))
    if args.fresh or args.ci:
        if not ctx.docker_ok():
            print(red("--fresh: нет docker compose или compose-файла в репозитории"))
            return 2
        try:
            ctx.fresh()
        except Exception as exc:
            print(red(f"База не поднялась из скриптов db/: {exc}"))
            print(ctx.compose("logs", "--no-color", "--tail", "60", show=False, check=False))
            return 1

    error = ctx.wait_db(60 if (args.fresh or args.ci) else 3)
    if error:
        print(red(f"\nБаза не отвечает ({ctx.describe()}): {error.strip().splitlines()[0]}"))
        print("Запустите базу: docker compose up -d. Порт и пароль чекер берёт из .env (DB_PORT, POSTGRES_*).")
        return 2

    results: list[LabResult] = []
    try:
        for n in numbers:
            last = n == args.lab
            results.append(run_lab(ctx, load_lab(n), stars=args.stars and last, only=args.only if last else None))
    except KeyboardInterrupt:
        print(yellow("\nПрервано."))
        return 1
    except Unreachable as exc:
        print(red(f"\n{exc}"))
        return 2
    finally:
        ctx.close()

    print_summary(results)
    if ctx.warnings:
        print(yellow("\nПредупреждения (на баллы не влияют, но про это могут спросить):"))
        for w in dict.fromkeys(ctx.warnings):
            print(yellow(f"  ⚠ {w}"))
    if os.environ.get("GITHUB_STEP_SUMMARY"):  # отчёт для вкладки Checks, раздел Summary
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(report(ctx, results, args) + "\n")
    return 1 if any(r.base_failed for r in results) else 0


def verdict(r: LabResult) -> str:
    total = len(r.base)
    if total < 6:
        return f"{r.base_passed}/{total} из выбранных пунктов"
    if r.base_passed >= 4:
        return green("порог «принята» (≥ 4) пройден")
    if r.base_passed + r.base_manual >= 4:
        return blue("порог пройдёт, если преподаватель подтвердит пункты «глазами»")
    return red("порог «принята» (≥ 4) не пройден")


def stars_text(r: LabResult) -> str:
    parts = []
    for star, _, lo, hi in r.stars:
        name = star.title.split()[0]
        parts.append(f"{name} {lo}/2" if lo == hi else f"{name} {lo}-{hi}/2 (ждёт проверки глазами)")
    return " · ".join(parts)


def print_summary(results: list[LabResult]) -> None:
    print("\n" + bold("━━ Итог ━━"))
    for r in results:
        marks = "".join({PASS: green("✓"), FAIL: red("✗"), MANUAL: blue("👁")}.get(i.status, "?") for i in r.base)
        manual = f" + {r.base_manual} на проверку глазами" if r.base_manual else ""
        print(f"Лаба {r.lab.number}: база {r.base_passed}/{len(r.base)}{manual}  {marks}  {verdict(r)}")
        if r.stars:
            print(f"        {stars_text(r)}")
            print(dim("        звёздочки засчитываются, только если база принята и PR открыт вовремя"))


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:160]


def report(ctx: Ctx, results: list[LabResult], args) -> str:
    icon = {PASS: "✓", FAIL: "✗", MANUAL: "👁", "half": "◐"}
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    fresh = " · база пересоздана с нуля" if ctx.fresh_done else ""
    out = [f"## dbcheck: лаба {args.lab}", "",
           f"`dbcheck {__version__}` · коммит `{ctx.commit()}` · {now}{fresh}", ""]
    for r in results:
        out += [f"### Лаба {r.lab.number}. {r.lab.title}", "", "| № | Пункт | | Подробности |", "| --- | --- | --- | --- |"]
        for i in r.base:
            detail = i.reason or (i.pending[0] + " (проверит преподаватель)" if i.pending else
                                  next((t for k, t in reversed(i.lines) if k == "ok"), ""))
            out.append(f"| {i.key} | {_cell(i.title)} | {icon.get(i.status, '?')} | {_cell(detail)} |")
        for star, parts, lo, hi in r.stars:
            score = f"{lo}/2" if lo == hi else f"{lo}-{hi}/2"
            names = ", ".join(f"{icon.get(p.status, '?')} {p.title}" for p in parts)
            out.append(f"| {star.title.split()[0]} | {_cell(names)} | {score} | |")
        manual = f" + {r.base_manual} на проверку глазами" if r.base_manual else ""
        out += ["", f"**База:** {r.base_passed}/{len(r.base)}{manual}" + (f" · {stars_text(r)}" if r.stars else ""), ""]
    if ctx.warnings:
        out += ["**Предупреждения:**", ""] + [f"- ⚠ {w}" for w in dict.fromkeys(ctx.warnings)] + [""]
    return "\n".join(out)


# ---------- dbcheck run файл.sql ----------

def run_command(argv: list[str]) -> int:
    from .sqlscript import Printer, run_script
    p = argparse.ArgumentParser(
        prog="dbcheck run",
        description="Прогоняет SQL-сценарий по шагам и сверяет ответы базы с комментариями «-- expect:». "
                    "Файл без BEGIN/COMMIT выполняется в транзакции и откатывается, база не меняется.")
    p.add_argument("files", nargs="+", type=Path, metavar="файл.sql")
    p.add_argument("--sql", action="store_true", help="показывать текст запросов")
    p.add_argument("--keep", action="store_true", help="не откатывать изменения (как в psql)")
    add_connection_args(p)
    args = p.parse_args(argv)
    ctx = Ctx(Path(args.repo).resolve(), dsn=args.dsn, verbose=False, ci=False)
    error = ctx.wait_db(3)
    if error:
        print(red(f"База не отвечает ({ctx.describe()}): {error.strip().splitlines()[0]}"))
        return 2
    ctx.close()
    code = 0
    for path in args.files:
        if not path.exists():
            print(red(f"Нет файла {path}"))
            code = 2
            continue
        printer = Printer(show_sql=args.sql, indent="  ")
        print(bold(f"━━ {path} ━━"))
        kw = {} if args.keep is False else {"keep": True}
        res = run_script(ctx.factory(), path, printer, **kw)
        print(dim(f"  режим: {res.mode}"))
        steps = [s for s in res.steps if s.step.key != "0"]
        bad = [s for s in steps if not s.passed]
        if res.fatal:
            print(red(f"  ✗ {res.fatal}"))
            code = 1
        elif bad:
            print(red(f"  ✗ не сбылось ожиданий: {len(bad)} из {len(steps)} шагов: "
                      + ", ".join(s.step.key for s in bad)))
            code = 1
        else:
            print(green(f"  ✓ все {len(steps)} шагов ответили как ожидалось"))
        print()
    return code
