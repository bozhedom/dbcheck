"""Лаба 1. Модель данных и схема."""

from __future__ import annotations

import re

from . import catalog
from .core import Ctx, Fail, Item, Lab, Star, need_script, plural, report_failed_steps, score_two

MONEY_WORDS = re.compile(r"(price|cost|amount|sum|total|balance|salary|fee|payment|money|deposit|цена|сумма)", re.I)
STATUS_WORDS = re.compile(r"^(status|state|stage|kind|type|role|category|level)$|_(status|state|type|kind|role)$", re.I)


def mermaid_entities(readme: str) -> set[str] | None:
    """Имена сущностей из ```mermaid erDiagram```. None, если диаграммы нет."""
    blocks = re.findall(r"```mermaid\s*\n(.*?)```", readme, re.S)
    found: set[str] = set()
    has = False
    for block in blocks:
        if not re.search(r"^\s*erDiagram\b", block, re.M):
            continue
        has = True
        for m in re.finditer(r"^\s*\"?([\w.]+)\"?\s*(?:\[[^\]]*\])?\s*\{", block, re.M):
            found.add(m.group(1).lower())
        for m in re.finditer(r"^\s*\"?([\w.]+)\"?\s+[|}o.\-]{2,}[|{o.\-]*\s+\"?([\w.]+)\"?\s*:", block, re.M):
            found.update({m.group(1).lower(), m.group(2).lower()})
    return found if has else None


def junction_tables(ctx: Ctx) -> list[str]:
    """Связующие таблицы N:M: ≥ 2 внешних ключа на разные таблицы, и их колонки покрыты PK или UNIQUE."""
    fks = catalog.constraints(ctx, "f")
    keys = catalog.constraints(ctx, "p") + catalog.constraints(ctx, "u")
    result = []
    for t in catalog.tables(ctx):
        mine = [f for f in fks if f["table"] == t.name]
        targets = {f["ref_table"] for f in mine}
        if len(targets) < 2:
            continue
        fk_cols = [set(f["columns"]) for f in mine]
        for k in keys:
            if k["table"] != t.name:
                continue
            covered = [cols for cols in fk_cols if cols <= set(k["columns"])]
            if len(covered) >= 2:
                result.append(t.name)
                break
    return result


# ---------- База ----------

def check_er(ctx: Ctx, item: Item) -> None:
    readme = ctx.readme()
    if not readme:
        raise Fail("нет README.md")
    entities = mermaid_entities(readme)
    tables = {t.name.lower() for t in catalog.tables(ctx, count=False)}
    if entities is None:
        if re.search(r"dbdiagram\.io|drawsql|\.(png|svg|jpg)\)", readme, re.I):
            item.info("Mermaid-диаграммы нет, но есть ссылка или картинка")
            item.eye("ER-диаграмма в README: 5+ таблиц и связь N:M через связующую таблицу?")
        else:
            raise Fail("в README нет блока ```mermaid erDiagram``` (или ссылки на dbdiagram/картинку)")
    else:
        if len(entities) < 5:
            raise Fail(f"на диаграмме {plural(len(entities), 'сущность', 'сущности', 'сущностей')}, нужно 5+")
        item.ok(f"Mermaid erDiagram: {len(entities)} сущностей")
        missing = sorted(t for t in tables if t not in entities and not t.endswith(("_history", "_log", "_audit")))
        extra = sorted(e for e in entities if e not in tables)
        if missing:
            item.warn(f"на диаграмме нет таблиц из базы: {', '.join(missing[:6])}")
        if extra:
            item.warn(f"на диаграмме есть то, чего нет в базе: {', '.join(extra[:6])}")
    junctions = junction_tables(ctx)
    if not junctions:
        raise Fail("в базе нет связующей таблицы N:M (два внешних ключа, покрытые PK или UNIQUE)")
    item.ok(f"связь N:M: {', '.join(junctions)}")


def check_compose(ctx: Ctx, item: Item) -> None:
    compose = ctx.compose_file()
    if compose is None:
        raise Fail("нет docker-compose.yml (или compose.yaml)")
    text = compose.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"image:\s*['\"]?postgres", text):
        raise Fail(f"в {compose.name} нет сервиса с образом postgres")
    if "docker-entrypoint-initdb.d" not in text:
        raise Fail(f"в {compose.name} папка db/ не смонтирована в /docker-entrypoint-initdb.d, скрипты не запустятся сами")
    scripts = ctx.db_scripts()
    if not scripts:
        raise Fail("в папке db/ нет .sql-скриптов")
    item.ok(f"{compose.name}: postgres + автозапуск db/ ({', '.join(p.name for p in scripts[:5])}"
            f"{'...' if len(scripts) > 5 else ''})")
    sql = ctx.db_sql_text().lower()
    created = set(re.findall(r"create\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?(?:\w+\.)?\"?(\w+)", sql))
    tables = catalog.tables(ctx, count=False)
    if not tables:
        raise Fail("база пустая: скрипты db/ не создали ни одной таблицы (подробности: docker compose logs db)")
    manual = [t.name for t in tables if t.name.lower() not in created]
    if manual:
        raise Fail(f"таблицы есть в базе, но не создаются скриптами db/: {', '.join(manual[:5])}. "
                   "при пересоздании они пропадут")
    item.ok("все таблицы базы создаются скриптами из db/")
    if not (ctx.repo / ".env.example").exists():
        item.warn("нет .env.example: непонятно, какие переменные нужно задать")
    if ctx.fresh_done:
        item.ok("база пересоздана с нуля (down -v, затем up), схема и данные на месте")
    else:
        item.info("полная проверка с пересозданием базы: dbcheck --lab 1 --fresh")


def check_keys(ctx: Ctx, item: Item) -> None:
    tables = catalog.tables(ctx, count=False)
    pks = {c["table"] for c in catalog.constraints(ctx, "p")}
    no_pk = [t.name for t in tables if t.name not in pks]
    if no_pk:
        raise Fail(f"нет первичного ключа: {', '.join(no_pk)}")
    item.ok(f"первичный ключ есть у всех {len(tables)} таблиц")
    fks = catalog.constraints(ctx, "f")
    fk_cols = {(f["table"], col) for f in fks for col in f["columns"]}
    suspicious = [f"{t.name}.{col['name']}" for t in tables if not t.service for col in t.columns
                  if re.search(r"(_id|Id)$", col["name"]) and (t.name, col["name"]) not in fk_cols]
    if suspicious:
        raise Fail(f"похоже на связь, но внешнего ключа нет: {', '.join(suspicious[:5])}")
    if len(fks) < 4:
        raise Fail(f"внешних ключей {len(fks)}, для 5 таблиц со связью N:M нужно хотя бы 4")
    item.ok(f"{plural(len(fks), 'внешний ключ', 'внешних ключа', 'внешних ключей')}: "
            + ", ".join(f"{f['table']} -> {f['ref_table']}" for f in fks[:6]) + ("..." if len(fks) > 6 else ""))


def check_constraints(ctx: Ctx, item: Item) -> None:
    uniques = catalog.constraints(ctx, "u")
    uidx = catalog.unique_indexes(ctx)
    checks = catalog.constraints(ctx, "c")
    n_unique = len(uniques) + len(uidx)
    if n_unique < 2:
        raise Fail(f"UNIQUE: {n_unique}, нужно минимум 2 (первичный ключ не считается)")
    item.ok(f"UNIQUE: {n_unique}: " + ", ".join(f"{u['table']}({', '.join(u['columns'])})" for u in uniques[:4]))
    if len(checks) < 2:
        raise Fail(f"CHECK: {len(checks)}, нужно минимум 2 (например, цена > 0, конец ≥ начала)")
    item.ok(f"CHECK: {len(checks)}: " + "; ".join(c["definition"].removeprefix("CHECK ")[:50] for c in checks[:3]))
    pk_cols = {(c["table"], col) for c in catalog.constraints(ctx, "p") for col in c["columns"]}
    cols = [(t.name, col) for t in catalog.tables(ctx, count=False) for col in t.columns
            if (t.name, col["name"]) not in pk_cols]
    notnull = sum(1 for _, col in cols if col["notnull"])
    if notnull < 3:
        raise Fail(f"NOT NULL почти нигде нет ({notnull} колонок), обязательные поля должны быть NOT NULL")
    item.ok(f"NOT NULL: {notnull} из {len(cols)} колонок (кроме ключей)")


def check_rows(ctx: Ctx, item: Item) -> None:
    tables = catalog.tables(ctx)
    fks = catalog.constraints(ctx, "f")
    with_fk = {f["table"] for f in fks}
    small, big = [], []
    for t in tables:
        if t.rows >= 10:
            big.append(t)
        elif t.service:
            item.info(f"{t.name}: {t.rows} строк, служебная таблица, не считается")
        elif t.name not in with_fk and len(t.columns) <= 3:
            item.info(f"{t.name}: {t.rows} строк, похоже на справочник, не считается")
        else:
            small.append(t)
    if small:
        raise Fail("меньше 10 строк: " + ", ".join(f"{t.name} ({t.rows})" for t in small))
    if len(big) < 5:
        raise Fail(f"таблиц с 10+ строками: {len(big)}, нужно 5")
    item.ok(", ".join(f"{t.name} {t.rows}" for t in big))


def check_bad_inserts(ctx: Ctx, item: Item) -> None:
    res = need_script(ctx, item, "demo/lab1.sql")
    kinds = {"unique": "23505", "check": "23514", "fk": "23503"}
    names = {"unique": "дубликат (UNIQUE)", "check": "нарушение CHECK", "fk": "несуществующий внешний ключ"}
    got = {k: [] for k in kinds}
    for s in res.steps:
        for exp, ok, _ in s.verdicts:
            if exp.kind == "error" and ok and s.error:
                for k, code in kinds.items():
                    if s.error.sqlstate == code:
                        got[k].append(s.step.key)
    for k in kinds:
        if got[k]:
            item.ok(f"{names[k]}: шаг {', '.join(got[k])}: база ответила ошибкой {kinds[k]}")
    missing = [names[k] for k in kinds if not got[k]]
    bad = report_failed_steps(item, "demo/lab1.sql", res)
    if missing:
        raise Fail("в demo/lab1.sql нет плохой вставки с «-- expect: error ...», которую отклонила база: "
                   + ", ".join(missing))
    if bad:
        item.warn(f"demo/lab1.sql: не сбылись ожидания в шагах {', '.join(s.step.key for s in bad)}")


# ---------- Звёздочки ----------

def star_normal_form(ctx: Ctx, item: Item) -> None:
    readme = ctx.readme()
    if not re.search(r"3\s*НФ|3NF|нормальн|нормализ", readme, re.I):
        raise Fail("в README нет раздела про нормализацию (3НФ)")
    item.ok("в README есть раздел про нормализацию")
    item.eye("Обоснование 3НФ по делу: где была бы избыточность и как её убрали?")


def star_types(ctx: Ctx, item: Item) -> None:
    tables = catalog.tables(ctx, count=False)
    checks = catalog.constraints(ctx, "c")
    fk_cols = {(f["table"], col) for f in catalog.constraints(ctx, "f") for col in f["columns"]}
    problems, good = [], []
    has_tz = False
    for t in tables:
        for col in t.columns:
            name, typ = col["name"], col["type"]
            where = f"{t.name}.{name}"
            if typ.startswith("timestamp without"):
                problems.append(f"{where}: timestamp без часового пояса (нужен timestamptz)")
            if typ.startswith("timestamp with"):
                has_tz = True
            if typ in ("real", "double precision", "money"):
                if MONEY_WORDS.search(name):
                    problems.append(f"{where}: {typ} для денег (нужен numeric)")
                else:
                    ctx.warnings.append(f"{where} хранится как {typ}, проверьте, не нужен ли numeric")
            if STATUS_WORDS.search(name):
                in_check = any(re.search(rf"\b{re.escape(name)}\b", c["definition"]) for c in checks if c["table"] == t.name)
                if col["typtype"] == "e":
                    good.append(f"{where}: enum {typ}")
                elif (t.name, name) in fk_cols:
                    good.append(f"{where}: справочник (внешний ключ)")
                elif in_check:
                    good.append(f"{where}: CHECK со списком значений")
                elif typ.startswith(("text", "character")):
                    problems.append(f"{where}: статус строкой без enum, справочника или CHECK")
    for p in problems:
        item.info(p)
    if problems:
        raise Fail(f"неосознанные типы: {len(problems)}")
    if not has_tz:
        raise Fail("нет ни одной колонки timestamptz: время хранится без часового пояса или не хранится")
    item.ok("время хранится в timestamptz, денег во float нет")
    for g in good[:3]:
        item.ok(g)


def star_big_data(ctx: Ctx, item: Item) -> None:
    tables = catalog.tables(ctx)
    if not tables:
        raise Fail("в базе нет таблиц")
    biggest = max(tables, key=lambda t: t.rows)
    if biggest.rows < 1000:
        raise Fail(f"самая большая таблица {biggest.name}: {biggest.rows} строк, нужно 1000+")
    item.ok(f"{biggest.name}: {biggest.rows} строк")
    texts = ctx.db_sql_text()
    if (ctx.repo / "scripts").is_dir():
        texts += "".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in (ctx.repo / "scripts").iterdir() if p.is_file())
    if not re.search(r"generate_series|faker|random\(", texts, re.I):
        raise Fail("не видно генерации: нет generate_series/random() в db/ или скрипта с Faker в scripts/")
    item.ok("данные генерируются скриптом")


def star_on_delete(ctx: Ctx, item: Item) -> None:
    fks = catalog.constraints(ctx, "f")
    names = {"a": "NO ACTION (по умолчанию)", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
    default = [f for f in fks if f["on_delete"] == "a"]
    summary: dict[str, int] = {}
    for f in fks:
        summary[names[f["on_delete"]]] = summary.get(names[f["on_delete"]], 0) + 1
    item.info(", ".join(f"{k}: {v}" for k, v in summary.items()))
    readme = ctx.readme()
    if default and "NO ACTION" not in readme.upper():
        raise Fail("ON DELETE не выбран для: " + ", ".join(f"{f['table']}.{'/'.join(f['columns'])}" for f in default[:5]))
    if not re.search(r"ON DELETE|CASCADE|RESTRICT|SET NULL", readme, re.I):
        raise Fail("в README не объяснено, что будет при удалении родителя")
    item.ok("политика ON DELETE выбрана для каждой связи")
    item.eye("В README для каждой связи объяснено, почему выбран именно такой ON DELETE?")


LAB = Lab(
    number=1,
    title="Модель данных и схема",
    base=[
        ("1", "ER-диаграмма в README, 5+ таблиц, связь N:M", check_er),
        ("2", "docker compose up создаёт схему и данные из db/", check_compose),
        ("3", "Первичный ключ у каждой таблицы, связи сделаны внешними ключами", check_keys),
        ("4", "NOT NULL, ≥ 2 UNIQUE, ≥ 2 CHECK", check_constraints),
        ("5", "10+ строк в каждой основной таблице", check_rows),
        ("6", "Три плохие вставки из demo/lab1.sql отклонены базой", check_bad_inserts),
    ],
    stars=[
        Star("★ нормализация и типы", score_two, [
            ("3НФ с обоснованием в README", star_normal_form),
            ("Осознанные типы: numeric, timestamptz, enum/справочник", star_types),
        ]),
        Star("★★ данные и ON DELETE", score_two, [
            ("1000+ строк из скрипта генерации", star_big_data),
            ("ON DELETE выбран и объяснён для каждой связи", star_on_delete),
        ]),
    ],
)
