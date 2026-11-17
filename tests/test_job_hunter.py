"""Offline integration tests: real temporary SQLite databases, mocked HTTP only."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import check_setup
import hh
import main as cli
from storage import JobStore

HH_SECRET = "synthetic-hh-secret"
OR_SECRET = "synthetic-openrouter-secret"
AREAS = [{"id": "1", "name": "root", "areas": [
    {"id": "987", "name": "Казахстан", "areas": []}
]}]


def response(payload=None, status=200, *, headers=None, raw=None):
    result = requests.Response()
    result.status_code = status
    result.url = hh.HH_API + "/vacancies"
    result.headers.update(headers or {})
    result._content = (raw if raw is not None else json.dumps(payload)).encode("utf-8")
    result._content_consumed = True
    return result


def vacancy(vacancy_id="1"):
    return {
        "id": vacancy_id,
        "name": f"ML Engineer {vacancy_id}",
        "employer": {"name": "Test Company"},
        "area": {"id": "160", "name": "Алматы"},
        "alternate_url": f"https://hh.kz/vacancy/{vacancy_id}",
        "description": "<p>ML &amp; AI</p><ul><li>Python</li><li>LLM</li></ul>",
        "experience": {"id": "moreThan6", "name": "Более 6 лет"},
        "salary": {"from": 500000, "to": None, "currency": "KZT", "gross": True},
        "published_at": "2026-10-19T10:00:00+0500",
    }


class JobHunterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "data" / "jobs.sqlite"
        env = patch.dict(os.environ, {
            "HH_ACCESS_TOKEN": HH_SECRET,
            "OPENROUTER_API_KEY": OR_SECRET,
            "HH_USER_AGENT": "PersonalJobHunter/0.1",
        })
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.output = io.StringIO()

    def assert_safe_output(self):
        output = self.output.getvalue()
        self.assertNotIn(HH_SECRET, output)
        self.assertNotIn(OR_SECRET, output)
        self.assertNotIn("Authorization", output)
        self.assertNotIn("Traceback", output)

    def transport(self, session, url, kwargs):
        self.assertTrue(url.startswith(hh.HH_API + "/"))
        self.assertEqual(kwargs["timeout"], (10, 30))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(session.headers["User-Agent"], "PersonalJobHunter/0.1")
        self.assertEqual(session.headers["Authorization"], f"Bearer {HH_SECRET}")
        self.calls.append((url, kwargs.get("params")))

    def run_collect(self, fake_get, limit=30):
        def get(session, url, **kwargs):
            return fake_get(session, url, **kwargs)

        with patch.object(requests.Session, "get", new=get), \
             patch.object(cli, "JobStore", side_effect=lambda: JobStore(self.db)), \
             patch.object(hh, "load_settings"), \
             patch.object(hh.time, "sleep"), \
             contextlib.redirect_stdout(self.output):
            return cli.run_collect(limit)

    def paginated_get(self, session, url, **kwargs):
        self.transport(session, url, kwargs)
        if url.endswith("/areas"):
            return response(AREAS)
        if url == hh.HH_API + "/vacancies":
            params = kwargs["params"]
            self.assertEqual(params["area"], "987")
            self.assertEqual(params["host"], "hh.kz")
            self.assertEqual(params["period"], 30)
            self.assertNotIn("experience", params)
            self.assertNotIn("only_with_salary", params)
            query, page = params["text"], params["page"]
            fixtures = {
                hh.SEARCH_QUERIES[0]: [["1", "2"], ["6", "7"]],
                hh.SEARCH_QUERIES[1]: [["2", "3"]],
                hh.SEARCH_QUERIES[2]: [["4", "1"]],
                hh.SEARCH_QUERIES[3]: [["5", "2"]],
                hh.SEARCH_QUERIES[4]: [["3"]],
                hh.SEARCH_QUERIES[5]: [["6"]],
            }
            pages = fixtures[query]
            return response({"items": [{"id": value} for value in pages[page]],
                             "pages": len(pages), "page": page})
        return response(vacancy(url.rsplit("/", 1)[1]))

    def test_pagination_global_limit_and_cross_query_deduplication(self):
        self.assertEqual(self.run_collect(self.paginated_get, limit=7), 0)
        with JobStore(self.db) as store:
            self.assertEqual(len(store.latest(100)), 7)
        searches = [params for url, params in self.calls if url == hh.HH_API + "/vacancies"]
        self.assertEqual([p["text"] for p in searches[:6]], list(hh.SEARCH_QUERIES))
        self.assertEqual(searches[-1]["page"], 1)
        details = [url for url, _ in self.calls if "/vacancies/" in url]
        self.assertEqual(len(details), len(set(details)))
        self.assertEqual(len(details), 7)
        self.assertIn("Найдено уникальных: 7", self.output.getvalue())
        self.assertIn("Добавлено новых: 7", self.output.getvalue())
        self.assert_safe_output()

    def test_repeat_collection_preserves_status_and_first_seen(self):
        self.assertEqual(self.run_collect(self.paginated_get, limit=7), 0)
        with JobStore(self.db) as store:
            original = dict(store.get("2"))
            store.connection.execute("UPDATE jobs SET status = 'shortlisted' WHERE vacancy_id = '2'")
            store.connection.commit()
        self.output = io.StringIO()
        self.assertEqual(self.run_collect(self.paginated_get, limit=7), 0)
        with JobStore(self.db) as store:
            updated = store.get("2")
            self.assertEqual(len(store.latest(100)), 7)
            self.assertEqual(updated["status"], "shortlisted")
            self.assertEqual(updated["first_seen_at"], original["first_seen_at"])
            self.assertGreater(updated["last_seen_at"], original["last_seen_at"])
        self.assertIn("Добавлено новых: 0", self.output.getvalue())
        self.assertIn("Уже было в базе (обновлено): 7", self.output.getvalue())

    def test_upsert_updates_fields_and_preserves_user_state_after_reopening(self):
        first = "2026-10-19T01:00:00.000000+00:00"
        last = "2026-10-20T02:00:00.000000+00:00"
        with JobStore(self.db) as store:
            self.assertTrue(store.save(vacancy(), seen_at=first))
            self.assertEqual(store.get("1")["status"], "new")
            store.connection.execute("UPDATE jobs SET status = 'saved' WHERE vacancy_id = '1'")
            store.connection.commit()
        changed = vacancy()
        changed["name"] = "Updated ML Engineer"
        changed["description"] = "<p>Updated &amp; complete</p>"
        with JobStore(self.db) as store:
            self.assertFalse(store.save(changed, seen_at=last))
            self.assertFalse(store.save(changed, seen_at=last))
            row = store.get("1")
            self.assertEqual(len(store.latest(20)), 1)
            self.assertEqual(row["status"], "saved")
            self.assertEqual(row["first_seen_at"], first)
            self.assertEqual(row["last_seen_at"], last)
            self.assertEqual(row["title"], "Updated ML Engineer")
            self.assertEqual(row["description_html"], changed["description"])
            self.assertEqual(row["description_text"], "Updated & complete")
            self.assertEqual(json.loads(row["salary_json"])["currency"], "KZT")
            self.assertEqual(row["experience_id"], "moreThan6")

    def test_uniqueness_is_source_and_vacancy_id(self):
        with JobStore(self.db) as store:
            self.assertTrue(store.save(vacancy(), source="hh"))
            self.assertTrue(store.save(vacancy(), source="other"))
            self.assertFalse(store.save(vacancy(), source="hh"))
            self.assertEqual(len(store.latest(20)), 2)

    def test_optional_salary_employer_and_experience(self):
        item = vacancy()
        for field in ("salary", "employer", "experience"):
            item[field] = None
        with JobStore(self.db) as store:
            store.save(item)
            row = store.get("1")
            self.assertIsNone(row["salary_json"])
            self.assertIsNone(row["company"])
            self.assertIsNone(row["experience_name"])

    def partial_get(self, fault):
        def fake_get(session, url, **kwargs):
            self.transport(session, url, kwargs)
            if url.endswith("/areas"):
                return response(AREAS)
            if url == hh.HH_API + "/vacancies":
                return response({"items": [{"id": "1"}, {"id": "2"}], "pages": 1})
            if url.endswith("/1"):
                return response(vacancy("1"))
            if isinstance(fault, BaseException):
                raise fault
            return fault
        return fake_get

    def test_auth_and_captcha_stop_immediately_preserving_committed_rows(self):
        for name, fault in (
            ("401", response({"message": HH_SECRET}, 401)),
            ("403", response({"message": HH_SECRET}, 403)),
            ("captcha", response({"errors": [{"type": "captcha_required",
                "captcha_url": "https://hh.ru/captcha?state=" + HH_SECRET}]}, 403)),
            ("oauth", response({"errors": [{"type": "oauth", "value": "bad_access_token"}]}, 400)),
        ):
            with self.subTest(name=name):
                self.db = Path(self.temp.name) / name / "jobs.sqlite"
                self.calls = []
                self.output = io.StringIO()
                self.assertEqual(self.run_collect(self.partial_get(fault), limit=10), 1)
                self.assertEqual(len(self.calls), 4)
                with JobStore(self.db) as store:
                    self.assertIsNotNone(store.get("1"))
                    self.assertIsNone(store.get("2"))
                self.assertIn("ЧАСТИЧНЫЙ СБОР", self.output.getvalue())
                self.assertIn("HH_ACCESS_TOKEN", self.output.getvalue())
                self.assertIn("OAuth 2.0", self.output.getvalue())
                self.assertIn("Найдено уникальных: 2", self.output.getvalue())
                self.assertIn("Добавлено новых: 1", self.output.getvalue())
                self.assertIn("Ошибок: 1", self.output.getvalue())
                self.assert_safe_output()

    def test_keyboard_interrupt_keeps_already_committed_rows(self):
        self.assertEqual(self.run_collect(self.partial_get(KeyboardInterrupt()), limit=10), 1)
        with JobStore(self.db) as store:
            self.assertIsNotNone(store.get("1"))
        self.assertIn("ЧАСТИЧНЫЙ СБОР", self.output.getvalue())

    def test_removed_vacancy_is_reported_without_losing_other_results(self):
        self.assertEqual(self.run_collect(self.partial_get(response({}, 404)), limit=2), 1)
        with JobStore(self.db) as store:
            self.assertEqual(len(store.latest(20)), 1)
        self.assertIn("HTTP 404", self.output.getvalue())

    def test_empty_search_is_success_for_all_six_queries(self):
        def fake_get(session, url, **kwargs):
            self.transport(session, url, kwargs)
            return response(AREAS if url.endswith("/areas") else {"items": [], "pages": 0})
        self.assertEqual(self.run_collect(fake_get), 0)
        self.assertEqual(len(self.calls), 7)
        self.assertIn("Найдено уникальных: 0", self.output.getvalue())
        self.assertIn("Ошибок: 0", self.output.getvalue())

    def test_network_errors_retry_at_most_three_times(self):
        for error in (requests.Timeout(HH_SECRET), requests.ConnectionError(HH_SECRET)):
            with self.subTest(error=type(error).__name__), \
                 patch.object(requests.Session, "get", side_effect=error) as get, \
                 patch.object(hh.time, "sleep") as sleep, hh.HHClient() as client:
                with self.assertRaises(hh.HHError) as caught:
                    client.kazakhstan_id()
                self.assertTrue(caught.exception.stop_collection)
                self.assertNotIn(HH_SECRET, str(caught.exception))
                self.assertEqual(get.call_count, 3)
                self.assertEqual(sleep.call_count, 2)

    def test_rate_limit_and_server_errors_have_bounded_retries_and_delays(self):
        for status in (429, 500, 503):
            with self.subTest(status=status), \
                 patch.object(requests.Session, "get", return_value=response(
                     {"message": HH_SECRET}, status, headers={"Retry-After": "9999"}
                 )) as get, patch.object(hh.time, "sleep") as sleep, hh.HHClient() as client:
                with self.assertRaises(hh.HHError) as caught:
                    client.kazakhstan_id()
                self.assertEqual(get.call_count, 3)
                self.assertEqual(sleep.call_count, 2)
                self.assertTrue(all(1 <= call.args[0] <= 10 for call in sleep.call_args_list))
                self.assertTrue(caught.exception.stop_collection)
                self.assertNotIn(HH_SECRET, str(caught.exception))

    def test_transient_429_recovers(self):
        with patch.object(requests.Session, "get", side_effect=[
            response({}, 429, headers={"Retry-After": "1"}), response(AREAS)
        ]) as get, patch.object(hh.time, "sleep") as sleep, hh.HHClient() as client:
            self.assertEqual(client.kazakhstan_id(), "987")
            self.assertEqual(get.call_count, 2)
            sleep.assert_called_once_with(1.0)

    def test_redirects_and_malformed_responses_are_not_followed_or_dumped(self):
        for reply in (response({}, 302, headers={"Location": "https://example.test/"}),
                      response(raw=HH_SECRET), response({"items": [], "pages": -1})):
            with self.subTest(status=reply.status_code), \
                 patch.object(requests.Session, "get", return_value=reply) as get, \
                 hh.HHClient() as client:
                with self.assertRaises(hh.HHError) as caught:
                    client.search("LLM", "987", 0, 5)
                self.assertEqual(get.call_count, 1)
                self.assertFalse(get.call_args.kwargs["allow_redirects"])
                self.assertNotIn(HH_SECRET, str(caught.exception))

    def test_missing_token_does_not_send_openrouter_key(self):
        with patch.dict(os.environ, {"HH_ACCESS_TOKEN": ""}), \
             patch.object(requests.Session, "get", return_value=response(AREAS)), \
             hh.HHClient() as client:
            self.assertFalse(client.has_token)
            self.assertNotIn("Authorization", client.session.headers)
            self.assertEqual(client.kazakhstan_id(), "987")

    def test_local_commands_work_without_settings_or_network(self):
        with JobStore(self.db) as store:
            store.save(vacancy())
        with patch.object(requests.Session, "request", side_effect=AssertionError("Network forbidden")), \
             patch.object(hh, "load_settings", side_effect=AssertionError("Settings forbidden")), \
             patch.object(cli, "JobStore", side_effect=lambda: JobStore(self.db)), \
             contextlib.redirect_stdout(self.output):
            self.assertEqual(cli.main(["list", "--limit", "20"]), 0)
            self.assertEqual(cli.main(["show", "1"]), 0)
            self.assertEqual(cli.main(["show", "999"]), 1)
        text = self.output.getvalue()
        self.assertIn("Test Company", text)
        self.assertIn("Алматы", text)
        self.assertIn("https://hh.kz/vacancy/1", text)
        self.assertIn("ML & AI\nPython\nLLM", text)
        self.assertNotIn("<p>", text)

    def test_env_loading_preserves_process_settings_and_file(self):
        env = Path(self.temp.name) / ".env"
        content = f"HH_ACCESS_TOKEN={HH_SECRET}\nHH_USER_AGENT=FromFile/1.0\n"
        env.write_text(content, encoding="utf-8-sig")
        original = env.read_bytes()
        with patch.dict(os.environ, {"HH_USER_AGENT": "Existing/1.0"}, clear=True), \
             patch.object(hh, "ENV_PATH", env):
            hh.load_settings()
            self.assertEqual(os.environ["HH_ACCESS_TOKEN"], HH_SECRET)
            self.assertEqual(os.environ["HH_USER_AGENT"], "Existing/1.0")
        self.assertEqual(env.read_bytes(), original)

    def test_html_text_preserves_paragraphs_and_omits_scripts(self):
        self.assertEqual(hh.html_to_text(
            "<p>First &amp; second</p><script>hidden()</script><p>Last<br/>Line</p>"
        ), "First & second\nLast\nLine")

    def test_setup_check_reports_an_empty_profile_with_a_pointer_to_the_example(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(self.output):
            with self.assertRaises(hh.CheckError) as error:
                check_setup.check_profile(Path(folder))
        self.assertIn("examples/profile", str(error.exception))
        self.assertIn("Профиль", [name for name, _ in check_setup.CHECKS])  # part of the normal run

    def test_hh_api_refusal_without_token_is_skipped_because_search_uses_the_browser(self):
        # 17.11.2026: anonymous /vacancies returns HTTP 403 for everyone; the program reads HH through Playwright.
        def fake_get(session, url, **kwargs):
            if url.endswith("/areas"):
                return response(AREAS)
            return response({"errors": [{"type": "forbidden"}]}, status=403)
        for token, expected in (("", "SKIP"), (HH_SECRET, None)):
            with patch.dict(os.environ, {"HH_ACCESS_TOKEN": token}), \
                 patch.object(requests.Session, "get", new=fake_get), contextlib.redirect_stdout(self.output):
                if expected:
                    self.assertEqual(check_setup.check_hh(), expected)
                else:
                    with self.assertRaises(hh.CheckError):check_setup.check_hh()
        self.assertIn("браузер", self.output.getvalue())

    def test_existing_hh_and_sqlite_checks_reuse_client_without_openrouter(self):
        def fake_get(session, url, **kwargs):
            self.transport(session, url, kwargs)
            if url.endswith("/areas"):
                return response(AREAS)
            if url == hh.HH_API + "/vacancies":
                return response({"items": [vacancy()], "pages": 1})
            return response(vacancy())
        with patch.object(requests.Session, "get", new=fake_get), \
             contextlib.redirect_stdout(self.output):
            self.assertEqual(check_setup.check_hh(), "OK")
            self.assertEqual(check_setup.check_sqlite(), "OK")
        self.assertEqual(len(self.calls), 3)
        self.assert_safe_output()


if __name__ == "__main__":
    unittest.main()
