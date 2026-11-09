"""Draft provenance, paid-call boundaries, edit preservation and location policy."""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import requests

import prepare
from common import CheckError


PROFILES = {
    "ml_engineer": "Оценивала качество моделей классификации изображений с учётом погрешностей ручной разметки.\n"
                   "Участвовала в развёртывании инференса предобученных моделей PyTorch в сервис.",
    "applied_ai": "Дорабатывала RAG-поиск по документации с индексами SQLite и кэшем эмбеддингов.\n"
                  "Разрабатывала FastAPI-эндпоинты для сервиса заявок и JSON-схемы.",
}
JOB = {"source": "hh", "vacancy_id": "10", "title": "Data Scientist", "company": "Example",
       "region": "Алматы", "experience_name": "1–3 года", "description_text": "Обучать и оценивать модели.",
       "url": "https://hh.kz/vacancy/10", "status": "new"}
ASSESSMENT = {"decision": "stretch", "summary": "Нужна проверка опыта табличного ML.",
              "gaps": [{"reason": "Табличные бустинги в CV не подтверждены."}]}
SELECTION = {"cv_variant": "ml_engineer", "fact_ids": ["ML001", "ML002"]}


def response(selection=None):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(SELECTION if selection is None else selection)}}],
        "usage": {"cost": 0.0001, "prompt_tokens": 500, "completion_tokens": 30}}).encode("utf-8")
    result._content_consumed = True
    return result


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = Mock()
        self.session.post.side_effect = lambda *args, **kwargs: response()
        self.output = io.StringIO()

    def run_prepare(self, *, run=True, profiles=None, job=None, **kwargs):
        with contextlib.redirect_stdout(self.output):
            return prepare.prepare(job or JOB, profiles or PROFILES, ASSESSMENT, "test-model",
                                   profile_dir=self.root / "profile", output_dir=self.root / "drafts",
                                   run=run, budget=kwargs.pop("budget", 0.01), key="synthetic-key",
                                   session=self.session, **kwargs)

    def test_preview_never_calls_model_or_writes_letter(self):
        self.assertIsNone(self.run_prepare(run=False))
        self.session.post.assert_not_called()
        self.assertFalse((self.root / "drafts").exists())

    def test_fact_only_letter_cache_and_user_edits_are_preserved(self):
        folder = self.run_prepare()
        letter = (folder / "letter.txt").read_text(encoding="utf-8")
        for paragraph in PROFILES["ml_engineer"].splitlines():
            self.assertIn(paragraph, letter)
        self.assertNotIn("CatBoost", letter)
        self.assertNotIn("Дорабатывала RAG", letter)
        (folder / "letter.txt").write_text("Редакция пользователя", encoding="utf-8")
        second = self.run_prepare()
        self.assertEqual(folder, second)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual((folder / "letter.txt").read_text(encoding="utf-8"), "Редакция пользователя")
        args, kwargs = self.session.post.call_args
        self.assertEqual(args[0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("synthetic-key", json.dumps(kwargs["json"]))

    def test_invented_free_text_is_rejected_and_not_paid_again(self):
        invalid = {**SELECTION, "letter": "У меня 10 лет опыта CatBoost в банке"}
        self.session.post.side_effect = lambda *args, **kwargs: response(invalid)
        with self.assertRaises(CheckError):
            self.run_prepare()
        with self.assertRaises(CheckError):
            self.run_prepare()
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(list((self.root / "drafts").rglob("letter.txt")), [])
        cached = json.loads(next((self.root / "drafts").rglob("selection.json")).read_text(encoding="utf-8"))
        self.assertEqual(cached["status"], "rejected")
        self.assertEqual(cached["usage"]["cost"], 0.0001)

    def test_cross_cv_unknown_and_duplicate_facts_are_rejected(self):
        catalogs = prepare.fact_catalog(PROFILES)
        for ids in [["ML001", "AI001"], ["ML001", "ML999"], ["ML001", "ML001"]]:
            with self.subTest(ids=ids), self.assertRaises(CheckError):
                prepare.validate_selection({"cv_variant": "ml_engineer", "fact_ids": ids}, catalogs)

    def test_location_policy_blocks_paid_request_and_artifacts(self):
        preferences = {"office_city": "Алматы", "other_locations": "remote_only", "remote_from": "Казахстан"}
        for text in ["Только оффлайн формат работы", "Формат не указан"]:
            with self.subTest(text=text), self.assertRaises(CheckError):
                self.run_prepare(job={**JOB, "region": "Астана", "description_text": text}, preferences=preferences)
        self.session.post.assert_not_called()
        self.assertFalse((self.root / "drafts").exists())

    def test_budget_is_checked_before_network(self):
        with self.assertRaises(CheckError):
            self.run_prepare(budget=0.00000001)
        self.session.post.assert_not_called()

    def test_relevant_source_changes_create_new_draft_without_overwriting_previous(self):
        first = self.run_prepare()
        changed = copy.deepcopy(PROFILES)
        changed["ml_engineer"] = changed["ml_engineer"].replace("предобученных моделей", "предобученных моделей классификации изображений")
        second = self.run_prepare(profiles=changed)
        self.assertNotEqual(first, second)
        self.assertEqual(self.session.post.call_count, 2)
        self.assertTrue((first / "letter.txt").is_file())

    def test_unused_paragraph_does_not_trigger_paid_repeat(self):
        first = self.run_prepare()
        changed = copy.deepcopy(PROFILES)
        changed["ml_engineer"] += "\nДорабатывала RAG-поиск по документации с индексами SQLite и кэшем эмбеддингов."
        second = self.run_prepare(profiles=changed)
        self.assertEqual(first, second)
        self.assertEqual(self.session.post.call_count, 1)

    def test_user_status_prevents_new_draft(self):
        for status in ["applied", "skipped"]:
            with self.subTest(status=status), self.assertRaises(CheckError):
                self.run_prepare(job={**JOB, "status": status})
        self.session.post.assert_not_called()

    def test_role_routing_respects_explicit_override(self):
        self.assertEqual(prepare.route_cv("Data Scientist", None), "ml_engineer")
        self.assertEqual(prepare.route_cv("Computer Vision Engineer", None), "ml_engineer")
        self.assertEqual(prepare.route_cv("AI Engineer (LLM, RAG)", None), "applied_ai")
        self.assertEqual(prepare.route_cv("Data Scientist", "applied_ai"), "applied_ai")
        self.assertIsNone(prepare.route_cv("Data Scientist", "auto"))

    def test_data_scientist_facts_prioritize_models_over_generic_rag(self):
        profiles = {"ml_engineer": PROFILES["ml_engineer"] +
                    "\nДорабатывала RAG-поиск по документации с индексами SQLite и кэшем эмбеддингов."}
        catalogs = prepare.relevant_facts(JOB, prepare.fact_catalog(profiles))
        self.assertEqual(len(catalogs["ml_engineer"]), 2)
        self.assertFalse(any("RAG" in fact for fact in catalogs["ml_engineer"].values()))


if __name__ == "__main__":
    unittest.main()
