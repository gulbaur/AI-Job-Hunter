import contextlib
import io
import json
from pathlib import Path
from datetime import datetime,timezone
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

import browser_check
import draft_queue
import hunt
import hunter_menu
import scheduled_run
import search_settings
from common import CheckError
from storage import JobStore
from test_hunt import vacancy,PREF


class SettingsTests(unittest.TestCase):
    def test_selected_groups_days_and_experience_reach_hh_query(self):
        settings=search_settings.validate({'role_groups':['applied_ai'],'search_days':7})
        plan=browser_check.search_plan(junior=True,expanded=True,pages=1,settings=settings)
        self.assertEqual(len(plan),1)
        query=parse_qs(urlsplit(plan[0][1]).query)
        self.assertEqual(query['search_period'],['7'])
        self.assertIn('LLM',query['text'][0])
        self.assertEqual(query['experience'],['noExperience','between1And3'])

    def test_bad_scope_and_nonfinite_budget_do_not_save(self):
        for value in [{'role_groups':[]},{'role_groups':['everything']},{'search_days':100},
                      {'budget_usd':float('nan')},{'budget_usd':float('inf')},{'budget_usd':-1},
                      {'vacancies_per_run':11},{'vacancies_per_run':0},{'send':True},
                      {'excluded_title_words':['']}]:
            with self.subTest(value=value),self.assertRaises(CheckError):search_settings.validate(value)

    def test_ten_letters_and_no_cash_limit_are_supported(self):
        cfg=search_settings.validate({'vacancies_per_run':10,'budget_usd':None})
        self.assertEqual(cfg['vacancies_per_run'],10)
        self.assertIsNone(cfg['budget_usd'])
        self.assertEqual(search_settings.validate({'budget_usd':1})['budget_usd'],1)

    def test_a_new_profile_starts_with_a_money_limit_per_run(self):
        # Publication review 17.11.2026: a fresh clone must not spend without a cap; «no limit» stays an explicit choice.
        self.assertEqual(search_settings.validate({})['budget_usd'],0.5)

    def test_menu_saves_real_settings_without_changing_geography(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);profile=root/'profile';profile.mkdir()
            prefs=profile/'preferences.json';prefs.write_text(json.dumps(PREF),encoding='utf-8')
            search_settings.save(profile,{'role_groups':['applied_ai']})
            answers=iter(['MLOps; Data Scientist','7','2','1','да','0,01','',''])
            with patch.object(hunter_menu,'ROOT',root),contextlib.redirect_stdout(io.StringIO()) as output:
                hunter_menu.settings_menu(lambda _:next(answers))
            settings=search_settings.load(profile)
            self.assertEqual(settings['role_groups'],list(search_settings.GROUPS))
            self.assertNotIn('Номера групп',output.getvalue())
            plan=browser_check.search_plan(junior=True,expanded=True,pages=1,settings=settings)
            self.assertEqual(len(plan),3)
            self.assertEqual(settings['vacancies_per_run'],2)
            self.assertEqual(settings['budget_usd'],.01)
            self.assertFalse(search_settings.title_allowed('Data Scientist',settings))
            self.assertEqual(json.loads(prefs.read_text(encoding='utf-8')),PREF)

    def test_excluded_titles_are_rechecked_for_saved_drafts(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with JobStore(root/'jobs.sqlite') as store:
                item=vacancy();item['name']='MLOps Engineer';store.save(item)
                letter=root/'letter.txt';letter.write_text('Проверенный факт.',encoding='utf-8')
                cv=root/'cv.docx';cv.write_bytes(b'fixture')
                path=root/'application.json';path.write_text(json.dumps({'source':'hh','vacancy_id':'1',
                        'letter_path':str(letter),'local_cv_path':str(cv)}),encoding='utf-8')
                draft_queue.register(store.connection,path)
                self.assertEqual(len(draft_queue.pending(store,PREF)),1)
                settings=search_settings.validate({'excluded_title_words':['MLOps']})
                self.assertEqual(draft_queue.pending(store,PREF,settings),[])
                self.assertEqual(hunt.eligible(store,PREF,settings=settings),[])

    def test_excluded_title_word_matches_whole_words_only(self):
        # Audit A32: «Lead» also excluded «Leading AI Engineer»; «ML» would exclude «HTML».
        settings=search_settings.validate({'excluded_title_words':['Lead','Senior ML']})
        self.assertFalse(search_settings.title_allowed('Team Lead, Data Science',settings))
        self.assertFalse(search_settings.title_allowed('Senior ML Engineer',settings))
        self.assertTrue(search_settings.title_allowed('Leading AI Engineer',settings))
        self.assertTrue(search_settings.title_allowed('Senior MLOps Engineer',settings))
        self.assertFalse(search_settings.title_allowed('Junior MLOps (lead)',search_settings.validate({'excluded_title_words':['MLOps']})))

    def test_scheduled_preparation_has_no_send_flag_and_runs_once_per_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);report=root/'report.json'
            report.write_text(json.dumps({'state':'draft_ready','packet_paths':['one'],'model_usage':{'calls':1}}),encoding='utf-8')
            with patch.object(scheduled_run,'ROOT',root),patch.object(scheduled_run,'STATE',root/'data/state.json'), \
                 patch.object(scheduled_run,'schedule_slot',side_effect=['2026-10-23/10:00','2026-10-23/10:00','2026-10-23/18:00']), \
                 patch.object(hunt,'LAST',report),patch.object(hunt,'main',return_value=0) as runner, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scheduled_run.main(),0)
                self.assertEqual(scheduled_run.main(),0)
                runner.assert_called_once_with(['--run','--queue'])
                self.assertEqual(scheduled_run.main(),0)
                self.assertEqual(runner.call_count,2)
                state=json.loads((root/'data/state.json').read_text(encoding='utf-8'))
                self.assertEqual(state['mode'],'prepare_only')
                self.assertEqual(state['prepared_count'],1)

    def test_almaty_slots_are_separate_and_early_run_is_skipped(self):
        self.assertEqual(scheduled_run.schedule_slot(datetime(2026,10,23,5,tzinfo=timezone.utc)),'2026-10-23/10:00')
        self.assertEqual(scheduled_run.schedule_slot(datetime(2026,10,23,13,tzinfo=timezone.utc)),'2026-10-23/18:00')
        self.assertIsNone(scheduled_run.schedule_slot(datetime(2026,10,23,4,tzinfo=timezone.utc)))


if __name__=='__main__':unittest.main()
