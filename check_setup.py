"""Read-only API checks and an in-memory SQLite check. Run with Python 3.13."""

import math
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from hh import CheckError, ENV_PATH, HHClient, TIMEOUT, html_to_text

ROOT = Path(__file__).resolve().parent
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"


def get_json(session: requests.Session, url: str, **kwargs: Any) -> Any:
    """Only GET; never include raw exceptions or API bodies in diagnostics."""
    try:
        # Disable redirects so credentials are sent only to the intended endpoint.
        with session.get(
            url, timeout=TIMEOUT, allow_redirects=False, **kwargs
        ) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise CheckError(f"HTTP {response.status_code}: ожидался HTTP 200.")
            try:
                return response.json()
            except ValueError:
                raise CheckError("API вернул некорректный JSON.") from None
    except requests.Timeout:
        raise CheckError("Истёк таймаут сетевого запроса.") from None
    except requests.ConnectionError:
        raise CheckError("Не удалось установить соединение с API.") from None
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        reason = {
            401: "авторизация отклонена; проверьте ключ, если он требуется",
            403: "доступ запрещён API",
            429: "превышен лимит запросов; повторите проверку позже",
        }.get(status, "API вернул ошибку")
        raise CheckError(f"HTTP {status}: {reason}.") from None
    except requests.RequestException:
        raise CheckError("Ошибка выполнения сетевого запроса.") from None


def description_excerpt(description: str, max_length: int = 350) -> str:
    text = " ".join(html_to_text(description).split())
    if not text:
        return "(описание пустое)"
    return text if len(text) <= max_length else text[:max_length].rstrip() + "…"


def check_hh() -> str:
    with HHClient() as client:
        area_id = client.kazakhstan_id()
        print(f"Казахстан: ID {area_id} (получен из /areas).")
        try:
            result = client.search("machine learning", area_id, page=0, per_page=3)
        except CheckError as exc:
            # Anonymous vacancy search is closed by HH (HTTP 403); the program itself reads HH through the browser.
            if client.has_token or not str(exc).startswith("HTTP 403"):
                raise
            print("Поиск через API HH без токена закрыт (HTTP 403). Программе он не нужен: "
                  "вакансии HH читаются через браузер после входа в hh_login.cmd.")
            return "SKIP"
        items = result["items"][:3]
        print(f"Получено вакансий: {len(items)}.")
        if not items:
            print("Запрос успешен, подходящих вакансий сейчас нет.")
            return "OK"
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise CheckError("HH API: некорректные данные вакансии.")
            employer = item.get("employer") or {}
            if not isinstance(employer, dict):
                raise CheckError("HH API: некорректные данные работодателя.")
            print(f"{index}. {item.get('name') or '(без названия)'}")
            print(f"   Компания: {employer.get('name') or '(не указана)'}")
            print(f"   Ссылка: {item.get('alternate_url') or '(не указана)'}")

        vacancy_id = str(items[0].get("id", ""))
        if not (vacancy_id.isascii() and vacancy_id.isdigit()):
            raise CheckError("HH API: у первой вакансии отсутствует корректный ID.")
        vacancy = client.vacancy(vacancy_id)
        print("Фрагмент полного описания первой вакансии:")
        print(description_excerpt(vacancy["description"]))
    return "OK"


def format_usd(value: Any) -> str:
    # Only numeric fields may reach output; never print an arbitrary API value.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CheckError("OpenRouter вернул некорректное значение лимита ключа.")
    if not math.isfinite(value):
        raise CheckError("OpenRouter вернул некорректное значение лимита ключа.")
    return f"{value:.4f} USD"


def check_openrouter() -> str:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("Ключ OPENROUTER_API_KEY не задан; проверка пропущена.")
        return "SKIP"
    # A separate session prevents the key from being included in HH API requests.
    with requests.Session() as session:
        session.headers.update({"Authorization": f"Bearer {key}"})
        result = get_json(session, OPENROUTER_KEY_URL)
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
        raise CheckError("OpenRouter: в ответе отсутствуют сведения о ключе.")
    data = result["data"]
    print("HTTP 200: ключ действителен.")
    if data.get("limit") is not None:
        print(f"Лимит ключа: {format_usd(data['limit'])}.")
    else:
        print("Лимит ключа не задан.")
    if data.get("limit_remaining") is not None:
        print(f"Доступный остаток лимита ключа: {format_usd(data['limit_remaining'])}.")
    elif data.get("limit") is not None:
        print("API не сообщил доступный остаток лимита ключа.")
    return "OK"


def check_sqlite() -> str:
    expected = "AI Job Hunter: тестовая строка"
    with closing(sqlite3.connect(":memory:")) as connection:
        with connection:
            connection.execute("CREATE TABLE setup_check (message TEXT NOT NULL)")
            connection.execute(
                "INSERT INTO setup_check (message) VALUES (?)", (expected,)
            )
        row = connection.execute("SELECT message FROM setup_check").fetchone()
        if row != (expected,):
            raise CheckError("Прочитанная строка не совпадает с записанной.")
    print("База :memory:: тестовая строка записана и прочитана успешно.")
    return "OK"


def check_profile(profile_dir: Path = ROOT / "profile") -> str:
    """The CVs and facts letters need; checked here so a new user learns it before any paid run."""
    import hunt
    hunt.check_profile(profile_dir)
    print("Профиль: CV прочитаны, факты для писем найдены.")
    return "OK"


def check_browser() -> str:
    """Playwright's Chromium is installed (HH search and sending use it)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            raise CheckError(r"Chromium не установлен: .venv\Scripts\python.exe -m playwright install chromium")
    print("Браузер Chromium для Playwright установлен.")
    return "OK"


CHECKS = (
    ("HH API", check_hh),
    ("OpenRouter", check_openrouter),
    ("SQLite", check_sqlite),
    ("Профиль", check_profile),
    ("Браузер", check_browser),
)


def main() -> int:
    # UTF-8 output also works when stdout is redirected on Windows.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    failed = False
    try:
        # Existing process settings take precedence; the .env file is never changed.
        load_dotenv(ENV_PATH, override=False, encoding="utf-8-sig", interpolate=False)
    except (OSError, UnicodeError, ValueError):
        print("[ОШИБКА] Не удалось прочитать .env; проверки продолжатся.")
        failed = True

    results: list[tuple[str, str]] = []
    for name, check in CHECKS:
        print(f"\n--- {name} ---", flush=True)
        try:
            status = check()
        except CheckError as exc:
            print(f"[ОШИБКА] {exc}")
            status = "FAIL"
        except sqlite3.Error:
            print("[ОШИБКА] Не удалось выполнить проверку SQLite.")
            status = "FAIL"
        except Exception:
            # Avoid tracebacks: third-party exception messages may contain secrets.
            print("[ОШИБКА] Непредвиденная ошибка проверки.")
            status = "FAIL"
        results.append((name, status))
        if status == "FAIL":
            failed = True

    labels = {"OK": "УСПЕХ", "SKIP": "ПРОПУЩЕНО", "FAIL": "ОШИБКА"}
    print("\nИтог:")
    for name, status in results:
        print(f"  {name}: {labels[status]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
