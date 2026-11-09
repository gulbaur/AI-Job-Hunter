"""Local HTML fixtures and temporary databases; no HH network or real session data."""

import json
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

import browser_check as browser
from storage import JobStore


FIXTURE = """
<h1 data-qa="vacancy-title">ML Engineer</h1>
<a data-qa="vacancy-company-name">Example Company</a>
<span data-qa="vacancy-view-location">Алматы</span>
<div data-qa="vacancy-description"><p>Full &amp; complete</p><p>Python and ML.</p></div>
<script type="application/ld+json">
{"@graph": [{"@type": "JobPosting", "datePosted": "2026-10-19T10:00:00+05:00"}]}
</script>
"""


class BrowserCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        # All external requests fail in these tests; only set_content fixtures are read.
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.addCleanup(self.context.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "jobs.sqlite"
        manifest_patch = patch.object(browser, "RUN_MANIFEST", Path(self.temp.name) / "last_browser_run.json")
        manifest_patch.start()
        self.addCleanup(manifest_patch.stop)

    def test_search_has_explicit_kazakhstan_and_ai_ml_queries(self):
        params = parse_qs(urlsplit(browser.SEARCH_URL).query)
        self.assertEqual(params["area"], ["40"])
        self.assertEqual(params["search_period"], ["30"])
        self.assertEqual(params["search_field"], ["name"])
        for phrase in ("machine learning", "ML Engineer", "AI Engineer", "LLM",
                       "Computer Vision", "машинное обучение"):
            self.assertIn(phrase, params["text"][0])

    def test_links_deduplicate_tracking_urls_and_reject_external_domains(self):
        links = browser.unique_links([
            "/vacancy/123?from=search", "https://hh.ru/vacancy/123?x=1",
            "https://hh.kz.evil.test/vacancy/555", "javascript:alert(1)",
            "https://hh.kz/vacancy/124", "/vacancy/125", "/vacancy/126",
        ], "https://hh.kz/search/vacancy", 3)
        self.assertEqual(links, [(str(value), f"https://hh.kz/vacancy/{value}")
                                 for value in (123, 124, 125)])

    def test_complete_fields_and_html_are_saved_to_existing_schema(self):
        self.page.set_content(FIXTURE)
        item = browser.read_vacancy(self.page, "123", "https://hh.kz/vacancy/123")
        with JobStore(self.db) as store:
            self.assertTrue(browser.save_browser_vacancy(store, item))
            row = store.get("123")
            self.assertEqual(row["source"], "hh")
            self.assertEqual(row["title"], "ML Engineer")
            self.assertEqual(row["company"], "Example Company")
            self.assertEqual(row["region"], "Алматы")
            self.assertEqual(row["description_text"], "Full & complete\nPython and ML.")
            self.assertIn("<p>", row["description_html"])
            self.assertEqual(row["published_at"], "2026-10-19T10:00:00+05:00")
            self.assertEqual(row["status"], "new")

    def test_browser_updates_api_identity_without_resetting_status_or_known_metadata(self):
        self.page.set_content(FIXTURE)
        item = browser.read_vacancy(self.page, "123", "https://hh.kz/vacancy/123")
        with JobStore(self.db) as store:
            original = {**item, "experience": {"id": "between1And3", "name": "1–3 года"},
                        "salary": {"from": 500000, "currency": "KZT"}}
            store.save(original, seen_at="2026-10-19T00:00:00.000000+00:00")
            store.connection.execute("UPDATE jobs SET status = 'shortlisted'")
            store.connection.commit()
            item["published_at"] = ""
            self.assertFalse(browser.save_browser_vacancy(store, item))
            self.assertFalse(browser.save_browser_vacancy(store, item))
            self.assertEqual(len(store.latest(10)), 1)
            row = store.get("123")
            self.assertEqual(row["status"], "shortlisted")
            self.assertEqual(row["first_seen_at"], "2026-10-19T00:00:00.000000+00:00")
            self.assertEqual(row["experience_id"], "between1And3")
            self.assertEqual(json.loads(row["salary_json"])["from"], 500000)
            self.assertEqual(row["published_at"], original["published_at"])

    def test_unknown_publication_date_is_not_fabricated(self):
        self.page.set_content(FIXTURE.split('<script')[0])
        item = browser.read_vacancy(self.page, "123", "https://hh.kz/vacancy/123")
        self.assertEqual(item["published_at"], "")
        with JobStore(self.db) as store:
            browser.save_browser_vacancy(store, item)
            self.assertEqual(store.get("123")["published_at"], "")

    def test_changed_interface_reports_exact_missing_field(self):
        self.page.set_content(FIXTURE.replace('data-qa="vacancy-company-name"', 'data-qa="changed"'))
        with self.assertRaisesRegex(browser.VacancyReadError, "компания"):
            browser.read_vacancy(self.page, "123", "https://hh.kz/vacancy/123")

    def test_missing_location_is_unknown_not_a_collection_error(self):
        self.page.set_content(FIXTURE.replace('data-qa="vacancy-view-location"', 'data-qa="changed"'))
        item = browser.read_vacancy(self.page, "123", "https://hh.kz/vacancy/123")
        self.assertEqual(item["area"]["name"], "")
        with JobStore(self.db) as store:
            browser.save_browser_vacancy(store, item)
            self.assertEqual(store.get("123")["region"], "")
            item["area"]["name"] = "Алматы"
            browser.save_browser_vacancy(store, item)
            item["area"]["name"] = ""
            browser.save_browser_vacancy(store, item)
            self.assertEqual(store.get("123")["region"], "Алматы")

    def test_location_falls_back_to_public_job_posting(self):
        html = FIXTURE.replace('data-qa="vacancy-view-location"', 'data-qa="changed"')
        html = html.replace('"datePosted":', '"jobLocation":{"address":{"addressLocality":"Астана"}},"datePosted":')
        self.page.set_content(html)
        self.assertEqual(browser.read_vacancy(self.page, "123", "https://hh.kz/vacancy/123")["area"]["name"], "Астана")

    def test_captcha_and_expired_login_stop_without_reading_session(self):
        self.page.set_content('<div data-qa="captcha">Robot check</div>')
        with self.assertRaisesRegex(browser.BrowserStop, "капчу"):
            browser.check_blocker(self.page)
        self.page.set_content("<p>Login required</p>")
        proxy = SimpleNamespace(is_closed=lambda: False,
                                url="https://hh.kz/account/login?secret=must-not-print",
                                locator=self.page.locator, get_by_text=self.page.get_by_text)
        with self.assertRaises(browser.BrowserStop) as caught:
            browser.check_blocker(proxy)
        self.assertIn("авторизация", str(caught.exception))
        self.assertNotIn("must-not-print", str(caught.exception))

    def test_browser_and_local_cli_import_without_hh_or_requests(self):
        code = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'hh', 'requests', 'dotenv'}:
        raise AssertionError('API dependency was imported')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import browser_check, storage, main
print('Browser and SQLite CLI: API-independent imports OK')
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1], timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_manual_login_handoff_uses_new_search_tab_if_original_tab_closed(self):
        visited = []

        def navigate(page, url, stage):
            visited.append(page)
            if url == browser.SEARCH_URL:
                page.set_content('<a data-qa="serp-item__title" href="https://hh.kz/vacancy/123">ML Engineer</a>')
            elif "/vacancy/123" in url:
                page.set_content(FIXTURE)

        launcher = SimpleNamespace(chromium=SimpleNamespace(
            launch_persistent_context=lambda **kwargs: self.context))
        with patch.object(browser, "sync_playwright") as runtime, \
             patch("builtins.input", side_effect=lambda prompt: self.page.close()), \
             patch.object(browser, "navigate", side_effect=navigate), \
             patch.object(browser, "JobStore", side_effect=lambda: JobStore(self.db)), \
             contextlib.redirect_stdout(io.StringIO()):
            runtime.return_value.__enter__.return_value = launcher
            self.assertEqual(browser.run(1), 0)
        self.assertIsNot(visited[0], visited[1])
        with JobStore(self.db) as store:
            self.assertIsNotNone(store.get("123"))

    def test_junior_search_keeps_region_and_uses_both_experience_buckets(self):
        params = parse_qs(urlsplit(browser.search_url(junior=True)).query)
        self.assertEqual(params["area"], ["40"])
        self.assertEqual(params["experience"], ["noExperience", "between1And3"])
        self.assertIn("LLM", params["text"][0])
        self.assertNotIn("experience", parse_qs(urlsplit(browser.SEARCH_URL).query))

    def test_title_filter_rejects_reported_adjacent_roles_but_keeps_target_roles(self):
        for title in ["Проектный менеджер / Бизнес-аналитик (IT)", "Fraud Analyst / Антифрод-менеджер",
                      "AI Project Manager", "Руководитель направления AI-ассистентов",
                      "Senior ML Engineer в RecSys", "Lead Computer Vision Engineer",
                      "Data analyst в команду Computer Vision", "Data Labeling Intern — Computer Vision / AI"]:
            self.assertFalse(browser.title_is_relevant(title), title)
        for title in ["ML Engineer", "Junior Data Scientist", "LLM Backend Engineer",
                      "Инженер машинного обучения", "Computer Vision Engineer"]:
            self.assertTrue(browser.title_is_relevant(title), title)

    def test_reuse_session_does_not_request_input_or_bypass_login_blocker(self):
        launcher = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda **kwargs: self.context))
        def navigate(page, url, stage):
            if url != "https://hh.kz/":
                raise browser.BrowserStop("Сессия истекла; требуется ручной вход.")
        with patch.object(browser, "sync_playwright") as runtime, \
             patch("builtins.input") as user_input, \
             patch.object(browser, "navigate", side_effect=navigate), \
             contextlib.redirect_stdout(io.StringIO()):
            runtime.return_value.__enter__.return_value = launcher
            self.assertEqual(browser.run(1, reuse_session=True), 1)
        user_input.assert_not_called()

    def test_malformed_vacancy_does_not_block_next_card(self):
        def navigate(page, url, stage):
            if url == browser.SEARCH_URL:
                page.set_content('<a data-qa="serp-item__title" href="https://hh.kz/vacancy/111">ML Engineer</a>'
                                 '<a data-qa="serp-item__title" href="https://hh.kz/vacancy/222">AI Engineer</a>')
            elif "/vacancy/111" in url:
                page.set_content(FIXTURE.replace('data-qa="vacancy-company-name"', 'data-qa="changed"'))
            elif "/vacancy/222" in url:
                page.set_content(FIXTURE)
        launcher = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda **kwargs: self.context))
        with patch.object(browser, "sync_playwright") as runtime, \
             patch.object(browser, "navigate", side_effect=navigate), \
             patch.object(browser, "JobStore", side_effect=lambda: JobStore(self.db)), \
             contextlib.redirect_stdout(io.StringIO()):
            runtime.return_value.__enter__.return_value = launcher
            self.assertEqual(browser.run(1, reuse_session=True), 1)
        with JobStore(self.db) as store:
            self.assertIsNone(store.get("111"))
            self.assertIsNotNone(store.get("222"))

    def test_expanded_plan_prioritizes_entry_roles_and_bounds_pages(self):
        plan = browser.search_plan(junior=True, expanded=True, pages=2)
        self.assertEqual(len(plan), 6)
        self.assertIn("Junior", plan[0][0])
        params = [parse_qs(urlsplit(url).query) for _, url in plan]
        for p in params:
            self.assertEqual(p["area"], ["40"])
            self.assertEqual(p["search_field"], ["name"])
            self.assertEqual(p["experience"], ["noExperience", "between1And3"])
        self.assertIn("стажер", params[0]["text"][0])
        self.assertIn("Data Scientist", params[2]["text"][0])
        self.assertEqual(params[1]["page"], ["1"])
        with self.assertRaises(ValueError):
            browser.search_plan(junior=True, expanded=True, pages=100)

    def test_known_jobs_are_not_reopened_and_queries_deduplicate_without_resetting_status(self):
        self.page.set_content(FIXTURE)
        old = browser.read_vacancy(self.page, "111", "https://hh.kz/vacancy/111")
        plan = [("Entry", "https://hh.kz/search/vacancy?page=0"),
                ("ML", "https://hh.kz/search/vacancy?page=1")]
        opened = []
        def navigate(page, url, stage):
            if "search/vacancy" in url:
                ids = (111, 222) if "page=0" in url else (222, 333)
                page.set_content(''.join(f'<a data-qa="serp-item__title" href="https://hh.kz/vacancy/{i}">ML Engineer</a>' for i in ids))
            else:
                opened.append(url)
                page.set_content(FIXTURE)
        detail = self.context.new_page()
        with JobStore(self.db) as store:
            store.save(old, seen_at="2026-10-01T00:00:00Z")
            store.connection.execute("UPDATE jobs SET status='viewed'")
            store.connection.commit()
            with patch.object(browser, "search_plan", return_value=plan), \
                 patch.object(browser, "navigate", side_effect=navigate), \
                 contextlib.redirect_stdout(io.StringIO()):
                counts = dict(saved=0, added=0, errors=0, skipped=0, known=0, searches=0)
                browser.collect_searches(self.page, detail, store, limit=3, junior=True, expanded=True,
                                         pages=1, refresh=False, counts=counts)
                self.assertEqual(counts["saved"], 2)
                self.assertEqual(counts["known"], 1)
                self.assertEqual(opened, ["https://hh.kz/vacancy/222", "https://hh.kz/vacancy/333"])
                opened.clear()
                counts = dict(saved=0, added=0, errors=0, skipped=0, known=0, searches=0)
                browser.collect_searches(self.page, detail, store, limit=3, junior=True, expanded=True,
                                         pages=1, refresh=False, counts=counts)
                self.assertEqual(counts["saved"], 0)
                self.assertEqual(counts["known"], 3)
                self.assertEqual(opened, [])
                browser.collect_searches(self.page, detail, store, limit=1, junior=True, expanded=True,
                                         pages=1, refresh=True, counts=counts)
                self.assertEqual(opened, ["https://hh.kz/vacancy/111"])
            self.assertEqual(store.get("111")["status"], "viewed")
            self.assertEqual(store.get("111")["first_seen_at"], "2026-10-01T00:00:00Z")

    def test_blocker_in_later_query_keeps_saved_progress_and_stops(self):
        plan = [("Entry", "https://hh.kz/search/vacancy?page=0"),
                ("ML", "https://hh.kz/search/vacancy?page=1")]
        def navigate(page, url, stage):
            if "page=1" in url:
                raise browser.BrowserStop("Капча: сбор остановлен.")
            if "search/vacancy" in url:
                page.set_content('<a data-qa="serp-item__title" href="https://hh.kz/vacancy/222">ML Engineer</a>')
            else:
                page.set_content(FIXTURE)
        counts = dict(saved=0, added=0, errors=0, skipped=0, known=0, searches=0)
        with JobStore(self.db) as store, \
             patch.object(browser, "search_plan", return_value=plan), \
             patch.object(browser, "navigate", side_effect=navigate), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(browser.BrowserStop, "Капча"):
                browser.collect_searches(self.page, self.context.new_page(), store, limit=3, junior=True,
                                         expanded=True, pages=1, refresh=False, counts=counts)
            self.assertEqual(counts["saved"], 1)
            self.assertIsNotNone(store.get("222"))

    def test_visible_total_skips_nonexistent_second_page(self):
        plan = [("Entry", "https://hh.kz/search/vacancy?page=0"),
                ("Entry", "https://hh.kz/search/vacancy?page=1")]
        visited = []
        def navigate(page, url, stage):
            visited.append(url)
            if "search/vacancy" in url:
                page.set_content('<h1 data-qa="vacancies-search-header">Найдена 1 вакансия</h1>'
                                 '<a data-qa="serp-item__title" href="https://hh.kz/vacancy/222">ML Engineer</a>')
            else:
                page.set_content(FIXTURE)
        with JobStore(self.db) as store, \
             patch.object(browser, "search_plan", return_value=plan), \
             patch.object(browser, "navigate", side_effect=navigate), \
             contextlib.redirect_stdout(io.StringIO()):
            counts = dict(saved=0, added=0, errors=0, skipped=0, known=0, searches=0)
            browser.collect_searches(self.page, self.context.new_page(), store, limit=3, junior=True,
                                     expanded=True, pages=2, refresh=False, counts=counts)
            self.assertEqual(counts["saved"], 1)
            self.assertEqual(counts["searches"], 1)
            self.assertEqual(counts["vacancy_ids"], ["222"])
            self.assertFalse(any("page=1" in u for u in visited))

    def test_excluded_company_does_not_consume_pipeline_limit(self):
        preferences={"office_city":"Алматы","other_locations":"remote_only","remote_from":"Казахстан",
                     "excluded_companies":["Kaspi.kz"]}
        def navigate(page,url,stage):
            if 'search/vacancy' in url:
                page.set_content('<a data-qa="serp-item__title" href="https://hh.kz/vacancy/222">ML Engineer</a>'
                                 '<a data-qa="serp-item__title" href="https://hh.kz/vacancy/333">AI Engineer</a>')
            else:
                page.set_content(FIXTURE.replace('Example Company','Kaspi.kz' if url.endswith('222') else 'Other Company'))
        with JobStore(self.db) as store,patch.object(browser,'search_plan',return_value=[('AI','https://hh.kz/search/vacancy')]), \
             patch.object(browser,'navigate',side_effect=navigate),contextlib.redirect_stdout(io.StringIO()):
            counts=dict(saved=0,added=0,errors=0,skipped=0,known=0,searches=0,vacancy_ids=[])
            browser.collect_searches(self.page,self.context.new_page(),store,limit=1,junior=True,expanded=True,
                                     pages=1,refresh=False,counts=counts,preferences=preferences)
            self.assertEqual(counts['vacancy_ids'],['333'])
            self.assertEqual(counts['filtered'],1)
            self.assertEqual(store.get('222')['status'],'filtered')
            self.assertEqual(store.get('222')['status_reason'],'company')


if __name__ == "__main__":
    unittest.main()
