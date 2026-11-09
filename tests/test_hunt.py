"""One-run integration: real assessment + drafting, mocked model and local browser routes."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import requests

import assess
import hunt
import hh_response
from common import CheckError
from storage import JobStore

PREF={'office_city':'Алматы','other_locations':'remote_only','remote_from':'Казахстан','excluded_companies':['Kaspi.kz']}
EN={'ml_engineer':'Evaluated image classification models with PyTorch.',
    'applied_ai':'Developed Python endpoints with FastAPI and RAG retrieval.'}
RU='Разрабатывала FastAPI-эндпоинты и JSON-схемы для сервиса заявок.\nДорабатывала RAG-поиск по документации с индексами SQLite и кэшем эмбеддингов.'


def vacancy(identity='1',company='Example',region='Алматы',description='Python and FastAPI.'):
    return {'id':identity,'name':'AI Engineer (LLM)','employer':{'name':company},'area':{'name':region},
            'alternate_url':f'https://hh.kz/vacancy/{identity}','description':description,'published_at':'2026-10-22'}


def response(data):
    r=requests.Response();r.status_code=200
    r._content=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}],
                           'usage':{'cost':.0001}}).encode();r._content_consumed=True
    return r


class HuntTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.profile=self.root/'profile';self.profile.mkdir()
        self.store=JobStore(self.root/'jobs.sqlite');self.addCleanup(self.store.connection.close)
        self.store.save(vacancy())

    def test_one_run_assesses_and_prepares_then_reuses_both_caches(self):
        assessment={'decision':'apply','cv_variant':'applied_ai','summary':'Подтверждён Python и FastAPI.',
                    'matches':[{'requirement_id':'J001','cv_id':'AP001','reason':'Подтверждено в CV.','coverage':'full'}],
                    'gaps':[],'questions':[]}
        raw=Mock();raw.post.side_effect=[response(assessment),response({'cv_variant':'applied_ai','fact_ids':['AI001','AI002']})]
        session=hunt.BudgetSession(raw,.02)
        output=self.root/'run1';output.mkdir()
        with patch.object(hunt,'ROOT',self.root),patch.object(assess,'load_profiles',return_value=EN), \
             patch.object(assess,'read_cv',return_value=RU),contextlib.redirect_stdout(io.StringIO()):
            packet=hunt.prepare_one(self.store,hunt.eligible(self.store,PREF),model='test',profile_dir=self.profile,
                    output=output,session=session,run=True,budget=.02,preferences=PREF,key='synthetic-key')
            self.assertEqual(session.calls,2)
            self.assertIn('Дорабатывала',Path(packet['letter_path']).read_text(encoding='utf-8'))
            self.assertEqual(packet['vacancy_id'],'1')
            second=hunt.BudgetSession(raw,.02)
            output=self.root/'run2';output.mkdir()
            self.assertIsNotNone(hunt.prepare_one(self.store,hunt.eligible(self.store,PREF),model='test',profile_dir=self.profile,
                    output=output,session=second,run=True,budget=.02,preferences=PREF,key='synthetic-key'))
            self.assertEqual(second.calls,0)
        self.assertEqual(raw.post.call_count,2)
        self.assertEqual(self.store.get('1')['status'],'new')

    def test_total_budget_covers_both_stages_and_failed_requests(self):
        payload={'max_tokens':300,'provider':{'max_price':assess.PRICE_CAP},'messages':[]}
        raw=Mock();raw.post.return_value=response({})
        session=hunt.BudgetSession(raw,.0002)
        session.post('https://openrouter.ai/api/v1/chat/completions',json=payload)
        with self.assertRaises(CheckError):session.post('https://openrouter.ai/api/v1/chat/completions',json=payload)
        self.assertEqual(raw.post.call_count,1)
        raw.post.side_effect=requests.Timeout('synthetic')
        session=hunt.BudgetSession(raw,.02)
        with self.assertRaises(requests.Timeout):session.post('https://openrouter.ai/api/v1/chat/completions',json=payload)
        self.assertEqual(session.summary()['unknown_cost_calls'],1)
        self.assertEqual(session.calls,1)

    def test_filters_apply_before_limit_or_model_and_empty_scope_stays_empty(self):
        self.store.save(vacancy('2','Kaspi.kz'))
        self.store.save(vacancy('3','Other','Астана','Работа полностью из офиса.'))
        self.assertEqual([j['vacancy_id'] for j in hunt.eligible(self.store,PREF,limit=1)],['1'])
        self.assertEqual(hunt.eligible(self.store,PREF,ids=[]),[])

    def test_legacy_unassessed_jobs_recover_once_and_processed_do_not_return(self):
        import discovery,search_settings
        cfg=search_settings.validate({})
        self.store.save(vacancy('2','Kaspi.kz'))
        self.assertEqual(discovery.recover_unassessed(self.store,cfg,PREF),1)
        self.assertEqual(discovery.pending_keys(self.store),[('hh','1')])
        self.assertEqual(discovery.recover_unassessed(self.store,cfg,PREF),0)
        discovery.processed(self.store,[dict(self.store.get('1'))])
        self.assertEqual(discovery.recover_unassessed(self.store,cfg,PREF),0)
        self.assertEqual(discovery.pending_keys(self.store),[])

    def test_legacy_recovery_never_retries_assessed_rejected_or_begun_submissions(self):
        import discovery,search_settings
        self.store.connection.executescript(assess.CACHE_SCHEMA+hh_response.SCHEMA)
        for identity in ['2','3','4']:self.store.save(vacancy(identity))
        with self.store.connection:
            self.store.connection.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?)',
                ('hh','2','cache','test','v1','{}','{}','now'))
            self.store.connection.execute('INSERT INTO rejected_assessments VALUES (?,?,?,?,?,?,?)',
                ('hh','3','cache','{}','{}','invalid','now'))
            self.store.connection.execute('INSERT INTO application_attempts VALUES (?,?,?,?)',
                ('https://hh.kz/vacancy/4','hash','submitted_unverified','now'))
        self.assertEqual(discovery.recover_unassessed(self.store,search_settings.validate({}),PREF),1)
        self.assertEqual(discovery.pending_keys(self.store),[('hh','1')])

    def test_ledger_blocks_already_sent_or_unknown_result(self):
        self.store.connection.executescript(hh_response.SCHEMA)
        self.store.connection.execute('INSERT INTO application_attempts VALUES (?,?,?,?)',
                ('https://hh.kz/vacancy/1','hash','submitted_unverified','now'))
        self.store.connection.commit()
        self.assertEqual(hunt.eligible(self.store,PREF),[])

    def test_submission_requires_current_packet_and_atomic_reservation(self):
        letter=self.root/'letter.txt';letter.write_text('Подтверждённое письмо',encoding='utf-8')
        cv=self.root/'cv.docx';cv.write_bytes(b'local fixture')
        packet={'url':'https://hh.kz/vacancy/1','letter_path':str(letter),'local_cv_path':str(cv)}
        key=hh_response.identity(packet)
        hh_response.reserve(self.store.connection,packet,key)
        letter.write_text('Изменено',encoding='utf-8')
        with self.assertRaises(CheckError):hh_response.begin_submit(self.store.connection,packet,key)
        key=hh_response.identity(packet);hh_response.reserve(self.store.connection,packet,key)
        hh_response.begin_submit(self.store.connection,packet,key)
        with self.assertRaises(CheckError):hh_response.begin_submit(self.store.connection,packet,key)
        with self.assertRaises(CheckError):hh_response.reserve(self.store.connection,packet,key)

    def test_no_network_in_preview_and_no_browser_for_already_sent_resume(self):
        with patch.object(hunt,'ROOT',self.root),patch.object(hunt,'DB_PATH',self.root/'jobs.sqlite'), \
             patch.object(assess,'load_profiles',return_value=EN),patch.object(assess,'read_cv',return_value=RU), \
             patch('requests.Session.post') as post,patch('hh_response.run') as browser, \
             contextlib.redirect_stdout(io.StringIO()):
            # Preview uses the existing store via a patched factory, preserving its ownership here.
            with patch.object(hunt,'JobStore') as factory:
                factory.return_value.__enter__.return_value=self.store
                self.assertEqual(hunt.main(['--from-db']),0)
            post.assert_not_called();browser.assert_not_called()

    def test_cross_host_or_wrong_vacancy_response_link_is_rejected(self):
        for href in ['https://evil.example/applicant/vacancy_response?vacancyId=1',
                     '/applicant/vacancy_response?vacancyId=2','javascript:alert(1)',None]:
            with self.subTest(href=href),self.assertRaises(CheckError):
                hh_response.response_url('https://hh.kz/vacancy/1',href,'1')
        self.assertIn('almaty.hh.kz',hh_response.response_url('https://almaty.hh.kz/vacancy/1',
                      '/applicant/vacancy_response?vacancyId=1','1'))


class HHBrowserTests(unittest.TestCase):
    def test_submission_receipt_handles_hh_spacing_and_rejects_other_text(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                page=browser.new_page()
                for text in ['Вы\u00a0откликнулись', 'Резюме доставлено', 'Отклик успешно отправлен!']:
                    page.set_content('<div>'+text+'</div>')
                    self.assertTrue(hh_response.success_marker(page).first.is_visible())
                for text in ['Откликнуться', 'Отклик не отправлен', 'Резюме не доставлено']:
                    page.set_content('<div>'+text+'</div>')
                    self.assertEqual(hh_response.success_marker(page).count(),0)
            finally:browser.close()

    def test_local_routes_fill_letter_without_clicking_submit_and_stop_on_questions(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as temp,sync_playwright() as p:
            root=Path(temp);letter=root/'letter.txt';letter.write_text('Мой проверенный факт.',encoding='utf-8')
            packet={'vacancy_id':'1','url':'https://hh.kz/vacancy/1','letter_path':str(letter)}
            browser=p.chromium.launch(headless=True)
            try:
                page=browser.new_page()
                extra=['']
                vacancy_html='<a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId=1">Откликнуться</a>'
                already=[False]
                expanded=[False]
                def route(req):
                    if '/applicant/vacancy_response' not in req.request.url:
                        req.fulfill(status=200,content_type='text/html',body='<meta charset="utf-8">'+vacancy_html+
                                    ('<div>Вы откликнулись</div>' if already[0] else ''));return
                    req.fulfill(status=200,content_type='text/html',body='''<meta charset="utf-8"><form>
                    <div data-qa="resume-title">AI/ML Engineer</div>
                    '''+('' if expanded[0] else '''<div data-qa="vacancy-response-letter-toggle" onclick="document.querySelector('textarea').style.display='block'">Добавить</div>''')+'''
                    <textarea style="display:'''+('block' if expanded[0] else 'none')+'''" data-qa="vacancy-response-popup-form-letter-input"></textarea>'''+extra[0]+'''
                    <button type="submit" data-qa="vacancy-response-submit-popup">Откликнуться</button></form>
                    <script>window.submitCount=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submitCount++;}</script>''')
                page.route('**/*',route)
                self.assertEqual(hh_response.prepare_page(page,packet),'AI/ML Engineer')
                self.assertEqual(page.locator(hh_response.LETTER).input_value(),'Мой проверенный факт.')
                self.assertEqual(page.evaluate('window.submitCount'),0)
                expanded[0]=True
                self.assertEqual(hh_response.prepare_page(page,packet),'AI/ML Engineer')
                extra[0]='<input name="question" placeholder="Ваш ответ">'
                with self.assertRaisesRegex(CheckError,'дополнительные вопросы'):
                    hh_response.prepare_page(page,packet)
                self.assertEqual(page.evaluate('window.submitCount'),0)
                already[0]=True
                with self.assertRaises(hh_response.AlreadyApplied):hh_response.prepare_page(page,packet)
            finally:browser.close()


if __name__=='__main__':unittest.main()
