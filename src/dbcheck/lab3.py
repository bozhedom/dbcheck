"""Лаба 3. Логика в базе: представления, функции, триггеры."""

from __future__ import annotations

import re

from . import catalog
from .core import Ctx, Fail, Item, Lab, Star, need_script, report_failed_steps, score_two

FILE = "demo/lab3.sql"
MODIFIES = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", re.I)


def _user_functions(ctx: Ctx) -> list[dict]:
    return [f for f in catalog.functions(ctx) if f["returns"] != "trigger"]


def _demo_text(ctx: Ctx) -> str:
    return ctx.read(FILE)


def check_views(ctx: Ctx, item: Item) -> None:
    views = catalog.views(ctx)
    if len(views) < 2:
        raise Fail(f"представлений: {len(views)}, нужно 2")
    for v in views:
        ctx.rows(f'SELECT * FROM "{v["schema"]}"."{v["name"]}" LIMIT 1')
    joined = [v["name"] for v in views if re.search(r"\bJOIN\b", v["definition"], re.I)]
    item.ok(f"представления: {', '.join(v['name'] for v in views)}, все читаются")
    if joined:
        item.ok(f"с JOIN по нескольким таблицам: {', '.join(joined)}")
    else:
        item.warn("ни одно представление не соединяет таблицы, «карточка» обычно собирается через JOIN")


def check_value_function(ctx: Ctx, item: Item) -> None:
    funcs = [f for f in _user_functions(ctx) if f["kind"] == "f" and f["returns"] != "void"]
    if not funcs:
        raise Fail("нет функции, которая возвращает значение или таблицу")
    item.ok("функции: " + ", ".join(f"{f['name']}(): {'таблица' if f['returns_set'] else f['returns']}"
                                    for f in funcs[:5]) + ("..." if len(funcs) > 5 else ""))


def check_business_operation(ctx: Ctx, item: Item) -> None:
    ops = [f for f in _user_functions(ctx) if MODIFIES.search(f["source"] or "")]
    if not ops:
        raise Fail("нет функции или процедуры, которая меняет данные (бизнес-операция одной командой)")
    item.ok("бизнес-операции: " + ", ".join(f"{'CALL ' if f['kind'] == 'p' else ''}{f['name']}()" for f in ops[:6]))
    demo = _demo_text(ctx)
    called = [f["name"] for f in ops if re.search(rf"\b{re.escape(f['name'])}\s*\(", demo, re.I)]
    if not called:
        raise Fail(f"ни одна бизнес-операция не вызывается в {FILE}")
    item.ok(f"вызывается в демо: {', '.join(called)}")


def _rule_triggers(ctx: Ctx) -> list[dict]:
    return [t for t in catalog.triggers(ctx)
            if re.search(r"\bRAISE\s+(EXCEPTION\b|')", t["source"] or "", re.I) or
            re.search(r"\bRAISE\s+USING\b", t["source"] or "", re.I)]


def check_rule_trigger(ctx: Ctx, item: Item) -> None:
    rules = _rule_triggers(ctx)
    if not rules:
        raise Fail("нет триггера, который отклоняет нарушение правила (RAISE EXCEPTION в функции триггера)")
    item.ok("триггеры-правила: " + ", ".join(f"{t['name']} ({t['timing']} {t['events']} ON {t['table']})" for t in rules))
    disabled = [t["name"] for t in rules if t["enabled"] == "D"]
    if disabled:
        raise Fail(f"триггер выключен: {', '.join(disabled)}")
    res = need_script(ctx, item, FILE)
    hits = [s for s in res.steps if s.error and any(e.kind == "error" and ok for e, ok, _ in s.verdicts)
            and s.error.sqlstate not in ("23505", "23503", "23502")]
    if not hits:
        raise Fail(f"в {FILE} нет шага, где правило нарушается: «-- expect: error ...» и ошибка от триггера")
    for s in hits[:3]:
        item.ok(f"шаг {s.step.key}: {s.error.message[:90]}")


def check_service_trigger(ctx: Ctx, item: Item) -> None:
    found = []
    for t in catalog.triggers(ctx):
        src = t["source"] or ""
        if re.search(r"NEW\s*\.\s*\w*(updated|modified|changed)\w*\s*(:=|=)", src, re.I):
            column = re.search(r"NEW\s*\.\s*(\w+)", src, re.I).group(1)
            found.append(f"{t['name']}: заполняет {column}")
        elif re.search(r"INSERT\s+INTO\s+\w*(history|audit|log|journal|changes)\w*", src, re.I):
            found.append(f"{t['name']}: пишет историю изменений")
    if not found:
        raise Fail("нет служебного триггера: заполнение updated_at или запись в таблицу истории")
    for f in found:
        item.ok(f)


def check_from_scripts(ctx: Ctx, item: Item) -> None:
    sql = ctx.db_sql_text().lower()
    names = [("представление", v["name"]) for v in catalog.views(ctx) + catalog.views(ctx, "m")]
    names += [("функция", f["name"]) for f in catalog.functions(ctx)]
    names += [("триггер", t["name"]) for t in catalog.triggers(ctx)]
    missing = [f"{kind} {name}" for kind, name in names if name.lower() not in sql]
    if missing:
        raise Fail("есть в базе, но не создаётся скриптами db/: " + ", ".join(missing[:5]))
    item.ok(f"все {len(names)} объектов (представления, функции, триггеры) создаются скриптами db/")
    res = need_script(ctx, item, FILE)
    bad = report_failed_steps(item, FILE, res)
    if bad:
        raise Fail(f"{FILE}: не сбылись ожидания в шагах {', '.join(s.step.key for s in bad)}")
    steps = [s for s in res.steps if s.step.key != "0"]
    item.ok(f"{FILE}: все {len(steps)} шагов ответили как ожидалось")


# ---------- Звёздочки ----------

def star_matview(ctx: Ctx, item: Item) -> None:
    mviews = catalog.views(ctx, "m")
    if not mviews:
        raise Fail("нет материализованного представления")
    item.ok(f"материализованные: {', '.join(v['name'] for v in mviews)}")
    if not re.search(r"REFRESH\s+MATERIALIZED\s+VIEW", _demo_text(ctx), re.I):
        raise Fail(f"в {FILE} нет REFRESH MATERIALIZED VIEW, не видно, что отчёт обновляется по команде")
    item.ok("REFRESH есть в демо")


def star_table_function(ctx: Ctx, item: Item) -> None:
    funcs = [f for f in _user_functions(ctx) if f["returns_set"] and f["nargs"] >= 1]
    if not funcs:
        raise Fail("нет функции с параметрами, возвращающей таблицу (RETURNS TABLE / SETOF)")
    item.ok(", ".join(f"{f['name']}({', '.join(a.split()[0] for a in f['args'].split(', '))})" for f in funcs[:3]))
    demo = _demo_text(ctx)
    if not any(re.search(rf"\b{re.escape(f['name'])}\s*\(", demo, re.I) for f in funcs):
        item.half = True
        item.info(f"функция не вызывается в {FILE}")


def star_journal(ctx: Ctx, item: Item) -> None:
    jsonb_tables = [t for t in catalog.tables(ctx, count=False)
                    if sum(1 for c in t.columns if c["type"] == "jsonb") >= 1 and t.service]
    if not jsonb_tables:
        jsonb_tables = [t for t in catalog.tables(ctx, count=False)
                        if sum(1 for c in t.columns if c["type"] == "jsonb") >= 2]
    if not jsonb_tables:
        raise Fail("нет таблицы-журнала с jsonb (старое и новое значение)")
    names = {t.name.lower() for t in jsonb_tables}
    writers = [t for t in catalog.triggers(ctx)
               if any(re.search(rf"INSERT\s+INTO\s+(\w+\.)?{re.escape(n)}\b", t["source"] or "", re.I) for n in names)]
    if not writers:
        raise Fail(f"в журнал {', '.join(names)} не пишет ни один триггер")
    cols = [c["name"] for c in jsonb_tables[0].columns]
    item.ok(f"журнал {jsonb_tables[0].name}({', '.join(cols)}), пишут: {', '.join(t['name'] for t in writers)}")
    if not any(re.search(r"(user|by|who|author)", c, re.I) for c in cols):
        item.half = True
        item.info("в журнале не видно, кто менял")


def star_state_at(ctx: Ctx, item: Item) -> None:
    ts_types = {"timestamp with time zone", "timestamp without time zone", "date"}
    funcs = [f for f in _user_functions(ctx)
             if f["nargs"] >= 1 and any(ctx.scalar("SELECT format_type(%s, NULL)", (oid,)) in ts_types
                                        for oid in (f["argtypes"] or []))
             and re.search(r"(history|audit|log|journal|changes)", f["source"] or "", re.I)]
    if not funcs:
        raise Fail("нет функции «состояние записи на дату» (параметр-время, читает журнал)")
    item.ok(", ".join(f"{f['name']}({f['args']})" for f in funcs))
    if not any(re.search(rf"\b{re.escape(f['name'])}\s*\(", _demo_text(ctx), re.I) for f in funcs):
        item.half = True
        item.info(f"функция не вызывается в {FILE}")


LAB = Lab(
    number=3,
    title="Логика в базе",
    base=[
        ("1", "2 представления для частых запросов", check_views),
        ("2", "Функция, возвращающая значение или таблицу", check_value_function),
        ("3", "Функция или процедура, выполняющая бизнес-операцию", check_business_operation),
        ("4", "Триггер бизнес-правила с понятной ошибкой", check_rule_trigger),
        ("5", "Служебный триггер: updated_at или история", check_service_trigger),
        ("6", "Всё создаётся из db/, demo/lab3.sql проходит", check_from_scripts),
    ],
    stars=[
        Star("★ отчёт и фильтры", score_two, [
            ("Материализованное представление + REFRESH", star_matview),
            ("Функция с параметрами-фильтрами, возвращающая таблицу", star_table_function),
        ]),
        Star("★★ журнал изменений", score_two, [
            ("Журнал главной таблицы: старое и новое в jsonb, кто и когда", star_journal),
            ("Функция «состояние записи на дату»", star_state_at),
        ]),
    ],
)
