# Example profile

Copy these files to `profile/` in the project root (that folder is git-ignored), fill them in and add your CVs.
`check_setup.py` checks the profile before the first run.

| File | Required | What it is |
|---|---|---|
| `CV_ML_ENGINEER_EN.docx`, `CV_APPLIED_AI_EN.docx` | yes | Two CV variants in English (ML engineer / applied AI). |
| `CV_ML_ENGINEER_RU.docx`, `CV_APPLIED_AI_RU.docx` | yes | The same two CVs in Russian, for Russian letters. |
| `candidate.md` | yes | In your own words: the roles and level you are looking for, the geography, and in which voice (grammatical gender, tone) letters are written. It is added to the prompts. |
| `preferences.json` | yes | Geography: an office city, and remote work only from the given country. Companies to exclude. |
| `confirmed_facts.json` | no | Extra facts you confirm that are not in the CV; letters may use them. |
| `search_settings.json`, `sources.json` | no | Created by the app's settings page; defaults are used until then. |

## CV format

Letters may only use sentences from the CV, so the CV is read strictly:

- DOCX; everything above the paragraph **Work Experience** / **Опыт работы** (name, contacts) is ignored;
- an experience bullet is 35–500 characters long and starts with a past-tense action verb:
  *Developed, Built, Evaluated, Implemented…* in English, *Разработал(а), Создал(а), Оценил(а)…* in Russian
  (the lists are `EN_ACTION` and `ACTION_VERBS` in `prepare.py`; extend them if your CV uses other verbs).
