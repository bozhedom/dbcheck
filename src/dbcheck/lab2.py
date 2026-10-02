"""Лаба 2. Запросы к своим данным: demo/lab2.sql, пронумерованные запросы с вопросом обычным языком."""

from __future__ import annotations

import re

from .core import Ctx, Fail, Item, Lab, Star, need_script, plural, score_two
from .sqlscript import step_sql

FILE = "demo/lab2.sql"
AGG = r"\b(count|sum|avg|min|max|string_agg|array_agg|bool_and|bool_or)\s*\("
RANKING = r"\b(row_number|rank|dense_rank|ntile)\s*\(\s*\)\s*OVER\b"
WINDOW = r"\b(\w+)\s*\([^()]*(?:\([^()]*\)[^()]*)*\)\s*(?:FILTER\s*\([^)]*\)\s*)?OVER\b"


def _steps(ctx: Ctx, item: Item):
    res = need_script(ctx, item, FILE)
    steps = [s for s in res.steps if s.step.key.isdigit() and s.step.key != "0"]
    if not steps:
        raise Fail(f"в {FILE} нет пронумерованных запросов (перед каждым нужен комментарий «-- 1. Вопрос»)")
    return res, steps


def _find(steps, pattern: str, flags=re.I | re.S) -> list[str]:
    return [s.step.key for s in steps if re.search(pattern, step_sql(s.step), flags)]


def _sql(s) -> str:
    return step_sql(s.step)


def check_joins(ctx: Ctx, item: Item) -> None:
    _, steps = _steps(ctx, item)
    joins = _find(steps, r"\bJOIN\b")
    if len(joins) < 3:
        raise Fail(f"запросов с JOIN: {len(joins)} ({', '.join(joins) or 'нет'}), нужно 3")
    item.ok(f"JOIN в запросах {', '.join(joins)}")
    wide = [s.step.key for s in steps if len(re.findall(r"\bJOIN\b", _sql(s), re.I)) >= 2]
    if not wide:
        raise Fail("нет запроса, соединяющего 3+ таблицы (два JOIN в одном запросе)")
    item.ok(f"3+ таблицы: запрос {', '.join(wide)}")


def check_group_by(ctx: Ctx, item: Item) -> None:
    _, steps = _steps(ctx, item)
    grouped = [s.step.key for s in steps if re.search(r"\bGROUP\s+BY\b", _sql(s), re.I) and re.search(AGG, _sql(s), re.I)]
    if len(grouped) < 2:
        raise Fail(f"запросов с GROUP BY и агрегатом: {len(grouped)}, нужно 2")
    item.ok(f"GROUP BY + агрегат: {', '.join(grouped)}")
    having = _find(steps, r"\bHAVING\b")
    if not having:
        raise Fail("нет запроса с HAVING")
    item.ok(f"HAVING: {', '.join(having)}")


def check_orphans(ctx: Ctx, item: Item) -> None:
    _, steps = _steps(ctx, item)
    found = [s for s in steps if re.search(r"\bLEFT\s+(OUTER\s+)?JOIN\b", _sql(s), re.I)
             and re.search(r"\bIS\s+NULL\b", _sql(s), re.I)]
    if not found:
        raise Fail("нет запроса «сироты»: LEFT JOIN ... WHERE <ключ справа> IS NULL")
    for s in found:
        rows = s.last_rows
        n = (rows.rowcount if rows and rows.rowcount >= 0 else 0)
        item.ok(f"запрос {s.step.key}: «{s.step.title[:60]}»: {plural(n, 'строка', 'строки', 'строк')}")
    if all(not s.last_rows or s.last_rows.rowcount == 0 for s in found):
        raise Fail("запрос-сироты ничего не нашёл: добавьте в данные запись без пары, иначе не видно, что он работает")


def check_subqueries(ctx: Ctx, item: Item) -> None:
    _, steps = _steps(ctx, item)
    keys = []
    for s in steps:
        text = re.sub(r"\bAS\s*(NOT\s+)?(MATERIALIZED\s*)?\(\s*SELECT", "AS (CTE", _sql(s), flags=re.I)
        if re.search(r"\(\s*SELECT\b", text, re.I):
            keys.append(s.step.key)
    if len(keys) < 2:
        raise Fail(f"запросов с подзапросом (IN, EXISTS, скалярный): {len(keys)}, нужно 2")
    kinds = []
    for word, pattern in (("IN", r"\bIN\s*\(\s*SELECT"), ("EXISTS", r"\bEXISTS\s*\(\s*SELECT"),
                          ("скалярный", r"[=<>,]\s*\(\s*SELECT|SELECT[^;]*?\(\s*SELECT")):
        if _find(steps, pattern):
            kinds.append(word)
    item.ok(f"подзапросы в {', '.join(keys)}" + (f" ({', '.join(kinds)})" if kinds else ""))


def check_dml(ctx: Ctx, item: Item) -> None:
    _, steps = _steps(ctx, item)
    found = {}
    for verb in ("INSERT", "UPDATE", "DELETE"):
        hits = [s for s in steps if any(re.match(rf"^\s*(WITH\b.*?\)\s*)?{verb}\b", sql, re.I | re.S)
                                        for _, sql in s.step.statements)]
        if not hits:
            raise Fail(f"нет {verb} в {FILE}")
        found[verb] = hits
    for verb in ("UPDATE", "DELETE"):
        for s in found[verb]:
            for _, sql in s.step.statements:
                if re.match(rf"^\s*{verb}\b", sql, re.I) and not re.search(r"\bWHERE\b", sql, re.I):
                    raise Fail(f"шаг {s.step.key}: {verb} без WHERE меняет всю таблицу")
    for verb, hits in found.items():
        for s in hits:
            for st in s.stmts:
                if st.status.startswith(verb) and st.rowcount == 0:
                    raise Fail(f"шаг {s.step.key}: {verb} не затронул ни одной строки, условие ничего не нашло")
    item.ok("INSERT: " + ", ".join(s.step.key for s in found["INSERT"]) + " · UPDATE: "
            + ", ".join(s.step.key for s in found["UPDATE"]) + " · DELETE: " + ", ".join(s.step.key for s in found["DELETE"]))
    no_check = [s.step.key for hits in found.values() for s in hits
                if sum(1 for _, sql in s.step.statements if re.match(r"^\s*SELECT\b", sql, re.I)) < 2
                and not any(re.search(r"\bRETURNING\b", sql, re.I) for _, sql in s.step.statements)]
    if no_check:
        item.info(f"в шагах {', '.join(no_check)} нет SELECT до и после")
        item.eye("Изменения данных показаны «до и после»?")
    else:
        item.ok("у каждого изменения есть проверка до и после")


def check_all_run(ctx: Ctx, item: Item) -> None:
    res, steps = _steps(ctx, item)
    if len(steps) < 12:
        raise Fail(f"в {FILE} {plural(len(steps), 'запрос', 'запроса', 'запросов')}, нужно 12")
    errors = [s for s in steps if s.error]
    for s in errors[:5]:
        item.info(f"запрос {s.step.key}: {s.error.short()}")
    if errors:
        raise Fail(f"с ошибкой: {', '.join(s.step.key for s in errors)}")
    empty = []
    for s in steps:
        rows = s.last_rows
        if rows is not None and (rows.rowcount if rows.rowcount >= 0 else len(rows.rows)) == 0:
            empty.append(s.step.key)
    if empty:
        raise Fail(f"пустой результат у запросов {', '.join(empty)}, данные должны отвечать на вопрос")
    unmet = [s for s in steps if not s.passed]
    if unmet:
        raise Fail("не сбылись «-- expect:» в шагах " + ", ".join(s.step.key for s in unmet))
    untitled = [s.step.key for s in steps if len(s.step.title) < 8]
    if untitled:
        item.warn(f"у запросов {', '.join(untitled)} нет вопроса обычным языком в заголовке")
    item.ok(f"{len(steps)} запросов без ошибок, у всех непустой результат")
    item.eye("Вопрос в комментарии и результат совпадают по смыслу (запрос отвечает на свой вопрос)?")


# ---------- Звёздочки ----------

def star_top_n(ctx: Ctx, item: Item) -> None:
    _, steps = _steps(ctx, item)
    cte = _find(steps, r"^\s*WITH\b")
    if not cte:
        raise Fail("нет запроса с WITH (CTE)")
    item.ok(f"CTE: {', '.join(cte)}")
    ranked = _find(steps, RANKING)
    if not ranked:
        raise Fail("нет ROW_NUMBER()/RANK() OVER для «топ-N в каждой категории»")
    part = [k for k in ranked if re.search(r"PARTITION\s+BY", step_sql(next(s for s in steps if s.step.key == k).step), re.I)]
    item.ok(f"рейтинг: {', '.join(ranked)}" + ("" if part else " (без PARTITION BY: топ по всей таблице)"))
    if not part:
        item.half = True


def star_second_window(ctx: Ctx, item: Item) -> None:
    _, steps = _steps(ctx, item)
    funcs: dict[str, list[str]] = {}
    for s in steps:
        for m in re.finditer(WINDOW, _sql(s), re.I):
            funcs.setdefault(m.group(1).lower(), []).append(s.step.key)
    others = {f: k for f, k in funcs.items() if f not in ("row_number", "rank", "dense_rank", "ntile")}
    if not others:
        raise Fail("кроме рейтинга нет второй оконной функции: накопительная SUM() OVER (ORDER BY ...) или LAG()")
    item.ok("оконные функции: " + ", ".join(f"{f}() в {', '.join(sorted(set(k)))}" for f, k in funcs.items()))


def star_monthly(ctx: Ctx, item: Item) -> None:
    _, steps = _steps(ctx, item)
    hits = [s for s in steps if re.search(r"date_trunc\s*\(\s*''|to_char\s*\(|extract\s*\(\s*month", _sql(s), re.I)
            and re.search(r"\blag\s*\(", _sql(s), re.I)]
    if not hits:
        raise Fail("нет помесячной динамики: date_trunc('month', ...) и LAG() для сравнения с прошлым месяцем")
    item.ok(f"помесячная динамика с LAG: запрос {', '.join(s.step.key for s in hits)}")
    if not any(re.search(r"100|percent|pct|%", _sql(s), re.I) for s in hits):
        item.half = True
        item.info("процента к прошлому месяцу не видно")


def star_recursive(ctx: Ctx, item: Item) -> None:
    _, steps = _steps(ctx, item)
    hits = _find(steps, r"\bWITH\s+RECURSIVE\b")
    if not hits:
        raise Fail("нет WITH RECURSIVE (иерархия или календарь без пропусков)")
    item.ok(f"рекурсивный CTE: запрос {', '.join(hits)}")


LAB = Lab(
    number=2,
    title="Запросы к своим данным",
    base=[
        ("1", "3+ запроса с JOIN, один по 3+ таблицам", check_joins),
        ("2", "2+ запроса с GROUP BY и агрегатами, один с HAVING", check_group_by),
        ("3", "LEFT JOIN, находящий «сирот»", check_orphans),
        ("4", "2+ подзапроса (IN, EXISTS или скалярный)", check_subqueries),
        ("5", "INSERT, UPDATE и DELETE по условию с проверкой до и после", check_dml),
        ("6", "12 запросов выполняются и возвращают непустой результат", check_all_run),
    ],
    stars=[
        Star("★ CTE и оконные функции", score_two, [
            ("WITH + топ-N в каждой категории (ROW_NUMBER/RANK)", star_top_n),
            ("Вторая оконная функция: накопительная сумма или LAG", star_second_window),
        ]),
        Star("★★ аналитика", score_two, [
            ("Помесячная динамика с процентом к прошлому месяцу", star_monthly),
            ("Рекурсивный CTE", star_recursive),
        ]),
    ],
)
