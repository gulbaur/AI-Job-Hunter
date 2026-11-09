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


if __name__ == "__main__":
    unittest.main()
