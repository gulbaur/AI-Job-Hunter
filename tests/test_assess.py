"""Offline tests for evidence grounding, paid-call controls and cache invalidation."""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import requests

import assess
from common import CheckError
from model_stub import setUpModule, tearDownModule  # noqa: F401
from storage import JobStore

PROFILES = {"ml_engineer": "Evaluated image classification models with PyTorch.",
            "applied_ai": "Developed Python endpoints with FastAPI and RAG retrieval."}
RESULT = {
    "decision": "stretch", "cv_variant": "applied_ai", "summary": "Есть релевантный опыт; стаж требует уточнения.",
    "matches": [{"requirement_quote": "Python and FastAPI. 2 years of experience.",
                 "cv_quote": PROFILES["applied_ai"], "reason": "Подтверждены Python и FastAPI, но не стаж.",
                 "coverage": "partial"}],
    "gaps": [{"requirement_quote": "2 years of experience", "reason": "В CV такой стаж не подтверждён."}],
    "questions": ["Гибкое ли требование к стажу?"],
}
WIRE_RESULT = {**RESULT,
    "matches": [{"requirement_id": "J001", "cv_id": "AP001",
                 "reason": RESULT["matches"][0]["reason"], "coverage": "partial"}],
    "gaps": [{"requirement_id": "J001", "reason": RESULT["gaps"][0]["reason"]}],
}


def vacancy():
    return {"id": "1", "name": "AI Engineer", "employer": {"name": "Test company"},
            "area": {"name": "Almaty"}, "published_at": "2026-10-20",
            "alternate_url": "https://hh.kz/vacancy/1",
            "description": "<p>Python and FastAPI. 2 years of experience.</p>"}


def model_response(result=None, *, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps({
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result or WIRE_RESULT)}}],
        "usage": {"cost": 0.0001, "prompt_tokens": 500, "completion_tokens": 100},
    }).encode("utf-8")
    response._content_consumed = True
    return response


class AssessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name) / "jobs.sqlite")
        self.addCleanup(self.store.connection.close)
        self.store.save(vacancy(), seen_at="2026-10-20T10:00:00Z")
        self.job = dict(self.store.get("1"))
        self.output = io.StringIO()
        self.session = Mock()
        self.session.post.side_effect = lambda *args, **kwargs: model_response()

    def run_assess(self, *, run, profiles=None, budget=0.02, **kwargs):
        with contextlib.redirect_stdout(self.output):
            return assess.assess_jobs(self.store, profiles or PROFILES, assess.DEFAULT_MODEL,
                                      limit=3, run=run, budget=budget,
                                      key="synthetic-key", session=self.session, **kwargs)

    def test_preview_never_calls_model_or_persists_assessment(self):
        self.assertEqual(self.run_assess(run=False), 0)
        self.session.post.assert_not_called()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0], 0)
        self.assertIn("ПРЕДПРОСМОТР", self.output.getvalue())

    def test_cache_avoids_paid_repeat_and_keeps_user_status(self):
        self.store.connection.execute("UPDATE jobs SET status='viewed'")
        self.store.connection.commit()
        self.assertEqual(self.run_assess(run=True), 0)
        self.assertEqual(self.run_assess(run=True), 0)
        self.assertEqual(self.session.post.call_count, 1)
        saved = self.store.get("1")
        self.assertEqual(saved["status"], "viewed")
        self.assertEqual(saved["first_seen_at"], "2026-10-20T10:00:00Z")

    def test_candidate_writes_the_search_goal_used_by_every_prompt(self):
        import letter_rewrite
        import personalized
        path = Path(self.temp.name) / "candidate.md"
        path.write_text("<!-- hint for the user -->\nЦель: Data Analyst, Караганда.", encoding="utf-8")
        with patch.object(assess, "CANDIDATE_FILE", path):
            system = assess.build_payload(self.job, PROFILES, "m", assessment_date="2026-10-20")["messages"][0]["content"]
            self.assertIn("Цель: Data Analyst, Караганда.", system)
            self.assertNotIn("hint for the user", system)
            for prompt in (personalized.SYSTEM_PROMPT, letter_rewrite.SYSTEM_PROMPT):
                self.assertIn("Цель: Data Analyst, Караганда.", assess.with_candidate(prompt))
        # The public prompts name no particular candidate.
        for prompt in (assess.SYSTEM_PROMPT, personalized.SYSTEM_PROMPT, personalized.AUDIT_PROMPT, letter_rewrite.SYSTEM_PROMPT):
            self.assertNotRegex(prompt, r"Алмат|Strong Junior|женщин|кандидатк")

    def test_profile_job_and_model_changes_invalidate_cache(self):
        self.run_assess(run=True)
        changed = {**PROFILES, "applied_ai": PROFILES["applied_ai"] + " New project."}
        self.run_assess(run=True, profiles=changed)
        self.assertEqual(self.session.post.call_count, 2)
        payload = assess.build_payload(self.job, PROFILES, assess.DEFAULT_MODEL, assessment_date="2026-10-20")
        original = assess.cache_key(payload)
        payload["model"] = "a-different-model"
        self.assertNotEqual(assess.cache_key(payload), original)
        payload = assess.build_payload({**self.job, "description_text": "Changed vacancy"}, PROFILES,
                                       assess.DEFAULT_MODEL, assessment_date="2026-10-20")
        self.assertNotEqual(assess.cache_key(payload), original)

    def test_monthly_experience_refresh_does_not_charge_daily(self):
        def key(day):
            return assess.cache_key(assess.build_payload(self.job, PROFILES, assess.DEFAULT_MODEL, assessment_date=day))
        self.assertEqual(key("2026-10-20"), key("2026-10-21"))
        self.assertNotEqual(key("2026-10-20"), key("2026-11-01"))

    def test_invented_cv_or_requirement_quote_is_rejected(self):
        for field, text in (("cv_quote", "Managed a team of 20 engineers"),
                            ("requirement_quote", "Required German language")):
            invented = copy.deepcopy(RESULT)
            invented["matches"][0][field] = text
            with self.subTest(field=field), self.assertRaises(CheckError):
                assess.validate_result(invented, self.job, PROFILES)
        invented = copy.deepcopy(RESULT)
        invented["cv_variant"] = "ml_engineer"
        with self.assertRaises(CheckError):
            assess.validate_result(invented, self.job, PROFILES)

    def test_budget_blocks_request_before_network(self):
        with self.assertRaises(CheckError):
            self.run_assess(run=True, budget=0.00000001)
        self.session.post.assert_not_called()

    def test_failed_model_output_is_not_cached_and_never_retried(self):
        invalid = copy.deepcopy(WIRE_RESULT)
        invalid["matches"][0]["cv_id"] = "AP999"
        self.session.post.side_effect = lambda *args, **kwargs: model_response(invalid)
        self.assertEqual(self.run_assess(run=True), 1)
        self.assertEqual(self.run_assess(run=True), 1)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0], 0)
        rejected = self.store.connection.execute("SELECT usage_json FROM rejected_assessments").fetchone()
        self.assertEqual(json.loads(rejected[0])["cost"], 0.0001)
        self.assertIn("0.000100", self.output.getvalue())

    def test_rejection_does_not_block_other_jobs_and_explicit_retry_can_recover(self):
        second = vacancy()
        second["id"] = "2"
        self.store.save(second, seen_at="2026-10-20T10:00:00Z")
        invalid = copy.deepcopy(WIRE_RESULT)
        invalid["gaps"][0]["requirement_id"] = "J999"
        self.session.post.side_effect = [model_response(invalid), model_response()]
        self.assertEqual(self.run_assess(run=True), 1)
        self.assertEqual(self.session.post.call_count, 2)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0], 1)
        self.session.post.side_effect = lambda *args, **kwargs: model_response()
        self.assertEqual(self.run_assess(run=True, retry_rejected=True), 0)
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM rejected_assessments").fetchone()[0], 0)

    def test_single_vacancy_filter_does_not_call_model_for_other_jobs(self):
        second = vacancy()
        second["id"] = "2"
        self.store.save(second)
        self.assertEqual(self.run_assess(run=True, vacancy_id="1"), 0)
        self.assertEqual(self.session.post.call_count, 1)
        saved = self.store.connection.execute("SELECT vacancy_id FROM assessments").fetchone()
        self.assertEqual(saved[0], "1")

    def test_empty_last_collection_cannot_fall_back_to_old_jobs(self):
        self.assertEqual(self.run_assess(run=True, vacancy_ids=[]), 0)
        self.session.post.assert_not_called()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0], 0)

    def test_collection_ids_exclude_other_saved_jobs(self):
        second = vacancy()
        second["id"] = "2"
        self.store.save(second)
        self.assertEqual(self.run_assess(run=True, vacancy_ids=["1"]), 0)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.store.connection.execute("SELECT vacancy_id FROM assessments").fetchone()[0], "1")

    def test_empty_collection_cli_does_not_read_cv_or_call_network(self):
        manifest = Path(self.temp.name) / "collection.json"
        manifest.write_text(json.dumps({"source": "hh", "vacancy_ids": [], "complete": True}), encoding="utf-8")
        with patch.object(assess, "RUN_MANIFEST", manifest), \
             patch.object(assess, "load_profiles") as load, \
             patch.object(assess.requests, "Session") as session, \
             contextlib.redirect_stdout(self.output):
            self.assertEqual(assess.main(["--last-collection", "--run"]), 0)
        load.assert_not_called()
        session.assert_not_called()

    def test_invalid_collection_cli_cannot_fall_back_to_saved_jobs(self):
        manifest = Path(self.temp.name) / "collection.json"
        manifest.write_text("[]", encoding="utf-8")
        with patch.object(assess, "RUN_MANIFEST", manifest), \
             patch.object(assess.requests, "Session") as session, \
             contextlib.redirect_stdout(self.output):
            self.assertEqual(assess.main(["--last-collection", "--run"]), 1)
        session.assert_not_called()

    def test_http_error_is_sanitized_and_credentials_stay_on_openrouter(self):
        self.session.post.side_effect = lambda *args, **kwargs: model_response(status=401)
        with self.assertRaisesRegex(CheckError, "HTTP 401") as error:
            self.run_assess(run=True)
        self.assertNotIn("synthetic-key", str(error.exception))
        args, kwargs = self.session.post.call_args
        self.assertEqual(args[0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("synthetic-key", json.dumps(kwargs["json"]))

    def test_ids_resolve_to_source_and_cannot_cross_cv_variants(self):
        resolved = assess.resolve_evidence(WIRE_RESULT, self.job, PROFILES)
        self.assertEqual(resolved["matches"][0]["cv_quote"], PROFILES["applied_ai"])
        self.assertEqual(resolved["matches"][0]["coverage"], "partial")
        other_cv = copy.deepcopy(WIRE_RESULT)
        other_cv["matches"][0]["cv_id"] = "ML001"
        with self.assertRaisesRegex(CheckError, "другого CV"):
            assess.resolve_evidence(other_cv, self.job, PROFILES)

    def test_irrelevant_roles_are_not_sent_and_user_status_is_preserved(self):
        self.store.connection.execute("UPDATE jobs SET title='Fraud Analyst / Антифрод-менеджер', status='viewed'")
        self.store.connection.commit()
        self.assertEqual(self.run_assess(run=True), 0)
        self.session.post.assert_not_called()
        self.assertEqual(self.store.get("1")["status"], "viewed")

    def test_long_source_lines_remain_exact_spans(self):
        text = "A document with meaningful words. " * 60
        catalog = assess.fragments(text, "J")
        self.assertGreater(len(catalog), 1)
        for span in catalog.values():
            self.assertLessEqual(len(span), 480)
            self.assertIn(span, assess.normalize(text))

    def test_partial_matches_and_senior_experience_cannot_produce_apply(self):
        optimistic = {**copy.deepcopy(RESULT), "decision": "apply"}
        guarded = assess.apply_safeguards(optimistic, self.job)
        self.assertEqual(guarded["decision"], "stretch")
        optimistic["matches"][0]["coverage"] = "full"
        job = {**self.job, "experience_name": "3–6 лет", "description_text": "От 3 лет Python в проде."}
        guarded = assess.apply_safeguards(optimistic, job)
        self.assertEqual(guarded["decision"], "stretch")
        self.assertEqual(guarded["gaps"][0]["requirement_quote"], "От 3 лет Python в проде.")
        job = {**self.job, "description_text": "2+ года коммерческой разработки на Python."}
        guarded = assess.apply_safeguards(optimistic, job)
        self.assertEqual(guarded["decision"], "stretch")
        self.assertEqual(guarded["gaps"][0]["requirement_quote"], job["description_text"])

    def test_astana_onsite_is_not_mistaken_for_almaty_fit(self):
        optimistic = {**copy.deepcopy(RESULT), "decision": "apply", "summary": "Алматы соответствует локации."}
        optimistic["matches"][0]["coverage"] = "full"
        for text in ["Только оффлайн формат работы (строго закрытый контур)",
                     "График работы 5/2 с 9:00-18:00 в комфортном офисе;"]:
            with self.subTest(text=text):
                job = {**self.job, "region": "Астана, Достык 8", "description_text": text}
                guarded = assess.apply_safeguards(optimistic, job)
                self.assertEqual(guarded["decision"], "stretch")
                self.assertIn("офис в Астане", guarded["summary"])
                # Audit A28: the checks come first, but the model's explanation is kept, not thrown away.
                self.assertTrue(guarded["summary"].startswith("Указан офис в Астане"))
                self.assertIn("Алматы соответствует локации.", guarded["summary"])
                self.assertEqual(guarded["gaps"][0]["requirement_quote"], text)

    def test_safeguards_are_idempotent_and_never_exceed_the_summary_limit(self):
        # Senior review 13.11 (my A28 change): flags were prepended again on every re-read and a long model
        # summary plus flags failed the 700-character check, rejecting a paid assessment.
        optimistic = {**copy.deepcopy(RESULT), "decision": "apply", "summary": "Хорошее совпадение. " * 30}
        job = {**self.job, "description_text": "Опыт работы от 2 лет с Python."}
        once = assess.apply_safeguards(optimistic, job)
        twice = assess.apply_safeguards(once, job)
        self.assertEqual(once["summary"], twice["summary"])
        self.assertEqual(once["summary"].count("Оценка модели:"), 1)
        self.assertLessEqual(len(once["summary"]), 700)
        old_flags_only = {**once, "summary": "Требование стажа выше целевого Junior/Strong Junior; такой стаж нужно подтвердить отдельно по CV."}
        self.assertNotIn("Оценка модели", assess.apply_safeguards(old_flags_only, job)["summary"])

    def test_english_minimum_and_russian_year_range_are_caught(self):
        optimistic = {**copy.deepcopy(RESULT), "decision": "apply"}
        optimistic["matches"][0]["coverage"] = "full"
        for text in ["No less than 3 years of professional experience.", "Опыт работы от 2-3 лет+", "At least 3 years of Python"]:
            with self.subTest(text=text):
                result = assess.apply_safeguards(optimistic, {**self.job, "description_text": text})
                self.assertEqual(result["decision"], "stretch")
                self.assertEqual(result["gaps"][0]["requirement_quote"], text)

    def test_unconfirmed_remote_outside_almaty_never_uses_paid_or_cached_result(self):
        self.store.connection.execute("UPDATE jobs SET region='Астана'")
        self.store.connection.commit()
        preferences = {"office_city": "Алматы", "other_locations": "remote_only", "remote_from": "Казахстан"}
        self.assertEqual(self.run_assess(run=True, preferences=preferences), 0)
        self.session.post.assert_not_called()
        self.assertIn("Отложено по географии", self.output.getvalue())

    def test_docx_extract_omits_identity_and_reads_table_text(self):
        path = Path(self.temp.name) / "cv.docx"
        text = "Developed FastAPI endpoints and RAG retrieval. " * 6
        xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
               '<w:p><w:r><w:t>Private Name private@example.com</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>Work Experience</w:t></w:r></w:p>'
               '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
               '</w:body></w:document>')
        with ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)
        extracted = assess.read_cv(path)
        self.assertNotIn("Private", extracted)
        self.assertNotIn("example.com", extracted)
        self.assertIn("FastAPI endpoints", extracted)


if __name__ == "__main__":
    unittest.main()
