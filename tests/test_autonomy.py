"""Independent search proofs and grounded, nonliteral cover letters."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import requests

from common import CheckError
import personalized
import web_search
import company_research
import scheduled_runner
from test_prepare import PROFILES,JOB,ASSESSMENT


def response(content=None,annotations=None,searches=1):
    r=requests.Response();r.status_code=200;r._content_consumed=True
    r._content=json.dumps({'choices':[{'finish_reason':'stop','message':{
        'content':json.dumps(content or {}),'annotations':annotations or []}}],
        'usage':{'cost':.001,'server_tool_use_details':{'web_search_requests':searches}}}).encode()
    return r


RESEARCH={'status':'vacancy_only','sources':[],'fragments':{},'company':'Example','note':'По задачам вакансии.',
          'retrieved_at':'2026-10-23T13:00:00+00:00'}
DRAFT={'cv_variant':'ml_engineer','fact_ids':['ML001','ML002'],
    'opening':{'text':'Мне интересны ваши задачи по обучению и оценке моделей: хочу работать над их качеством на реальных данных.',
               'job_ids':['J001'],'company_ids':[]},
    'experience':[{'text':'В работе я оценивала модели классификации изображений с учётом качества разметки и участвовала в развёртывании инференса PyTorch в сервис.',
                   'fact_ids':['ML001','ML002']}],
    'closing':'Буду рада обсудить задачи команды и подробнее рассказать о своём опыте.'}


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_web_search_uses_only_engine_citations_and_requires_actual_search(self):
        session=Mock();session.post.return_value=response({'urls':['https://invented.example/job']},[
            {'type':'url_citation','url_citation':{'url':'https://company.example/job?utm_source=x','title':'AI Engineer','content':'Snippet'}}])
        result=web_search.search_query('exact query',session=session,key='fixture',model='test')
        self.assertEqual([h['url'] for h in result['hits']],['https://company.example/job'])
        payload=session.post.call_args.kwargs['json']
        self.assertEqual(payload['tools'][0]['parameters']['max_uses'],1)
        self.assertEqual(payload['messages'][1]['content'],'exact query')
        session.post.return_value=response({'urls':['https://invented.example/job']},searches=0)
        with self.assertRaises(CheckError):web_search.search_query('exact query',session=session,key='fixture',model='test')

    def test_search_cache_avoids_paid_repeat_and_query_failures_are_honest(self):
        session=Mock();session.post.return_value=response()
        first=web_search.cached_query('query',session=session,key='fixture',model='test',cache_dir=self.root)
        second=web_search.cached_query('query',session=session,key='fixture',model='test',cache_dir=self.root)
        self.assertFalse(first['cached']);self.assertTrue(second['cached']);self.assertEqual(session.post.call_count,1)
        with patch.object(web_search.sources,'web_plan',return_value=['q1','q2']), \
             patch.object(web_search,'cached_query',side_effect=[CheckError('unavailable'),{**first,'hits':[]}]), \
             contextlib.redirect_stdout(io.StringIO()):
            report=web_search.refresh({},self.root/'web.json',session=session,key='fixture',model='test')
        self.assertEqual([q['status'] for q in report['queries']],['error','ok'])

    def make(self,session,job=None):
        with patch('company_research.research',return_value=RESEARCH),contextlib.redirect_stdout(io.StringIO()):
            return personalized.prepare_letter(job or JOB,PROFILES,ASSESSMENT,'test',profile_dir=self.root/'profile',
                output_dir=self.root/'drafts',run=True,budget=1,key='fixture',session=session)

    def test_personal_letter_paraphrases_supported_experience_and_preserves_user_edit(self):
        session=Mock();session.post.side_effect=[response(DRAFT),response({'valid':True,'issues':[]})]
        folder=self.make(session)
        text=(folder/'letter.txt').read_text(encoding='utf-8')
        self.assertIn(DRAFT['opening']['text'],text)
        self.assertNotIn(PROFILES['ml_engineer'].splitlines()[0],text)
        (folder/'letter.txt').write_text('Моя редакция',encoding='utf-8')
        self.assertEqual(self.make(session),folder)
        self.assertEqual((folder/'letter.txt').read_text(encoding='utf-8'),'Моя редакция')
        self.assertEqual(session.post.call_count,2)
        audit_payload=session.post.call_args.kwargs['json']
        self.assertNotIn('temperature',audit_payload)
        self.assertEqual(audit_payload['reasoning'],{'effort':'low'})

    def test_invalid_numbers_unknown_company_source_and_fake_motivation_are_rejected(self):
        catalogs=__import__('prepare').fact_catalog(PROFILES)
        for kind in ['number','company','motive']:
            draft=copy.deepcopy(DRAFT)
            if kind=='number':draft['experience'][0]['text']+=' У меня 10 лет опыта.'
            if kind=='company':draft['opening']['company_ids']=['C999']
            if kind=='motive':draft['opening']['text']='Я давно мечтала работать именно у вас.'
            with self.subTest(kind=kind),self.assertRaises(CheckError):personalized.validate(draft,catalogs,{'J001':'Обучать модели.'},{})

    def test_grounding_failure_is_recorded_without_automatic_paid_retry(self):
        session=Mock();session.post.side_effect=[response(DRAFT),response({'valid':False,'issues':['Недоказанный опыт.']})]
        with self.assertRaises(CheckError):self.make(session)
        with self.assertRaises(CheckError):self.make(session)
        self.assertEqual(session.post.call_count,2)
        self.assertEqual(list(self.root.rglob('letter.txt')),[])
        record=json.loads(next(self.root.rglob('personalized.json')).read_text(encoding='utf-8'))
        self.assertEqual(record['status'],'rejected')

    def test_preview_never_researches_company_or_calls_model(self):
        session=Mock()
        with patch('company_research.research') as research,contextlib.redirect_stdout(io.StringIO()):
            result=personalized.prepare_letter(JOB,PROFILES,ASSESSMENT,'test',profile_dir=self.root/'profile',
                output_dir=self.root/'drafts',run=False,budget=1,key='fixture',session=session)
        self.assertIsNone(result);research.assert_not_called();session.post.assert_not_called()

    def test_third_party_company_mentions_are_not_official_sources(self):
        self.assertFalse(company_research.first_party('https://news.example/story','Acme','Acme company announcement'))
        self.assertFalse(company_research.first_party('https://jobs.ashbyhq.com/acme','Acme'))
        self.assertTrue(company_research.first_party('https://acme.com/about','Acme'))

    def test_explicit_new_revision_preserves_old_user_edited_file(self):
        session=Mock();session.post.side_effect=[response(DRAFT),response({'valid':True,'issues':[]}),
            response(DRAFT),response({'valid':True,'issues':[]})]
        first=self.make(session,{**JOB,'_letter_revision':'first'})
        (first/'letter.txt').write_text('Моя редакция',encoding='utf-8')
        second=self.make(session,{**JOB,'_letter_revision':'second'})
        self.assertNotEqual(first,second)
        self.assertEqual((first/'letter.txt').read_text(encoding='utf-8'),'Моя редакция')

    def test_native_runner_duplicate_window_does_not_notify_again(self):
        data=self.root/'data';data.mkdir()
        state=data/'last_scheduled_run.json'
        state.write_text(json.dumps({'started_at':'old','prepared_count':3}),encoding='utf-8')
        with patch.object(scheduled_runner,'ROOT',self.root),patch('scheduled_run.main',return_value=0), \
             patch('subprocess.Popen') as notify:
            self.assertEqual(scheduled_runner.main(),0)
        notify.assert_not_called()
        saved=json.loads((data/'last_scheduler_invocation.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['prepared_count'],0)

    def test_native_runner_new_letters_notify_locally_without_application_send(self):
        data=self.root/'data';data.mkdir()
        def run():
            (data/'last_scheduled_run.json').write_text(json.dumps({'started_at':'new','prepared_count':2}),encoding='utf-8')
            return 0
        with patch.object(scheduled_runner,'ROOT',self.root),patch('scheduled_run.main',side_effect=run), \
             patch('subprocess.Popen') as notify:
            self.assertEqual(scheduled_runner.main(),0)
        args=notify.call_args.args[0]
        self.assertIn('2',args);self.assertNotIn('--send',args)


if __name__=='__main__':unittest.main()
