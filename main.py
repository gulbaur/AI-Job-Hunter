"""Personal Job Hunter CLI: collect HH vacancies and browse the local SQLite database."""

import argparse
import json
import sqlite3
import sys
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from common import CheckError
from storage import DB_PATH, JobStore

if TYPE_CHECKING:
    from hh import HHClient


@dataclass
class CollectionStats:
    found: int = 0
    added: int = 0
    existing: int = 0
    errors: int = 0
    partial: bool = False
    limit_reached: bool = False


def collect(store: JobStore, client: "HHClient", limit: int, stats: CollectionStats) -> None:
    from hh import HHError, SEARCH_QUERIES

    try:
        area_id = client.kazakhstan_id()
    except CheckError as exc:
        stats.errors += 1
        stats.partial = True
        print(f"[ОШИБКА] {exc}")
        return
    print(f"Казахстан: ID {area_id}. Период: последние 30 дней. Все уровни опыта.")
    seen: set[str] = set()
    # Round-robin pages give all search phrases a turn before fetching later pages.
    pending = deque((query, 0) for query in SEARCH_QUERIES)
    per_page = min(100, max(1, (limit + len(SEARCH_QUERIES) - 1) // len(SEARCH_QUERIES)))
    max_pages = 2000 // per_page
    while pending and len(seen) < limit:
        query, page = pending.popleft()
        print(f"Поиск: {query}; страница {page + 1}.", flush=True)
        try:
            result = client.search(query, area_id, page, per_page)
        except CheckError as exc:
            stats.errors += 1
            stats.partial = True
            print(f"[ОШИБКА] {exc}")
            if isinstance(exc, HHError) and exc.stop_collection:
                return
            continue

        for item in result["items"]:
            if len(seen) >= limit:
                break
            vacancy_id = str(item.get("id", "")) if isinstance(item, dict) else ""
            if not (vacancy_id.isascii() and vacancy_id.isdigit()):
                stats.errors += 1
                stats.partial = True
                print("[ОШИБКА] HH API: в поисковой выдаче некорректный ID вакансии.")
                continue
            if vacancy_id in seen:
                continue
            seen.add(vacancy_id)
            stats.found = len(seen)
            try:
                vacancy = client.vacancy(vacancy_id)
                added = store.save(vacancy)
            except CheckError as exc:
                stats.errors += 1
                stats.partial = True
                print(f"[ОШИБКА] Вакансия {vacancy_id}: {exc}")
                if isinstance(exc, HHError) and exc.stop_collection:
                    return
                continue
            except sqlite3.Error:
                stats.errors += 1
                stats.partial = True
                print("[ОШИБКА] Не удалось сохранить вакансию в SQLite; сбор остановлен.")
                return
            if added:
                stats.added += 1
            else:
                stats.existing += 1

        if result["items"] and page + 1 < result["pages"] and page + 1 < max_pages:
            pending.append((query, page + 1))
        elif result["items"] and page + 1 >= max_pages:
            found = result.get("found", 0)
            if result["pages"] > max_pages or (
                isinstance(found, int) and found > per_page * max_pages
            ):
                stats.errors += 1
                stats.partial = True
                print("[ОШИБКА] Достигнута глубина выдачи HH (2000 результатов на запрос).")
    stats.limit_reached = len(seen) >= limit


def print_summary(stats: CollectionStats) -> None:
    if stats.partial:
        print("\nЧАСТИЧНЫЙ СБОР: результаты, сохранённые до остановки или ошибок, оставлены в базе.")
    elif stats.limit_reached:
        print("\nСбор завершён: достигнут общий лимит уникальных вакансий.")
    else:
        print("\nСбор завершён: доступные страницы всех запросов обработаны.")
    print(f"Найдено уникальных: {stats.found}")
    print(f"Добавлено новых: {stats.added}")
    print(f"Уже было в базе (обновлено): {stats.existing}")
    print(f"Ошибок: {stats.errors}")


def run_collect(limit: int) -> int:
    from hh import HHClient, load_settings

    stats = CollectionStats()
    try:
        load_settings()
        with JobStore() as store, HHClient() as client:
            if not client.has_token:
                print("HH_ACCESS_TOKEN не задан: доступ без токена диагностический; "
                      "HH может потребовать авторизацию или капчу.")
            collect(store, client, limit, stats)
    except CheckError as exc:
        stats.errors += 1
        stats.partial = True
        print(f"[ОШИБКА] {exc}")
    except (sqlite3.Error, OSError):
        stats.errors += 1
        stats.partial = True
        print("[ОШИБКА] Не удалось открыть или записать локальную базу данных.")
    except KeyboardInterrupt:
        stats.partial = True
        print("\nСбор прерван пользователем.")
    except Exception:
        # No traceback or exception text: a library error can contain credentials.
        stats.errors += 1
        stats.partial = True
        print("[ОШИБКА] Непредвиденная ошибка; сбор остановлен.")
    print_summary(stats)
    return 1 if stats.partial else 0


def list_jobs(store: JobStore, limit: int) -> None:
    jobs = store.latest(limit)
    if not jobs:
        print("В базе пока нет вакансий.")
        return
    for job in jobs:
        print(f"{job['vacancy_id']} | {job['title']} | статус: {job['status']}")
        print(f"  Компания: {job['company'] or '(не указана)'}; "
              f"регион: {job['region'] or '(не указан)'}")
        print(f"  {job['url']}")


def show_job(store: JobStore, vacancy_id: str) -> int:
    job = store.get(vacancy_id)
    if job is None:
        print(f"Вакансия HH с ID {vacancy_id} не найдена в локальной базе.")
        return 1
    print(f"{job['vacancy_id']} | {job['title']}")
    print(f"Компания: {job['company'] or '(не указана)'}")
    print(f"Регион: {job['region'] or '(не указан)'}")
    print(f"Опыт: {job['experience_name'] or '(не указан)'}")
    print(f"Статус: {job['status']}; опубликовано: {job['published_at'] or '(не указано)'}")
    print(f"Впервые обнаружена: {job['first_seen_at']}")
    print(f"Последнее обнаружение: {job['last_seen_at']}")
    if job["salary_json"]:
        salary = json.loads(job["salary_json"])
        print(f"Зарплата: от {salary.get('from')} до {salary.get('to')} "
              f"{salary.get('currency') or ''}; gross={salary.get('gross')}")
    print(job["url"])
    print("\nПолное сохранённое описание:\n")
    print(job["description_text"] or "(описание пустое)")
    return 0


def positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Ожидалось положительное целое число.") from None
    if result <= 0:
        raise argparse.ArgumentTypeError("Лимит должен быть больше нуля.")
    return result


def hh_id(value: str) -> str:
    if not (value.isascii() and value.isdigit()):
        raise argparse.ArgumentTypeError("ID вакансии HH должен содержать только цифры.")
    return value


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Личный AI Job Hunter: HH и SQLite.")
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect", help="Необязательный источник: HH API за 30 дней")
    collect_parser.add_argument("--limit", type=positive_int, default=30)
    list_parser = commands.add_parser("list", help="Последние обнаруженные вакансии из SQLite")
    list_parser.add_argument("--limit", type=positive_int, default=20)
    show_parser = commands.add_parser("show", help="Полное описание из SQLite, без API")
    show_parser.add_argument("id", type=hh_id)
    args = parser.parse_args(argv)
    if args.command == "collect":
        return run_collect(args.limit)
    try:
        with JobStore() as store:
            if args.command == "list":
                list_jobs(store, args.limit)
                return 0
            return show_job(store, args.id)
    except (sqlite3.Error, OSError, ValueError):
        print("[ОШИБКА] Не удалось прочитать локальную базу данных.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
