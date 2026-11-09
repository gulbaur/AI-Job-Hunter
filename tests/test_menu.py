"""Local UI/queue acceptance tests; browser and model traffic stay in test fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import draft_queue
import hh_response
import hunt
import hunter_menu
from storage import JobStore
from common import CheckError
from test_hunt import vacancy, PREF, EN, RU, response


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.db_path=self.root/'jobs.sqlite'
        self.store=JobStore(self.db_path);self.addCleanup(self.store.connection.close)
        self.profile=self.root/'profile';self.profile.mkdir()
        (self.profile/'preferences.json').write_text(json.dumps(PREF),encoding='utf-8')
        __import__('search_settings').save(self.profile,{'letter_mode':'literal'})
        self.store.save(vacancy())

    def packet(self, identity='1'):
        folder=self.root/'drafts'/'runs'/'test'/identity;folder.mkdir(parents=True,exist_ok=True)
        letter=folder/'letter.txt';letter.write_text('Письмо пользователя',encoding='utf-8')
        cv=folder/'cv.docx';cv.write_bytes(b'fixture')
        packet={'source':'hh','vacancy_id':identity,'url':f'https://hh.kz/vacancy/{identity}',
                'title':'AI Engineer','company':'Example','decision':'stretch','gaps':[],
                'local_cv_path':str(cv),'letter_path':str(letter),'output_dir':str(folder)}
        path=folder/'application.json';path.write_text(json.dumps(packet),encoding='utf-8')
        return path,packet

    def test_queue_survives_reopening_and_hides_applied_skipped_excluded_and_unknown(self):
        for identity in ['1','2','3','4','5','6']:
            self.store.save(vacancy(identity,'Kaspi.kz' if identity=='5' else 'Example'))
            path,_=self.packet(identity);draft_queue.register(self.store.connection,path)
        self.store.connection.executescript(hh_response.SCHEMA)
        with self.store.connection:
            self.store.connection.execute("UPDATE jobs SET status='applied' WHERE vacancy_id='2'")
            self.store.connection.execute("UPDATE jobs SET status='skipped' WHERE vacancy_id='3'")
            self.store.connection.execute('INSERT INTO application_attempts VALUES (?,?,?,?)',
                        ('https://hh.kz/vacancy/4','hash','submitted_unverified','now'))
        (self.root/'drafts/runs/test/6/letter.txt').unlink()
        with JobStore(self.db_path) as reopened:
            self.assertEqual([p['vacancy_id'] for _,p in draft_queue.pending(reopened,PREF)],['1'])

    def test_legacy_import_does_not_replace_current_packet_or_user_letter(self):
        path,packet=self.packet();draft_queue.register(self.store.connection,path)
        Path(packet['letter_path']).write_text('Моя редакция',encoding='utf-8')
        other=self.root/'drafts/runs/newer';other.mkdir()
        (other/'application.json').write_text(json.dumps({**packet,'title':'Old alternate'}),encoding='utf-8')
        draft_queue.import_existing(self.store.connection,self.root)
        saved=draft_queue.pending(self.store,PREF)
        self.assertEqual(saved[0][0],path.resolve())
        self.assertEqual(Path(saved[0][1]['letter_path']).read_text(encoding='utf-8'),'Моя редакция')

    def test_batch_prepares_multiple_packets_and_reuses_caches(self):
        self.store.save(vacancy('2'))
        for filename in hunt.prepare.RU_FILES.values():
            (self.profile/filename).write_bytes(b'fixture')
        assessment={'decision':'apply','cv_variant':'applied_ai','summary':'Python подтверждён.',
            'matches':[{'requirement_id':'J001','cv_id':'AP001','reason':'CV','coverage':'full'}],
            'gaps':[],'questions':[]}
        raw=Mock();raw.post.side_effect=[response(assessment),response(assessment),
             response({'cv_variant':'applied_ai','fact_ids':['AI001','AI002']}),
             response({'cv_variant':'applied_ai','fact_ids':['AI001','AI002']})]
        with patch.object(hunt,'ROOT',self.root),patch.object(hunt.assess,'load_profiles',return_value=EN), \
             patch.object(hunt.assess,'read_cv',return_value=RU),contextlib.redirect_stdout(io.StringIO()):
            session=hunt.BudgetSession(raw,.02)
            packets=hunt.prepare_batch(self.store,hunt.eligible(self.store,PREF),model='test',profile_dir=self.profile,
                output=self.root/'drafts/runs/batch',session=session,run=True,budget=.02,preferences=PREF,key='fixture')
            self.assertEqual(len(packets),2);self.assertEqual(session.calls,4)
            self.assertEqual(len(draft_queue.pending(self.store,PREF)),2)
            second=hunt.BudgetSession(raw,.02)
            again=hunt.prepare_batch(self.store,hunt.eligible(self.store,PREF),model='test',profile_dir=self.profile,
                output=self.root/'drafts/runs/again',session=second,run=True,budget=.02,preferences=PREF,key='fixture')
            self.assertEqual(len(again),2);self.assertEqual(second.calls,0)

    def test_menu_preview_and_closed_input_never_open_browser_or_model(self):
        path,_=self.packet();draft_queue.register(self.store.connection,path)
        answers=iter(['0','0'])
        with patch.object(hunter_menu,'ROOT',self.root), \
             patch.object(hunter_menu,'JobStore',side_effect=lambda:JobStore(self.db_path)), \
             patch('requests.Session.post') as post,patch('hh_response.run') as browser, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(hunter_menu.main(lambda _:next(answers)),0)
            self.assertIn('Письмо пользователя',output.getvalue())
            self.assertIn('есть заметные пробелы',output.getvalue())
            self.assertEqual(hunter_menu.main(Mock(side_effect=EOFError)),0)
            browser.assert_not_called();post.assert_not_called()

    def test_queue_command_prepares_all_without_forms_and_skips_ready_on_next_run(self):
        self.store.save(vacancy('2'))
        for filename in hunt.prepare.RU_FILES.values():
            (self.profile/filename).write_bytes(b'fixture')
        assessment={'decision':'apply','cv_variant':'applied_ai','summary':'Python подтверждён.',
            'matches':[{'requirement_id':'J001','cv_id':'AP001','reason':'CV','coverage':'full'}],
            'gaps':[],'questions':[]}
        responses=[response(assessment),response(assessment),
            response({'cv_variant':'applied_ai','fact_ids':['AI001','AI002']}),
            response({'cv_variant':'applied_ai','fact_ids':['AI001','AI002']})]
        with patch.object(hunt,'ROOT',self.root),patch.object(hunt,'LAST',self.root/'data/last.json'), \
             patch.object(hunt,'LAST_PACKET',self.root/'data/packet.json'), \
             patch.object(hunt,'JobStore',side_effect=lambda:JobStore(self.db_path)), \
             patch.object(hunt.assess,'load_profiles',return_value=EN),patch.object(hunt.assess,'read_cv',return_value=RU), \
             patch('browser_check.refresh_saved') as refresh,patch('hh_response.run') as form, \
             patch('requests.Session.post',side_effect=responses) as post, \
             patch.dict(os.environ,{'OPENROUTER_API_KEY':'fixture','OPENROUTER_MODEL':'test'}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hunt.main(['--run','--from-db','--queue']),0)
            self.assertEqual(len(draft_queue.pending(self.store,PREF)),2)
            report=json.loads((self.root/'data/last.json').read_text(encoding='utf-8'))
            self.assertEqual(len(report['packet_paths']),2)
            self.assertEqual(hunt.main(['--run','--from-db','--queue']),0)
            self.assertEqual(post.call_count,4);self.assertEqual(refresh.call_count,1)
            form.assert_not_called()

    def test_menu_dispatches_only_selected_packet_after_review(self):
        path,_=self.packet();draft_queue.register(self.store.connection,path)
        with patch.object(hunter_menu,'ROOT',self.root), \
             patch.object(hunter_menu,'JobStore',side_effect=lambda:JobStore(self.db_path)), \
             patch.object(hunt,'main',return_value=0) as runner,contextlib.redirect_stdout(io.StringIO()):
            # The letter appears on launch; only its explicit action opens the form.
            answers=iter(['о','0'])
            hunter_menu.main(lambda _:next(answers))
            runner.assert_called_once_with(['--run','--resume','--packet',str(path.resolve()),'--send'])

    def test_queue_cli_prepares_ten_letters_without_old_budget_or_three_packet_cap(self):
        for identity in range(2,12):self.store.save(vacancy(str(identity)))
        for filename in hunt.prepare.RU_FILES.values():(self.profile/filename).write_bytes(b'fixture')
        search_settings=__import__('search_settings')
        search_settings.save(self.profile,{'vacancies_per_run':10,'budget_usd':None,'letter_mode':'literal'})
        assessment={'decision':'apply','cv_variant':'applied_ai','summary':'Python подтверждён.',
            'matches':[{'requirement_id':'J001','cv_id':'AP001','reason':'CV','coverage':'full'}],
            'gaps':[],'questions':[]}
        responses=[response(assessment) for _ in range(10)]+[
            response({'cv_variant':'applied_ai','fact_ids':['AI001','AI002']}) for _ in range(10)]
        with patch.object(hunt,'ROOT',self.root),patch.object(hunt,'LAST',self.root/'data/last.json'), \
             patch.object(hunt,'LAST_PACKET',self.root/'data/packet.json'), \
             patch.object(hunt,'JobStore',side_effect=lambda:JobStore(self.db_path)), \
             patch.object(hunt.assess,'load_profiles',return_value=EN),patch.object(hunt.assess,'read_cv',return_value=RU), \
             patch('browser_check.refresh_saved'),patch('hh_response.run') as form, \
             patch('requests.Session.post',side_effect=responses) as post, \
             patch.dict(os.environ,{'OPENROUTER_API_KEY':'fixture','OPENROUTER_MODEL':'test'}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hunt.main(['--run','--from-db','--queue','--limit','10']),0)
        report=json.loads((self.root/'data/last.json').read_text(encoding='utf-8'))
        self.assertEqual(len(report['packet_paths']),10)
        self.assertEqual(len(draft_queue.pending(self.store,PREF)),10)
        self.assertEqual(post.call_count,20)
        self.assertGreater(report['model_usage']['reserved_usd'],.02)
        form.assert_not_called()

    def test_enter_defers_each_letter_once_without_search_or_form(self):
        for identity in ['1','2']:
            self.store.save(vacancy(identity))
            path,_=self.packet(identity);draft_queue.register(self.store.connection,path)
        with patch.object(hunter_menu,'ROOT',self.root), \
             patch.object(hunter_menu,'JobStore',side_effect=lambda:JobStore(self.db_path)), \
             patch.object(hunt,'main') as runner,contextlib.redirect_stdout(io.StringIO()) as output:
            answers=iter(['','','0'])
            self.assertEqual(hunter_menu.main(lambda _:next(answers)),0)
        self.assertEqual(output.getvalue().count('ПИСЬМО\n'),2)
        self.assertNotIn('Номер для просмотра',output.getvalue())
        self.assertEqual(len(draft_queue.pending(self.store,PREF)),2)
        runner.assert_not_called()

    def test_one_search_goes_directly_to_letter_without_stage_prompts(self):
        def prepare(_):
            path,_=self.packet();draft_queue.register(self.store.connection,path)
            return 0
        with patch.object(hunter_menu,'ROOT',self.root), \
             patch.object(hunter_menu,'JobStore',side_effect=lambda:JobStore(self.db_path)), \
             patch.object(hunt,'main',side_effect=prepare) as runner,contextlib.redirect_stdout(io.StringIO()) as output:
            answers=iter(['','','0'])
            self.assertEqual(hunter_menu.main(lambda _:next(answers)),0)
        runner.assert_called_once_with(['--run','--queue'])
        self.assertIn('Письмо пользователя',output.getvalue())

    def test_empty_search_does_not_repeat_automatically(self):
        with patch.object(hunter_menu,'ROOT',self.root), \
             patch.object(hunter_menu,'JobStore',side_effect=lambda:JobStore(self.db_path)), \
             patch.object(hunt,'main',return_value=0) as runner,contextlib.redirect_stdout(io.StringIO()):
            answers=iter(['1','0'])
            hunter_menu.main(lambda _:next(answers))
        runner.assert_called_once_with(['--run','--queue'])

    def test_continue_reuses_ready_letters_without_paid_preparation(self):
        path,_=self.packet();draft_queue.register(self.store.connection,path)
        with patch.object(hunter_menu,'ROOT',self.root), \
             patch.object(hunter_menu,'JobStore',side_effect=lambda:JobStore(self.db_path)), \
             patch.object(hunt,'main') as runner,contextlib.redirect_stdout(io.StringIO()):
            hunter_menu.continue_work(lambda _:'0')
        runner.assert_not_called()

    def test_restart_remembers_display_and_keeps_draft_available(self):
        path,_=self.packet();draft_queue.register(self.store.connection,path)
        with patch.object(hunter_menu,'ROOT',self.root), \
             patch.object(hunter_menu,'JobStore',side_effect=lambda:JobStore(self.db_path)), \
             patch.object(hunt,'main') as runner,contextlib.redirect_stdout(io.StringIO()) as output:
            answers=iter(['0','0'])
            hunter_menu.main(lambda _:next(answers))
            self.assertIn('Письмо пользователя',output.getvalue())
            output.seek(0);output.truncate()
            hunter_menu.main(lambda _:'0')
            self.assertNotIn('Письмо пользователя',output.getvalue())
            answers=iter(['п','0','0'])
            hunter_menu.main(lambda _:next(answers))
            self.assertIn('Письмо пользователя',output.getvalue())
        runner.assert_not_called()
        self.assertEqual(len(draft_queue.pending(self.store,PREF)),1)
        self.assertEqual(self.store.get('1')['status'],'new')

    def test_continue_searches_instead_of_repeating_viewed_draft(self):
        path,packet=self.packet();draft_queue.register(self.store.connection,path)
        draft_queue.mark_reviewed(self.store.connection,path,packet)
        with patch.object(hunter_menu,'ROOT',self.root), \
             patch.object(hunter_menu,'JobStore',side_effect=lambda:JobStore(self.db_path)), \
             patch.object(hunt,'main',return_value=0) as runner,contextlib.redirect_stdout(io.StringIO()) as output:
            answers=iter(['','0'])
            hunter_menu.main(lambda _:next(answers))
        runner.assert_called_once_with(['--run','--queue'])
        self.assertNotIn('Письмо пользователя',output.getvalue())

    def test_review_marker_survives_registration_and_changed_letter_is_new(self):
        path,packet=self.packet();draft_queue.register(self.store.connection,path)
        draft_queue.mark_reviewed(self.store.connection,path,packet)
        draft_queue.register(self.store.connection,path)
        with JobStore(self.db_path) as reopened:
            self.assertEqual(draft_queue.pending(reopened,PREF,unread_only=True),[])
            Path(packet['letter_path']).write_text('Обновлённое письмо',encoding='utf-8')
            self.assertEqual(len(draft_queue.pending(reopened,PREF,unread_only=True)),1)

    def test_compact_settings_combines_reports_without_running_pipeline(self):
        answers=iter(['3','0'])
        with patch.object(hunter_menu,'history') as sent,patch.object(hunter_menu,'coverage_report') as coverage, \
             patch.object(hunt,'main') as runner,contextlib.redirect_stdout(io.StringIO()) as output:
            hunter_menu.more_menu(lambda _:next(answers))
        sent.assert_called_once_with();coverage.assert_called_once_with();runner.assert_not_called()
        self.assertIn('3 — общий отчёт',output.getvalue())
        self.assertNotIn('4 —',output.getvalue())
        self.assertNotIn('подготовить письма по сохранённым',output.getvalue())


class LocalSubmissionTests(unittest.TestCase):
    def test_real_browser_form_submits_once_only_with_exact_confirmation(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as temp,sync_playwright() as p:
            root=Path(temp)
            with JobStore(root/'test.sqlite') as store:
                store.save(vacancy())
                letter=root/'letter.txt';letter.write_text('Моя финальная редакция.',encoding='utf-8')
                cv=root/'cv.docx';cv.write_bytes(b'fixture')
                packet={'source':'hh','vacancy_id':'1','title':'AI Engineer','company':'Example',
                    'url':'https://hh.kz/vacancy/1','letter_path':str(letter),'local_cv_path':str(cv),
                    'output_dir':str(root/'artifacts')}
                browser=p.chromium.launch(headless=True)
                submitted=[]
                def make_context(*args,**kwargs):
                    context=browser.new_context()
                    def route(req):
                        url=req.request.url
                        if '/submit' in url:
                            submitted.append(req.request.post_data)
                            req.fulfill(status=200,body='ok');return
                        if '/applicant/vacancy_response' in url:
                            html='''<meta charset="utf-8"><form>
                            <div data-qa="resume-title">AI/ML Engineer</div>
                            <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
                            <button data-qa="vacancy-response-submit-popup">Откликнуться</button></form>
                            <script>document.querySelector('form').onsubmit=async e=>{
                            e.preventDefault();await fetch('/submit',{method:'POST',body:document.querySelector('textarea').value});
                            document.body.innerHTML='<div>Резюме доставлено</div>';};</script>'''
                        else:
                            html='<a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId=1">Откликнуться</a>'
                        req.fulfill(status=200,content_type='text/html',body=html)
                    context.route('**/*',route)
                    return context
                fake=Mock();fake.__enter__=Mock(return_value=fake);fake.__exit__=Mock(return_value=False)
                fake.chromium.launch_persistent_context.side_effect=make_context
                try:
                    with patch('playwright.sync_api.sync_playwright',return_value=fake), \
                         patch('sys.stdin.isatty',return_value=True),contextlib.redirect_stdout(io.StringIO()):
                        state=hh_response.run(packet,store.connection,allow_send=True,prompt=lambda _:'',profile_dir=root/'profile')
                        self.assertEqual(state,'cancelled_before_send');self.assertEqual(submitted,[])
                        answers=iter(['ОТПРАВИТЬ 1',''])
                        state=hh_response.run(packet,store.connection,allow_send=True,prompt=lambda _:next(answers),profile_dir=root/'profile')
                        self.assertEqual(state,'confirmed_by_site')
                        self.assertEqual(submitted,['Моя финальная редакция.'])
                        self.assertEqual(store.get('1')['status'],'applied')
                        with self.assertRaises(CheckError):
                            hh_response.run(packet,store.connection,allow_send=True,profile_dir=root/'profile')
                        self.assertEqual(len(submitted),1)
                finally:browser.close()


if __name__=='__main__':unittest.main()
