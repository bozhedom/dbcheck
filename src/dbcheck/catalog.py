"""Запросы к системному каталогу PostgreSQL. Чекер смотрит только на то, что реально есть в базе."""

from __future__ import annotations

from dataclasses import dataclass, field

from .core import Ctx

# Схемы студента: всё, кроме системных и схем расширений
USER_SCHEMAS = """
    n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    AND n.nspname !~ '^pg_(toast_)?temp_'
"""
NOT_EXTENSION = """
    NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = {oid} AND d.deptype = 'e')
"""
SERVICE_WORDS = ("history", "audit", "log", "journal", "changes", "истори", "журнал")


@dataclass
class Table:
    oid: int
    schema: str
    name: str
    kind: str                 # r: обычная, p: секционированная
    rows: int = 0
    columns: list[dict] = field(default_factory=list)

    @property
    def qname(self) -> str:
        return self.name if self.schema == "public" else f"{self.schema}.{self.name}"

    @property
    def service(self) -> bool:
        return any(w in self.name.lower() for w in SERVICE_WORDS)


def tables(ctx: Ctx, *, count: bool = True) -> list[Table]:
    key = f"tables:{count}"
    if key in ctx.state:
        return ctx.state[key]
    rows = ctx.rows(f"""
        SELECT c.oid, n.nspname, c.relname, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND {USER_SCHEMAS}
          AND {NOT_EXTENSION.format(oid='c.oid')}
        ORDER BY n.nspname, c.relname""")
    result = [Table(*r) for r in rows]
    for t in result:
        t.columns = ctx.dicts("""
            SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS notnull,
                   t.typtype AS typtype, a.atttypid AS typoid
            FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
            WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""", (t.oid,))
        if count:
            t.rows = ctx.scalar(f'SELECT count(*) FROM "{t.schema}"."{t.name}"')
    ctx.state[key] = result
    return result


def constraints(ctx: Ctx, contype: str) -> list[dict]:
    """contype: p первичный ключ, f внешний, u UNIQUE, c CHECK, x EXCLUDE."""
    return ctx.dicts(f"""
        SELECT con.oid, con.conname AS name, c.relname AS table, n.nspname AS schema,
               rc.relname AS ref_table, con.confdeltype AS on_delete,
               ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
                     JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                     ORDER BY k.ord)::text[] AS columns,
               con.conkey::int[] AS attnums,
               pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_class rc ON rc.oid = con.confrelid
        WHERE con.contype = %s AND {USER_SCHEMAS} AND NOT c.relispartition
        ORDER BY c.relname, con.conname""", (contype,))


def unique_indexes(ctx: Ctx) -> list[dict]:
    """Уникальные индексы, которые не созданы ограничением (CREATE UNIQUE INDEX ...)."""
    return ctx.dicts(f"""
        SELECT i.relname AS name, c.relname AS table
        FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class c ON c.oid = x.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE x.indisunique AND NOT x.indisprimary AND {USER_SCHEMAS}
          AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = x.indexrelid)""")


def views(ctx: Ctx, kind: str = "v") -> list[dict]:
    """kind: v представления, m материализованные."""
    return ctx.dicts(f"""
        SELECT c.oid, n.nspname AS schema, c.relname AS name, pg_get_viewdef(c.oid) AS definition
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = %s AND {USER_SCHEMAS} AND {NOT_EXTENSION.format(oid='c.oid')}
        ORDER BY c.relname""", (kind,))


def functions(ctx: Ctx) -> list[dict]:
    """Функции и процедуры студента (без функций расширений)."""
    return ctx.dicts(f"""
        SELECT p.oid, n.nspname AS schema, p.proname AS name, p.prokind AS kind,
               l.lanname AS language, p.prosecdef AS security_definer, p.proretset AS returns_set,
               p.pronargs AS nargs, format_type(p.prorettype, NULL) AS returns,
               pg_get_function_identity_arguments(p.oid) AS args,
               p.proargtypes::oid[] AS argtypes,
               p.prosrc AS source
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
        WHERE {USER_SCHEMAS} AND p.prokind IN ('f', 'p') AND {NOT_EXTENSION.format(oid='p.oid')}
        ORDER BY p.proname""")


def triggers(ctx: Ctx) -> list[dict]:
    return ctx.dicts(f"""
        SELECT t.tgname AS name, c.relname AS table, p.proname AS function, p.prosrc AS source,
               t.tgenabled AS enabled,
               CASE WHEN t.tgtype & 2 = 2 THEN 'BEFORE' WHEN t.tgtype & 64 = 64 THEN 'INSTEAD OF' ELSE 'AFTER' END AS timing,
               CASE WHEN t.tgtype & 1 = 1 THEN 'ROW' ELSE 'STATEMENT' END AS level,
               concat_ws(' OR ', CASE WHEN t.tgtype & 4 = 4 THEN 'INSERT' END,
                         CASE WHEN t.tgtype & 8 = 8 THEN 'DELETE' END,
                         CASE WHEN t.tgtype & 16 = 16 THEN 'UPDATE' END,
                         CASE WHEN t.tgtype & 32 = 32 THEN 'TRUNCATE' END) AS events
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_proc p ON p.oid = t.tgfoid
        WHERE NOT t.tgisinternal AND {USER_SCHEMAS} AND NOT c.relispartition
        ORDER BY c.relname, t.tgname""")


def indexes(ctx: Ctx) -> list[dict]:
    return ctx.dicts(f"""
        SELECT i.relname AS name, c.relname AS table, am.amname AS method,
               x.indisprimary AS primary, x.indisunique AS unique,
               x.indnkeyatts AS nkeys, x.indnatts AS natts,
               x.indpred IS NOT NULL AS partial, x.indexprs IS NOT NULL AS expression,
               x.indkey::int[] AS attnums,
               EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = x.indexrelid) AS from_constraint,
               pg_get_indexdef(x.indexrelid) AS definition,
               pg_relation_size(x.indexrelid) AS bytes
        FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class c ON c.oid = x.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_am am ON am.oid = i.relam
        WHERE {USER_SCHEMAS} AND NOT c.relispartition
        ORDER BY c.relname, i.relname""")


def partitions(ctx: Ctx) -> list[dict]:
    return ctx.dicts(f"""
        SELECT p.relname AS parent, count(i.inhrelid) AS parts
        FROM pg_class p JOIN pg_namespace n ON n.oid = p.relnamespace
        LEFT JOIN pg_inherits i ON i.inhparent = p.oid
        WHERE p.relkind = 'p' AND {USER_SCHEMAS}
        GROUP BY p.relname""")


def roles(ctx: Ctx, *names: str) -> dict[str, dict]:
    rows = ctx.dicts("""SELECT rolname AS name, rolcanlogin AS login, rolsuper AS super, rolbypassrls AS bypassrls
                        FROM pg_roles WHERE rolname = ANY(%s)""", (list(names),))
    return {r["name"]: r for r in rows}


def table_privileges(ctx: Ctx, role: str) -> list[dict]:
    """Права роли на все таблицы и представления студента (с учётом членства в других ролях)."""
    return ctx.dicts(f"""
        SELECT c.relname AS name, c.relkind AS kind,
               has_table_privilege(%(r)s, c.oid, 'SELECT') AS sel,
               has_table_privilege(%(r)s, c.oid, 'INSERT') AS ins,
               has_table_privilege(%(r)s, c.oid, 'UPDATE') AS upd,
               has_table_privilege(%(r)s, c.oid, 'DELETE') AS del,
               has_table_privilege(%(r)s, c.oid, 'TRUNCATE') AS trn
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition AND {USER_SCHEMAS}
          AND {NOT_EXTENSION.format(oid='c.oid')}
        ORDER BY c.relname""", {"r": role})


def function_privileges(ctx: Ctx, role: str) -> list[dict]:
    return ctx.dicts(f"""
        SELECT p.proname AS name, p.prosecdef AS security_definer, p.prosrc AS source,
               has_function_privilege(%(r)s, p.oid, 'EXECUTE') AS can_execute
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE {USER_SCHEMAS} AND p.prokind IN ('f', 'p') AND {NOT_EXTENSION.format(oid='p.oid')}
          AND p.prorettype <> 'trigger'::regtype""", {"r": role})


def rls_tables(ctx: Ctx) -> list[dict]:
    return ctx.dicts(f"""
        SELECT c.relname AS table, c.relforcerowsecurity AS forced,
               (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relrowsecurity AND {USER_SCHEMAS}""")


def size_pretty(ctx: Ctx, table: str) -> tuple[str, str]:
    row = ctx.rows("SELECT pg_size_pretty(pg_table_size(%s::regclass)), pg_size_pretty(pg_indexes_size(%s::regclass))",
                   (table, table))
    return row[0] if row else ("?", "?")
