"""Лаба 5. Производительность: большие данные, EXPLAIN ANALYZE до и после, индексы."""

from __future__ import annotations

import re

from . import catalog
from .core import Ctx, Fail, Item, Lab, Star, need_script, plural, score_any, score_two
from .lab4 import main_table
from .sqlscript import PLAN_ALIASES, execution_time, plan_text

FILE = "demo/lab5.sql"


def _plan_steps(ctx: Ctx, item: Item):
    """Шаги demo/lab5.sql с «-- expect: plan ...», которые сбылись: (шаг, что ожидали)."""
    res = need_script(ctx, item, FILE)
    out = []
    for s in res.steps:
        for exp, ok, _ in s.verdicts:
            if exp.kind == "plan" and ok:
                out.append((s, exp.arg.lower()))
    return res, out


def _is_index_plan(arg: str) -> bool:
    return arg not in ("seq",) and not arg.startswith("seq")


def _ms(s) -> str:
    t = execution_time(s)
    return f"{t:.2f} мс" if t is not None else "без ANALYZE"


def check_big_table(ctx: Ctx, item: Item) -> None:
    name = main_table(ctx)
    if not name:
        raise Fail("главная таблица не найдена, укажите main_table в dbcheck.toml")
    rows = ctx.scalar(f"SELECT count(*) FROM {name}")
    if rows < 100_000:
        raise Fail(f"{name}: {rows} строк, нужно ≥ 100 000")
    item.ok(f"{name}: {rows:,} строк".replace(",", " "))
    texts = ctx.db_sql_text()
    if (ctx.repo / "scripts").is_dir():
        texts += "".join(p.read_text(encoding="utf-8", errors="replace") for p in (ctx.repo / "scripts").iterdir() if p.is_file())
    if not re.search(r"generate_series|faker", texts, re.I):
        raise Fail("не видно скрипта генерации (generate_series в db/ или Faker в scripts/)")
    item.ok("данные генерируются скриптом" + (" из db/, сами при docker compose up"
                                              if re.search(r"generate_series", ctx.db_sql_text(), re.I) else ""))


def check_before(ctx: Ctx, item: Item) -> None:
    res, plans = _plan_steps(ctx, item)
    seq = [s for s, arg in plans if arg.startswith("seq")]
    if len(seq) < 3:
        raise Fail(f"в {FILE} {plural(len(seq), 'запрос', 'запроса', 'запросов')} с «-- expect: plan seq», нужно 3 медленных «до»")
    for s in seq[:4]:
        item.ok(f"шаг {s.step.key}: Seq Scan, {_ms(s)}, {s.step.title[:55]}")
    readme = ctx.readme()
    if "Seq Scan" not in readme or not re.search(r"\d\s*(ms|мс)", readme):
        raise Fail("в README нет планов «до»: Seq Scan и время в мс")
    item.eye("В README для трёх запросов есть план и время «до»?")


def check_after(ctx: Ctx, item: Item) -> None:
    res, plans = _plan_steps(ctx, item)
    fast = [(s, arg) for s, arg in plans if _is_index_plan(arg)]
    if len(fast) < 3:
        raise Fail(f"в {FILE} {plural(len(fast), 'запрос', 'запроса', 'запросов')} с индексным планом "
                   "(«-- expect: plan index»), нужно 3 «после»")
    for s, arg in fast[:3]:
        item.ok(f"шаг {s.step.key}: {arg}, {_ms(s)}")
    regular = {t.name for t in catalog.tables(ctx, count=False) if not t.service}
    composite = [i for i in catalog.indexes(ctx) if not i["primary"] and not i["from_constraint"]
                 and i["nkeys"] >= 2 and i["method"] == "btree" and i["table"] in regular]
    if not composite:
        raise Fail("нет составного индекса (2+ колонки), созданного под запрос")
    item.ok("составной: " + ", ".join(f"{i['name']}" for i in composite[:3]))
    readme = ctx.readme()
    if not any(re.search(r"\b(до|before)\b", ln, re.I) and re.search(r"\b(после|after)\b", ln, re.I)
               for ln in readme.splitlines() if ln.lstrip().startswith("|")):
        raise Fail("в README нет таблицы «до / после»")
    item.ok("в README есть таблица «до / после»")


def check_fk_indexes(ctx: Ctx, item: Item) -> None:
    rows = {r[0]: r[1] for r in ctx.rows("""SELECT c.relname, c.reltuples::bigint FROM pg_class c
                                              JOIN pg_namespace n ON n.oid = c.relnamespace
                                              WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')""")}
    indexes = catalog.indexes(ctx)
    missing, covered, small = [], [], []
    for fk in catalog.constraints(ctx, "f"):
        cols = fk["attnums"]
        ok = any(i["table"] == fk["table"] and sorted(i["attnums"][:len(cols)]) == sorted(cols) for i in indexes)
        label = f"{fk['table']}({', '.join(fk['columns'])})"
        if ok:
            covered.append(label)
        elif rows.get(fk["table"], 0) >= 1000:
            missing.append(label)
        else:
            small.append(label)
    if not covered and not missing and not small:
        raise Fail("в базе нет внешних ключей")
    if missing:
        raise Fail("нет индекса на внешнем ключе: " + ", ".join(missing))
    item.ok(f"внешние ключи с индексом: {', '.join(covered)}")
    if small:
        item.info(f"без индекса, но таблица маленькая: {', '.join(small)}")


def check_not_used(ctx: Ctx, item: Item) -> None:
    res, plans = _plan_steps(ctx, item)
    hits = [s for s, arg in plans if arg.startswith("seq")
            and not any(re.match(r"^\s*DROP\s+INDEX", sql, re.I) for _, sql in s.step.statements)]
    if not hits:
        raise Fail(f"в {FILE} нет случая, когда индекс есть, но не используется (шаг с «plan seq» без DROP INDEX)")
    for s in hits[:3]:
        item.ok(f"шаг {s.step.key}: {s.step.title[:80]}")
    item.eye("Студент объяснил, почему индекс здесь не помогает?")


def check_sizes(ctx: Ctx, item: Item) -> None:
    name = main_table(ctx)
    if not name:
        raise Fail("главная таблица не найдена, укажите main_table в dbcheck.toml")
    table, idx = catalog.size_pretty(ctx, name)
    item.info(f"{name}: таблица {table}, индексы {idx}")
    readme = ctx.readme()
    if not re.search(r"pg_size_pretty|pg_relation_size|\d\s*(kB|MB|GB|КБ|МБ|ГБ)", readme):
        raise Fail("в README нет размеров таблицы и индексов (pg_size_pretty)")
    item.ok("размеры таблицы и индексов есть в README")


# ---------- Звёздочки ----------

def star_partial(ctx: Ctx, item: Item) -> None:
    special = [i for i in catalog.indexes(ctx) if i["partial"] or i["expression"]]
    if not special:
        raise Fail("нет частичного (WHERE ...) или функционального (lower(email)) индекса")
    item.ok(", ".join(f"{i['name']} ({'частичный' if i['partial'] else 'функциональный'})" for i in special[:4]))
    res, plans = _plan_steps(ctx, item)
    names = {i["name"].lower() for i in special}
    used = [s for s, _ in plans if any(n in plan_text(s).lower() for n in names)]
    if not used:
        item.half = True
        item.info(f"в {FILE} нет плана, где этот индекс используется («-- expect: plan <имя индекса>»)")
    else:
        item.ok(f"используется в шаге {used[0].step.key}")


def star_covering(ctx: Ctx, item: Item) -> None:
    covering = [i for i in catalog.indexes(ctx) if i["natts"] > i["nkeys"]]
    if not covering:
        raise Fail("нет покрывающего индекса (INCLUDE)")
    item.ok(", ".join(i["definition"].split(" USING ")[-1][:70] for i in covering[:2]))
    res, plans = _plan_steps(ctx, item)
    names = [i["name"] for i in covering]
    only = [s for s, _ in plans if any(n in plan_text(s) for n in PLAN_ALIASES["only"])]
    mine = [s for s in only if any(f"Index Only Scan using {n}" in plan_text(s) for n in names)]
    if mine:
        item.ok(f"Index Only Scan по покрывающему индексу в шаге {mine[0].step.key}, {_ms(mine[0])}")
    elif only:
        item.half = True
        item.info(f"Index Only Scan есть (шаг {only[0].step.key}), но не по INCLUDE-индексу")
    else:
        raise Fail(f"в {FILE} нет плана с Index Only Scan («-- expect: plan only»)")


def star_fts(ctx: Ctx, item: Item) -> None:
    gin = [i for i in catalog.indexes(ctx) if i["method"] == "gin"]
    if not gin:
        raise Fail("нет GIN-индекса для полнотекстового поиска")
    item.ok(", ".join(i["name"] for i in gin))
    res, plans = _plan_steps(ctx, item)
    def text(s) -> str:
        return " ".join(sql for _, sql in s.step.statements)

    fts = [s for s, arg in plans if _is_index_plan(arg) and "@@" in text(s)]
    cols = {m.group(1).lower() for s in fts
            for m in re.finditer(r"to_tsvector\s*\([^,]*,\s*(?:coalesce\s*\()?\s*(\w+)", text(s), re.I)}
    like = [s for s, arg in plans if arg.startswith("seq")
            and any(re.search(rf"\b{c}\s+I?LIKE\b", text(s), re.I) for c in cols)]
    if not fts:
        raise Fail(f"в {FILE} нет плана поиска через @@ по GIN-индексу")
    item.ok(f"tsvector @@ tsquery: шаг {fts[0].step.key}, {_ms(fts[0])}")
    if like:
        item.ok(f"сравнение с LIKE: шаг {like[0].step.key}, {_ms(like[0])}")
    else:
        item.half = True
        item.info("нет сравнения с LIKE («-- expect: plan seq»)")


def star_partitions(ctx: Ctx, item: Item) -> None:
    parts = [p for p in catalog.partitions(ctx) if p["parts"] >= 2]
    if not parts:
        raise Fail("нет секционированной таблицы (PARTITION BY) с 2+ секциями")
    item.ok(", ".join(f"{p['parent']}: {p['parts']} секций" for p in parts))
    item.eye("План запроса по дате читает только нужную секцию (partition pruning)?")


LAB = Lab(
    number=5,
    title="Производительность",
    base=[
        ("1", "Главная таблица ≥ 100 000 строк из скрипта генерации", check_big_table),
        ("2", "3 медленных запроса: план «до» с Seq Scan и время", check_before),
        ("3", "Индексы, включая составной: план «после» с индексом, таблица в README", check_after),
        ("4", "Индексы на внешних ключах", check_fk_indexes),
        ("5", "Случай, когда индекс не используется, с объяснением", check_not_used),
        ("6", "Размеры таблицы и индексов в README", check_sizes),
    ],
    stars=[
        Star("★ особые индексы", score_two, [
            ("Частичный или функциональный индекс", star_partial),
            ("Покрывающий индекс (INCLUDE) и Index Only Scan", star_covering),
        ]),
        Star("★★ одно на выбор", score_any, [
            ("(а) Полнотекстовый поиск: tsvector + GIN против LIKE", star_fts),
            ("(б) Секционирование по дате", star_partitions),
        ]),
    ],
)
