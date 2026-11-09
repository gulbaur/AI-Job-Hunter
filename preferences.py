"""User-confirmed location preferences; separate from model recommendations."""

import json
from pathlib import Path
import re

from common import CheckError


def load_preferences(profile_dir: Path) -> dict | None:
    path = profile_dir / "preferences.json"
    if not path.exists():
        return None
    try:
        preferences = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CheckError("Не удалось прочитать настройки географии в profile/preferences.json.") from None
    required = {"office_city", "other_locations", "remote_from"}
    if (not isinstance(preferences, dict)
            or not required <= set(preferences)
            or set(preferences) - required - {"excluded_companies"}
            or preferences["other_locations"] != "remote_only"
            or not all(isinstance(preferences[key], str) and preferences[key].strip() for key in required)
            or not isinstance(preferences.get("excluded_companies", []), list)
            or not all(isinstance(value, str) and value.strip()
                       for value in preferences.get("excluded_companies", []))):
        raise CheckError("Некорректные настройки географии в profile/preferences.json.")
    return preferences


def company_is_excluded(job: dict, preferences: dict | None) -> bool:
    company = " ".join(str(job.get("company") or "").casefold().split())
    return bool(preferences and company in {
        " ".join(value.casefold().split()) for value in preferences.get("excluded_companies", [])
    })


def location_fit(job: dict, preferences: dict) -> tuple[str, str]:
    city = preferences["office_city"]
    aliases = [city] if city.casefold() != "алматы" else ["Алматы", "Almaty", "Алма-Ата"]
    region = str(job.get("region") or "")
    if any(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", region, re.I) for alias in aliases):
        return "allowed", f"Офис или удалённая работа в {city} подходят по географии."
    text = str(job.get("description_text") or "")
    remote_denied = re.search(
        r"удал[её]н\w*[^\n.]{0,70}(?:\bнет\b|не\s+(?:предусмотр|рассматрива|возмож))|"
        r"\b(?:not|no)\s+(?:a\s+)?(?:fully\s+)?remote\b|"
        r"\bremote\s+(?:work\s+|working\s+|format\s+)?(?:is\s+)?(?:not\s+(?:available|allowed|offered|permitted)|unavailable)", text, re.I)
    remote_offered = re.search(
        r"удал[её]нн\w*\s+(?:работ\w*|формат)|формат[^\n.]{0,30}удал[её]н\w*|\bremote\b", text, re.I)
    onsite = re.search(
        r"формат\s+работы:\s*офис|работ\w*\s+(?:полностью\s+)?(?:из|в)\s+офис|"
        r"в\s+комфортном\s+офис|(?:только|строго)\s+офф?лайн\s+формат|"
        r"на\s+месте\s+работ[оа]дателя|\bon[- ]?site\b|\boffice\s+(?:work\s+)?only\b|"
        r"\bmust\s+(?:work|be)\s+(?:in|at)\s+(?:the\s+)?office\b", text, re.I)
    if remote_denied or onsite:
        return "blocked", f"За пределами {city} подходит только удалёнка; в описании указан офис или отказ в удалённом формате."
    if remote_offered:
        return "remote_review", f"Удалённый формат указан; доступность работы из {preferences['remote_from']} нужно проверить."
    return "unconfirmed", f"Локация: {region or 'не указана'}. Удалённый формат вне {city} не подтверждён; оценка отложена."
