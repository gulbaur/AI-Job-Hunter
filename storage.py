"""SQLite vacancy storage. Each upsert commits independently of network collection."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import CheckError, html_to_text

DB_PATH = Path(__file__).resolve().parent / "data" / "jobs.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    source TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    region TEXT,
    region_id TEXT,
    url TEXT NOT NULL,
    description_html TEXT NOT NULL,
    description_text TEXT NOT NULL,
    experience_id TEXT,
    experience_name TEXT,
    salary_json TEXT,
    published_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    PRIMARY KEY (source, vacancy_id)
);
CREATE INDEX IF NOT EXISTS jobs_last_seen ON jobs (last_seen_at DESC);
"""


def _optional_object(vacancy: dict, field: str) -> dict:
    value = vacancy.get(field)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CheckError("HH API: некорректные дополнительные поля вакансии.")
    return value


class JobStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.executescript(SCHEMA)
            columns={r[1] for r in self.connection.execute('PRAGMA table_info(jobs)')}
            with self.connection:
                for name in ('status_reason','location_requirements_json'):
                    if name not in columns:self.connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
        except Exception:
            self.connection.close()
            raise

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *args: Any) -> None:
        self.connection.close()

    def save(self, vacancy: dict, *, source: str = "hh", seen_at: str | None = None) -> bool:
        """Return True for an insert; preserve first_seen_at and status on updates."""
        vacancy_id = str(vacancy.get("id", ""))
        if not (vacancy_id.isascii() and vacancy_id.isdigit()):
            raise CheckError("HH API: некорректный ID вакансии.")
        for field in ("name", "alternate_url", "description", "published_at"):
            if not isinstance(vacancy.get(field), str):
                raise CheckError("HH API: в полной вакансии отсутствуют обязательные поля.")
        employer = _optional_object(vacancy, "employer")
        area = _optional_object(vacancy, "area")
        experience = _optional_object(vacancy, "experience")
        salary = vacancy.get("salary") or vacancy.get("salary_range")
        locations=vacancy.get('applicant_locations')
        if locations is not None and (not isinstance(locations,list) or not all(isinstance(v,str) for v in locations)):
            raise CheckError('Некорректные требования к стране кандидата.')
        if salary is not None and not isinstance(salary, dict):
            raise CheckError("HH API: некорректные данные зарплаты.")
        timestamp = seen_at or datetime.now(timezone.utc).isoformat(timespec="microseconds")
        values = (
            source, vacancy_id, vacancy["name"], employer.get("name"), area.get("name"),
            str(area["id"]) if area.get("id") is not None else None,
            vacancy["alternate_url"], vacancy["description"],
            html_to_text(vacancy["description"]), experience.get("id"),
            experience.get("name"),
            json.dumps(salary, ensure_ascii=False) if salary is not None else None,
            vacancy["published_at"], timestamp, timestamp,
            json.dumps(locations,ensure_ascii=False) if locations is not None else None,
        )
        with self.connection:
            # Serialize the existence check and upsert so concurrent runs count correctly.
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT 1 FROM jobs WHERE source = ? AND vacancy_id = ?", (source, vacancy_id)
            ).fetchone()
            self.connection.execute("""
                INSERT INTO jobs (
                    source, vacancy_id, title, company, region, region_id, url,
                    description_html, description_text, experience_id, experience_name,
                    salary_json, published_at, first_seen_at, last_seen_at, location_requirements_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, vacancy_id) DO UPDATE SET
                    title = excluded.title,
                    company = excluded.company,
                    region = excluded.region,
                    region_id = excluded.region_id,
                    url = excluded.url,
                    description_html = excluded.description_html,
                    description_text = excluded.description_text,
                    experience_id = excluded.experience_id,
                    experience_name = excluded.experience_name,
                    salary_json = excluded.salary_json,
                    published_at = excluded.published_at,
                    last_seen_at = MAX(jobs.last_seen_at, excluded.last_seen_at),
                    location_requirements_json=COALESCE(excluded.location_requirements_json,jobs.location_requirements_json)
            """, values)
        return existing is None

    def latest(self, limit: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM jobs ORDER BY last_seen_at DESC, source, vacancy_id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def get(self, vacancy_id: str, source: str = "hh") -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM jobs WHERE source = ? AND vacancy_id = ?", (source, vacancy_id)
        ).fetchone()
