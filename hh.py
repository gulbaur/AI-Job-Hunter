"""Shared HH API access, settings and description parsing. Only GET requests."""

import math
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().with_name(".env")
HH_API = "https://api.hh.ru"
DEFAULT_USER_AGENT = "PersonalJobHunter/0.1"
TIMEOUT = (10, 30)
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 10
SEARCH_QUERIES = (
    "machine learning", "ML Engineer", "AI Engineer", "LLM",
    "Computer Vision", "машинное обучение",
)
AUTH_HELP = (
    "Для штатной авторизации зарегистрируйте приложение на https://dev.hh.ru, "
    "получите access_token через OAuth 2.0 HH и укажите HH_ACCESS_TOKEN в .env. "
    "Если токен уже задан, проверьте срок его действия и права приложения. "
    "При капче пройдите проверку в HH вручную и повторите запуск. "
    "Документация: https://github.com/hhru/api/blob/master/docs/authorization.md"
)


class CheckError(Exception):
    """A diagnostic safe to print: no server bodies, secrets or raw exceptions."""


class HHError(CheckError):
    def __init__(self, message: str, *, stop_collection: bool = False) -> None:
        super().__init__(message)
        self.stop_collection = stop_collection


def load_settings() -> None:
    try:
        load_dotenv(ENV_PATH, override=False, encoding="utf-8-sig", interpolate=False)
    except (OSError, UnicodeError, ValueError):
        raise CheckError("Не удалось прочитать .env; содержимое файла не выводится.") from None


def find_kazakhstan_id(areas: Any) -> str:
    if not isinstance(areas, list):
        raise CheckError("HH API: ожидался список регионов.")
    pending = list(areas)
    while pending:
        area = pending.pop()
        if not isinstance(area, dict):
            raise CheckError("HH API: некорректный элемент списка регионов.")
        if str(area.get("name", "")).strip().casefold() in {
            "казахстан", "kazakhstan", "қазақстан"
        }:
            area_id = str(area.get("id", ""))
            if area_id.isascii() and area_id.isdigit():
                return area_id
        children = area.get("areas", [])
        if not isinstance(children, list):
            raise CheckError("HH API: некорректный список дочерних регионов.")
        pending.extend(children)
    raise CheckError("Казахстан не найден в справочнике регионов HH API.")


class DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in {
            "p", "br", "li", "div", "h1", "h2", "h3", "tr", "ul", "ol"
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {
            "p", "li", "div", "h1", "h2", "h3", "tr", "ul", "ol"
        }:
            self.parts.append("\n")


def html_to_text(description: str) -> str:
    parser = DescriptionParser()
    parser.feed(description)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _error_codes(payload: Any) -> set[str]:
    """Inspect machine-readable error codes, never echo arbitrary server text."""
    if not isinstance(payload, dict):
        return set()
    codes = {
        value for key in ("type", "value", "error")
        if isinstance((value := payload.get(key)), str)
    }
    for key in ("errors", "error"):
        nested = payload.get(key)
        if isinstance(nested, list):
            for item in nested:
                codes.update(_error_codes(item))
        elif isinstance(nested, dict):
            codes.update(_error_codes(nested))
    return codes


def _retry_delay(header: str | None, attempt: int) -> float:
    delay = float(2 ** attempt)
    if header:
        try:
            delay = float(header)
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    if not math.isfinite(delay):
        delay = float(2 ** attempt)
    return min(MAX_RETRY_DELAY, max(1.0, delay))


class HHClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": os.getenv("HH_USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
        })
        self.has_token = bool(os.getenv("HH_ACCESS_TOKEN", "").strip())
        if self.has_token:
            self.session.headers["Authorization"] = (
                "Bearer " + os.environ["HH_ACCESS_TOKEN"].strip()
            )

    def __enter__(self) -> "HHClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.session.close()

    def _get_json(self, path: str, **kwargs: Any) -> Any:
        # Callers supply only fixed paths or a validated numeric vacancy ID.
        # Reject external URLs and redirects; the HH token stays on api.hh.ru.
        if not path.startswith("/") or path.startswith("//"):
            raise HHError("Недопустимый путь HH API.", stop_collection=True)
        for attempt in range(MAX_ATTEMPTS):
            try:
                with self.session.get(
                    HH_API + path, timeout=TIMEOUT, allow_redirects=False, **kwargs
                ) as response:
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
            except (requests.Timeout, requests.ConnectionError):
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(_retry_delay(None, attempt))
                    continue
                raise HHError(
                    f"Ошибка соединения или таймаут HH API после {MAX_ATTEMPTS} попыток.",
                    stop_collection=True,
                ) from None
            except requests.RequestException:
                raise HHError(
                    "Не удалось выполнить запрос HH API; проверьте сетевые настройки и User-Agent.",
                    stop_collection=True,
                ) from None

            codes = _error_codes(payload)
            if "captcha_required" in codes:
                raise HHError(
                    f"HTTP {status}: HH требует капчу. {AUTH_HELP}", stop_collection=True
                )
            if status in {401, 403} or codes & {
                "bad_authorization", "invalid_token", "bad_access_token",
                "token_expired", "invalid_client", "oauth",
            }:
                raise HHError(
                    f"HTTP {status}: HH отклонил авторизацию или доступ. {AUTH_HELP}",
                    stop_collection=True,
                )
            if status == 429 or 500 <= status <= 599:
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(_retry_delay(retry_after, attempt))
                    continue
                raise HHError(
                    f"HTTP {status}: HH API недоступен после {MAX_ATTEMPTS} попыток; "
                    "повторите сбор позже.", stop_collection=True,
                )
            if status != 200:
                raise HHError(f"HTTP {status}: запрос HH API не выполнен.")
            if payload is None:
                raise HHError("HH API вернул некорректный JSON.")
            return payload
        raise HHError("Не удалось выполнить запрос HH API.", stop_collection=True)

    def kazakhstan_id(self) -> str:
        return find_kazakhstan_id(self._get_json("/areas"))

    def search(self, query: str, area_id: str, page: int, per_page: int) -> dict:
        result = self._get_json("/vacancies", params={
            "text": query, "area": area_id, "host": "hh.kz", "period": 30,
            "page": page, "per_page": per_page, "order_by": "publication_time",
        })
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise HHError("HH API: в ответе отсутствует список вакансий.")
        pages = result.get("pages")
        if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
            raise HHError("HH API: некорректные данные пагинации.")
        return result

    def vacancy(self, vacancy_id: str) -> dict:
        if not (vacancy_id.isascii() and vacancy_id.isdigit()):
            raise HHError("HH API: некорректный ID вакансии.")
        result = self._get_json(f"/vacancies/{vacancy_id}", params={"host": "hh.kz"})
        if not isinstance(result, dict) or str(result.get("id", "")) != vacancy_id:
            raise HHError("HH API: ID полной вакансии не совпадает с запрошенным.")
        if not isinstance(result.get("description"), str):
            raise HHError("HH API: в полной вакансии отсутствует описание.")
        return result
