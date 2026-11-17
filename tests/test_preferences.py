"""User rule: office in Almaty, other locations only with explicit remote work."""

import unittest

from preferences import location_fit


PREFERENCES = {"office_city": "Алматы", "other_locations": "remote_only", "remote_from": "Казахстан"}


class LocationTests(unittest.TestCase):
    def test_almaty_office_is_allowed(self):
        for region in ["Алматы, Абая 26", "Almaty", "Алма-Ата"]:
            result, _ = location_fit({"region": region, "description_text": "Удалённого формата нет."}, PREFERENCES)
            self.assertEqual(result, "allowed")

    def test_astana_office_and_unknown_format_do_not_pass(self):
        for text, expected in [("Работа полностью из офиса, удалённого и гибридного формата нет.", "blocked"),
                               ("Строго оффлайн формат работы", "blocked"),
                               ("NLP and CV research; location Astana", "unconfirmed")]:
            result, _ = location_fit({"region": "Астана", "description_text": text}, PREFERENCES)
            self.assertEqual(result, expected)

    def test_other_countries_need_explicit_remote_and_eligibility_review(self):
        result, reason = location_fit({"region": "Berlin", "description_text": "Fully remote role."}, PREFERENCES)
        self.assertEqual(result, "remote_review")
        self.assertIn("Казахстан", reason)
        result, _ = location_fit({"region": "Berlin", "description_text": "Office work only."}, PREFERENCES)
        self.assertNotIn(result, {"allowed", "remote_review"})

    def test_negated_remote_does_not_pass_as_remote_offer(self):
        result, _ = location_fit({"region": "Астана", "description_text": "Удалённая работа не предусмотрена."}, PREFERENCES)
        self.assertEqual(result, "blocked")

    def test_office_phrasings_and_hybrid_outside_almaty_are_office(self):
        # Audit A6: all of these passed as remote_review and spent a paid assessment.
        for text in ["Удалённая работа невозможна.", "Формат: офис, без удалёнки.",
                     "Гибрид: 2 дня удалённо, 3 в офисе.", "Hybrid: 3 days in the office, remote on Fridays.",
                     "Формат работы — гибридный (удалённо до 2 дней в неделю)."]:
            result, _ = location_fit({"region": "Астана", "description_text": text}, PREFERENCES)
            self.assertEqual(result, "blocked", text)

    def test_hybrid_with_full_remote_option_still_needs_remote_review(self):
        for text in ["Формат: гибрид или полностью удалённо.", "Hybrid or fully remote."]:
            result, _ = location_fit({"region": "Астана", "description_text": text}, PREFERENCES)
            self.assertEqual(result, "remote_review", text)


class ExcludedCompanyTests(unittest.TestCase):
    """Moved from the archived tests/test_apply.py (audit A38)."""
    def test_excluded_companies_are_exact_and_backward_compatible(self):
        import json,tempfile
        from pathlib import Path
        from preferences import load_preferences,company_is_excluded
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            data={"office_city":"Алматы","other_locations":"remote_only","remote_from":"Казахстан"}
            (root/"preferences.json").write_text(json.dumps(data),encoding="utf-8")
            self.assertEqual(load_preferences(root),data)
            data["excluded_companies"]=["Kaspi.kz","Kaspi"]
            (root/"preferences.json").write_text(json.dumps(data),encoding="utf-8")
            prefs=load_preferences(root)
            self.assertTrue(company_is_excluded({"company":"  KASPI.KZ  "},prefs))
            self.assertFalse(company_is_excluded({"company":"Other Kaspi partners"},prefs))
            # Senior review 13.11 (pipeline 13): web-search ATS links name the company by its slug.
            self.assertTrue(company_is_excluded({"company":"higgsfieldai"},{"excluded_companies":["Higgsfield AI"]}))
            self.assertFalse(company_is_excluded({"company":"Higgsfield AI Labs"},{"excluded_companies":["Higgsfield AI"]}))


if __name__ == "__main__":
    unittest.main()
