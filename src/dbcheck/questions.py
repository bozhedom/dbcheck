"""dbcheck ask --lab N: случайный вопрос из банка и живая правка, без повторов в течение дня."""

from __future__ import annotations

import argparse
import json
import random
from datetime import date
from pathlib import Path

from .core import blue, bold, dim, yellow

# Вопросы совпадают с разделом «Вопросы к сдаче» в docs/lab-N.md шаблона.
QUESTIONS = {
    1: [
        "Чем база данных удобнее таблицы Excel, когда с данными работают несколько человек?",
        "Что такое первичный ключ и зачем он нужен каждой таблице?",
        "Что такое внешний ключ? Покажите один в своей схеме.",
        "Какие бывают связи между таблицами? Приведите пример каждой из своей темы.",
        "Зачем для связи N:M нужна отдельная таблица? Покажите её на диаграмме.",
        "Чем PRIMARY KEY отличается от UNIQUE?",
        "Что проверяет CHECK? Покажите один из своих.",
        "Что такое нормализация и какую проблему она решает?",
        "Что произойдёт с зависимыми строками при удалении строки, на которую они ссылаются?",
        "Почему деньги не хранят во float?",
    ],
    2: [
        "В каком порядке база выполняет части запроса: FROM, WHERE, GROUP BY, HAVING, SELECT, ORDER BY?",
        "Чем INNER JOIN отличается от LEFT JOIN?",
        "Как найти строки, у которых нет пары в другой таблице? Покажите свой запрос.",
        "Что делает GROUP BY?",
        "Чем WHERE отличается от HAVING?",
        "Чем count(*) отличается от count(колонка)?",
        "Почему WHERE x = NULL ничего не находит и как написать правильно?",
        "Что такое подзапрос? Покажите один из своих.",
        "Что такое CTE (WITH) и зачем он нужен?",
        "Чем оконная функция отличается от GROUP BY?",
    ],
    3: [
        "Что такое представление? Хранит ли оно данные?",
        "Чем материализованное представление отличается от обычного?",
        "Чем функция отличается от процедуры?",
        "Что такое триггер и когда он срабатывает?",
        "Чем BEFORE-триггер отличается от AFTER-триггера?",
        "Что такое NEW и OLD в триггере?",
        "Что делает RAISE EXCEPTION?",
        "Какую логику лучше держать в базе, а какую в приложении? Приведите пример.",
        "Зачем нужна таблица истории и что в ней хранится?",
        "Чем опасны триггеры?",
    ],
    4: [
        "Что такое транзакция? Что делают BEGIN, COMMIT и ROLLBACK?",
        "Что означает каждая буква в ACID?",
        "Что такое гонка? Где она возникает в вашей теме?",
        "Что делает SELECT ... FOR UPDATE?",
        "Какие уровни изоляции есть в PostgreSQL и какой используется по умолчанию?",
        "Что такое неповторяющееся чтение?",
        "Что такое взаимоблокировка и что с ней делает база?",
        "Почему в PostgreSQL чтение не блокирует запись?",
        "Что делают GRANT и REVOKE?",
        "Почему приложение не должно подключаться к базе под суперпользователем?",
    ],
    5: [
        "Как база ищет строку в таблице без индекса?",
        "Что такое индекс и почему поиск по нему быстрее?",
        "Как в общих чертах устроено B-дерево?",
        "Чем EXPLAIN отличается от EXPLAIN ANALYZE?",
        "Чем Seq Scan отличается от Index Scan?",
        "Почему индекс замедляет вставку и изменение данных?",
        "Что такое составной индекс и почему важен порядок колонок в нём?",
        "В каких случаях база не использует индекс?",
        "Зачем нужен индекс на внешнем ключе?",
        "Как быстро создать в таблице 100 000 строк?",
    ],
}

EDITS = {
    1: [
        ("Добавьте колонку с CHECK (например, рейтинг от 1 до 5)",
         "ALTER TABLE ... ADD COLUMN ... CHECK (...); вставка с плохим значением даёт ошибку 23514"),
        ("Добавьте таблицу с внешним ключом на существующую таблицу",
         "CREATE TABLE ... REFERENCES ...; вставка с несуществующим родителем даёт ошибку 23503"),
        ("Сделайте поле уникальным",
         "ALTER TABLE ... ADD UNIQUE (...); повторная вставка того же значения даёт ошибку 23505"),
    ],
    2: [
        ("Добавьте условие в один из запросов",
         "запрос выполняется, строк стало меньше, условие видно в тексте"),
        ("Покажите топ-5 вместо топ-3",
         "в рейтинге по 5 строк на группу (WHERE place <= 5 или LIMIT 5)"),
        ("Посчитайте среднее по группе",
         "avg(...) и GROUP BY; результат одной группы сверяется ручным подсчётом"),
    ],
    3: [
        ("Поменяйте текст ошибки триггера",
         "CREATE OR REPLACE FUNCTION ...; нарушение правила из demo/lab3.sql выдаёт новый текст"),
        ("Добавьте колонку в представление",
         "CREATE OR REPLACE VIEW, новая колонка в конце списка; SELECT показывает её"),
        ("Добавьте параметр в функцию",
         "новый параметр с DEFAULT; старый вызов из демо работает, новый вызов фильтрует"),
    ],
    4: [
        ("Поменяйте порядок шагов в race.sql и скажите результат до запуска",
         "студент говорит, что произойдёт; dbcheck run demo/race.sql подтверждает или нет"),
        ("Выдайте роли новое право (например, app_reader: SELECT ещё на одно представление)",
         "GRANT ...; SET ROLE app_reader; SELECT работает, лишние действия по-прежнему дают permission denied"),
        ("Добавьте SAVEPOINT в транзакцию",
         "ошибка после SAVEPOINT, ROLLBACK TO SAVEPOINT, после COMMIT первая часть сохранилась"),
    ],
    5: [
        ("Удалите индекс и покажите разницу",
         "BEGIN; DROP INDEX ...; EXPLAIN ANALYZE ...; ROLLBACK; в плане Seq Scan, время выросло"),
        ("Поменяйте порядок колонок в составном индексе",
         "CREATE INDEX с другим порядком в транзакции; план поменялся или индекс не используется, студент объясняет почему"),
        ("Добавьте условие в запрос и посмотрите на план",
         "EXPLAIN ANALYZE до и после: появился Filter или поменялся тип узла, студент объясняет"),
    ],
}

STATE = Path.home() / ".cache" / "dbcheck" / "asked.json"


def _load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if data.get("date") == date.today().isoformat() else {"date": date.today().isoformat()}


def _pick(pool: list, used: list[int]) -> tuple[int, object]:
    free = [i for i in range(len(pool)) if i not in used] or list(range(len(pool)))
    if len(free) == len(pool):
        used.clear()
    idx = random.choice(free)
    used.append(idx)
    return idx, pool[idx]


def ask_command(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="dbcheck ask", description="Вопрос из банка и живая правка для приёма лабы")
    p.add_argument("--lab", type=int, choices=range(1, 6), required=True, metavar="N")
    p.add_argument("--edit", action="store_true", help="сразу выдать и живую правку")
    p.add_argument("--reset", action="store_true", help="забыть, что уже спрашивали сегодня")
    p.add_argument("--list", action="store_true", help="показать весь банк лабы")
    args = p.parse_args(argv)

    if args.list:
        print(bold(f"Лаба {args.lab}: вопросы"))
        for n, q in enumerate(QUESTIONS[args.lab], 1):
            print(f"  {n}. {q}")
        print(bold("Живые правки"))
        for task, verify in EDITS[args.lab]:
            print(f"  • {task}\n    {dim(verify)}")
        return 0

    state = {"date": date.today().isoformat()} if args.reset else _load()
    used = state.setdefault(str(args.lab), {"q": [], "e": []})
    n, question = _pick(QUESTIONS[args.lab], used["q"])
    print(bold(f"Лаба {args.lab} · вопрос №{n + 1}: ") + question)
    print(dim("  2 балла за ответ"))
    if args.edit:
        _, (task, verify) = _pick(EDITS[args.lab], used["e"])
        print(yellow("Правка: ") + task)
        print(blue("  Как проверить: ") + verify)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return 0
