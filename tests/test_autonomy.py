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


def error_response(status,message):
    r=requests.Response();r.status_code=status;r._content_consumed=True
    r._content=json.dumps({'error':{'message':message,'code':status}}).encode()
    return r


RESEARCH={'status':'vacancy_only','sources':[],'fragments':{},'company':'Example','note':'По задачам вакансии.',
          'retrieved_at':'2026-10-23T13:00:00+00:00'}
DRAFT={'cv_variant':'ml_engineer','fact_ids':['ML001','ML002'],
    'opening':{'text':'Мне интересны ваши задачи по обучению и оценке моделей: хочу работать над их качеством на реальных данных.',
               'job_ids':['J001'],'company_ids':[]},
    'experience':[{'text':'В работе я оценивала модели классификации изображений с учётом качества разметки и участвовала в развёртывании инференса PyTorch в сервис.',
                   'fact_ids':['ML001','ML002']}],
    'closing':'Буду рада обсудить задачи команды и подробнее рассказать о своём опыте.'}
# The automatic rewrite after AUDIT_BAD: the unsupported claim is gone.
REVISED={**DRAFT,'experience':[{'text':'В работе я оценивала модели классификации изображений с учётом качества разметки и работала над подготовкой данных для оценки.',
                                'fact_ids':['ML001','ML002']}]}


AUDIT_OK={'findings':[]}
# Quote really present in DRAFT and judged unsupported: a genuine grounding failure.
AUDIT_BAD={'findings':[{'quote':'участвовала в развёртывании инференса PyTorch в сервис','verdict':'unsupported',
    'explanation':'Недоказанный опыт.'}]}


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

    def test_web_search_does_not_force_tool_choice_that_cheap_providers_reject(self):
        # 05.11.2026+: 'required' left zero endpoints under PRICE_CAP -> HTTP 404 on every query.
        session=Mock();session.post.return_value=response()
        web_search.search_query('q',session=session,key='fixture',model='test')
        self.assertIn(session.post.call_args.kwargs['json'].get('tool_choice'),(None,'auto'))

    def test_web_search_error_keeps_provider_message_without_secrets(self):
        session=Mock();session.post.return_value=error_response(404,"No endpoints found that support the provided 'tool_choice' value.")
        with self.assertRaises(CheckError) as caught:
            web_search.search_query('q',session=session,key='secret-key-value',model='test')
        self.assertIn('HTTP 404',str(caught.exception));self.assertIn('tool_choice',str(caught.exception))
        self.assertNotIn('secret-key-value',str(caught.exception))

    def test_price_cap_error_is_explained_in_plain_russian(self):
        # 13.11.2026: providers raised the model prices; every request got this English 404 in the Telegram summary.
        import model_api
        text='No endpoints found that satisfy the max price for this request.'
        session=Mock();session.post.return_value=error_response(404,text)
        with self.assertRaises(CheckError) as caught:
            web_search.search_query('q',session=session,key='fixture',model='test')
        self.assertIn('лимит цены',str(caught.exception))
        session.post.return_value=error_response(404,text)
        with self.assertRaises(model_api.ModelUnavailable) as caught:
            model_api.request_json(session,{'model':'m','messages':[]},'fixture',lambda body:body)
        self.assertIn('лимит цены',str(caught.exception))

    def test_price_cap_admits_cheapest_providers_of_13_11_2026(self):
        # USD per million tokens from the OpenRouter endpoints list of the model.
        import assess
        cap=assess.PRICE_CAP
        admitted=[name for name,prompt,completion in [('Morph',.04,.60),('Wafer',.051,.567),('River',.12,.48),
                                                      ('DeepInfra',.14,.42),('Relace',.12,.50)]
                  if prompt<=cap['prompt'] and completion<=cap['completion']]
        self.assertGreaterEqual(len(admitted),4)
        self.assertLessEqual(cap['prompt'],.20);self.assertLessEqual(cap['completion'],.80)

    def test_model_request_error_keeps_provider_message(self):
        import model_api
        session=Mock();session.post.return_value=error_response(402,'Insufficient credits')
        with self.assertRaises(model_api.ModelUnavailable) as caught:
            model_api.request_json(session,{},'fixture',lambda raw:raw)
        self.assertIn('HTTP 402',str(caught.exception));self.assertIn('Insufficient credits',str(caught.exception))

    def test_web_refresh_stops_series_after_configuration_error(self):
        session=Mock();session.post.return_value=error_response(404,'No endpoints found')
        with patch.object(web_search.sources,'web_plan',return_value=['q1','q2','q3']), \
             contextlib.redirect_stdout(io.StringIO()):
            report=web_search.refresh({},self.root/'web.json',session=session,key='fixture',model='test')
        self.assertEqual(session.post.call_count,1)
        self.assertEqual([q['status'] for q in report['queries']],['error','skipped','skipped'])

    def test_web_refresh_continues_after_transient_error(self):
        session=Mock();session.post.side_effect=[error_response(429,'Rate limited'),response()]
        with patch.object(web_search.sources,'web_plan',return_value=['q1','q2']), \
             contextlib.redirect_stdout(io.StringIO()):
            report=web_search.refresh({},self.root/'web.json',session=session,key='fixture',model='test')
        self.assertEqual([q['status'] for q in report['queries']],['error','ok'])

    def make(self,session,job=None):
        with patch('company_research.research',return_value=RESEARCH),contextlib.redirect_stdout(io.StringIO()):
            return personalized.prepare_letter(job or JOB,PROFILES,ASSESSMENT,'test',profile_dir=self.root/'profile',
                output_dir=self.root/'drafts',run=True,budget=1,key='fixture',session=session)

    def test_personal_letter_paraphrases_supported_experience_and_preserves_user_edit(self):
        session=Mock();session.post.side_effect=[response(DRAFT),response(AUDIT_OK)]
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

    def test_paid_letter_survives_crash_between_record_and_letter_file(self):
        # Audit A35: personalized.json «valid» is written before letter.txt; a crash in between lost the paid letter
        # and the retry answered «вариант уже отклонён».
        session=Mock();session.post.side_effect=[response(DRAFT),response(AUDIT_OK)]
        folder=self.make(session)
        expected=(folder/'letter.txt').read_text(encoding='utf-8')
        (folder/'letter.txt').unlink();(folder/'research.json').unlink()
        self.assertEqual(self.make(session),folder)
        self.assertTrue((folder/'research.json').is_file())
        # The real retry path (hunt --retry-key) adds a revision, i.e. a new identity: the valid letter is still reused.
        (folder/'letter.txt').unlink()
        retried=self.make(session,{**JOB,'_letter_revision':'20261113T120000Z'})
        self.assertEqual((retried/'letter.txt').read_text(encoding='utf-8'),expected)
        self.assertEqual(session.post.call_count,2)
        self.assertEqual((folder/'letter.txt').read_text(encoding='utf-8'),expected)
        self.assertEqual(session.post.call_count,2)

    def test_invalid_numbers_unknown_company_source_and_fake_motivation_are_rejected(self):
        catalogs=__import__('prepare').fact_catalog(PROFILES)
        for kind in ['number','company','motive']:
            draft=copy.deepcopy(DRAFT)
            if kind=='number':draft['experience'][0]['text']+=' У меня 10 лет опыта.'
            if kind=='company':draft['opening']['company_ids']=['C999']
            if kind=='motive':draft['opening']['text']='Я давно мечтала работать именно у вас.'
            with self.subTest(kind=kind),self.assertRaises(CheckError):personalized.validate(draft,catalogs,{'J001':'Обучать модели.'},{})

    def test_numbers_in_opening_and_closing_are_checked_too(self):
        # Audit A5: «3 года production-опыта» in the opening passed; only experience paragraphs were checked.
        catalogs=__import__('prepare').fact_catalog(PROFILES)
        for part in ['opening','closing']:
            draft=copy.deepcopy(DRAFT)
            if part=='opening':draft['opening']['text']+=' У меня 3 года production-опыта.'
            else:draft['closing']='Повысила точность на 25% и готова обсудить задачи.'
            with self.subTest(part=part),self.assertRaises(CheckError):personalized.validate(draft,catalogs,{'J001':'Обучать модели.'},{})
        # A number taken from the vacancy itself is not a claim about the candidate's record.
        draft=copy.deepcopy(DRAFT);draft['opening']['text']+=' Интересна задача сократить задержку до 200 мс.'
        personalized.validate(draft,catalogs,{'J001':'Сократить задержку ответа до 200 мс.'},{})

    def test_finding_whose_own_explanation_says_no_error_does_not_reject(self):
        letter='Здравствуйте!\n\nДелала проверку логов и ручную оценку ответов чат-бота.\n'
        quote='Делала проверку логов и ручную оценку ответов чат-бота'
        # Real Kaspi audit of 09.11.2026: verdict «unsupported», explanation ends «существенной ошибки нет».
        raw={'findings':[{'quote':quote,'verdict':'unsupported','explanation':'AI013 подтверждает этот факт, но в fact_ids '
             'письма указаны только AI005, AI008, AI012. Само содержание подтверждено AI013, поэтому существенной ошибки нет.'}]}
        self.assertEqual(len(personalized.audit_verdict(raw,letter)),1)
        raw={'findings':[{'quote':quote,'verdict':'unsupported','explanation':'Это допустимое обобщение, ошибки нет; '
             'однако оценка чат-бота не подтверждена ни одним фактом.'}]}
        with self.assertRaises(CheckError):personalized.audit_verdict(raw,letter)

    def test_writer_prompt_forbids_the_claims_audits_keep_rejecting(self):
        prompt=personalized.SYSTEM_PROMPT
        for phrase in ['важно для компании','это дало понимание','недавно','fact_ids этого абзаца']:
            self.assertIn(phrase,prompt)
        self.assertEqual(personalized.VERSION,'personal-letter-v5')

    def test_rejected_letter_is_revised_once_with_the_audit_findings(self):
        # User decision 10.11.2026: one automatic rewrite after a failed fact check, then a new check.
        session=Mock();session.post.side_effect=[response(DRAFT),response(AUDIT_BAD),response(REVISED),response(AUDIT_OK)]
        folder=self.make(session)
        self.assertEqual(session.post.call_count,4)
        text=(folder/'letter.txt').read_text(encoding='utf-8')
        self.assertIn(REVISED['experience'][0]['text'],text);self.assertNotIn('развёртывании инференса',text)
        rewrite=session.post.call_args_list[2].kwargs['json']['messages']
        self.assertIn('участвовала в развёртывании инференса PyTorch в сервис',rewrite[-1]['content'])
        self.assertEqual(rewrite[-2]['role'],'assistant')
        record=self.last_record()
        self.assertEqual(record['status'],'valid');self.assertEqual(record['revision']['findings'][0]['quote'],AUDIT_BAD['findings'][0]['quote'])

    def test_revision_rejected_again_is_final_without_more_paid_calls(self):
        again={'findings':[{'quote':'работала над подготовкой данных для оценки','verdict':'unsupported','explanation':'Нет в CV.'}]}
        session=Mock();session.post.side_effect=[response(DRAFT),response(AUDIT_BAD),response(REVISED),response(again)]
        with self.assertRaises(CheckError) as caught:self.make(session)
        self.assertIn('работала над подготовкой данных',str(caught.exception))
        with self.assertRaises(CheckError):self.make(session)
        self.assertEqual(session.post.call_count,4)
        self.assertEqual(list(self.root.rglob('letter.txt')),[])
        self.assertEqual(self.last_record()['status'],'rejected')

    def test_outage_during_rewrite_keeps_the_rejection_not_a_retryable_failure(self):
        import requests
        session=Mock();session.post.side_effect=[response(DRAFT),response(AUDIT_BAD),requests.ConnectionError('down')]
        with self.assertRaises(CheckError):self.make(session)
        self.assertEqual(self.last_record()['status'],'rejected')

    def last_record(self):
        return json.loads(max(self.root.rglob('personalized.json'),key=lambda p:p.stat().st_mtime).read_text(encoding='utf-8'))

    def test_audit_comments_confirming_claims_do_not_reject_letter(self):
        # Kolesa 139110453 / Platvix: the auditor listed only confirmations, the old rule rejected anyway.
        audit={'findings':[{'quote':'оценивала модели классификации изображений с учётом качества разметки','verdict':'supported',
            'explanation':'Соответствует ML001, допустимое обобщение.'}]}
        session=Mock();session.post.side_effect=[response(DRAFT),response(audit)]
        folder=self.make(session)
        self.assertTrue((folder/'letter.txt').exists())
        self.assertEqual(self.last_record()['status'],'valid')

    def test_unsupported_claim_quoted_from_letter_rejects_it(self):
        session=Mock();session.post.side_effect=[response(DRAFT),response(AUDIT_BAD),response(DRAFT),response(AUDIT_BAD)]
        with self.assertRaises(CheckError) as caught:self.make(session)
        self.assertIn('участвовала в развёртывании инференса PyTorch',str(caught.exception))
        record=self.last_record()
        self.assertEqual(record['status'],'rejected');self.assertEqual(record['audit_model'],personalized.AUDIT_MODEL)

    def test_auditor_finding_about_text_absent_from_letter_is_ignored_and_kept(self):
        # Kaspi 138894205: the auditor complained about a "UNIX projects" paragraph the letter does not contain.
        audit={'findings':[{'quote':'в абзаце про UNIX-подобные проекты','verdict':'unsupported','explanation':'Нет в CV.'}]}
        session=Mock();session.post.side_effect=[response(DRAFT),response(audit)]
        self.make(session)
        record=self.last_record()
        self.assertEqual(record['status'],'valid');self.assertEqual(len(record['ignored_findings']),1)

    def test_letter_uses_cv_variant_chosen_by_assessment(self):
        session=Mock();session.post.side_effect=[response({**DRAFT,'cv_variant':'applied_ai','fact_ids':['AI001','AI002'],
            'experience':[{**DRAFT['experience'][0],'fact_ids':['AI001','AI002']}]}),response(AUDIT_OK)]
        job={**JOB,'title':'ML Engineer','description_text':'Разрабатывать RAG и LLM-сервисы.'}
        assessment={**ASSESSMENT,'cv_variant':'applied_ai','matches':[{'requirement_quote':'RAG','cv_quote':'RAG-поиск','reason':'Есть RAG.'}]}
        with patch('company_research.research',return_value=RESEARCH),contextlib.redirect_stdout(io.StringIO()):
            personalized.prepare_letter(job,PROFILES,assessment,'test',profile_dir=self.root/'profile',
                output_dir=self.root/'drafts',run=True,budget=1,key='fixture',session=session)
        sent=json.loads(session.post.call_args_list[0].kwargs['json']['messages'][1]['content'])
        self.assertEqual(list(sent['cv_facts']),['applied_ai'])
        self.assertEqual(sent['assessment']['matches'][0]['reason'],'Есть RAG.')

    def test_data_scientist_vacancy_about_rag_keeps_rag_facts(self):
        # Parametric 138879721: title "Data Scientist", tasks about RAG/LLM agents.
        job={**JOB,'title':'Data Scientist','description_text':'Внедрение RAG по корпоративным документам и интеграции LLM.'}
        catalogs={'applied_ai':{'AI001':'Дорабатывала RAG-поиск по документации с индексами SQLite.',
            'AI002':'Дообучала модель на собранном датасете.','AI003':'Оценивала качество моделей PyTorch.'}}
        self.assertIn('AI001',__import__('prepare').relevant_facts(job,catalogs)['applied_ai'])
        classic={**job,'description_text':'Строить модели прогнозирования спроса на табличных данных.'}
        self.assertNotIn('AI001',__import__('prepare').relevant_facts(classic,catalogs)['applied_ai'])

    def test_audit_outage_reuses_paid_draft_on_explicit_retry(self):
        session=Mock();session.post.side_effect=[response(DRAFT),error_response(503,'Provider unavailable')]
        with self.assertRaises(CheckError):self.make(session,{**JOB,'_letter_revision':'first'})
        with self.assertRaises(CheckError) as caught:self.make(session,{**JOB,'_letter_revision':'first'})
        self.assertNotIn('отклон',str(caught.exception));self.assertIn('из-за сбоя',str(caught.exception))
        session.post.side_effect=[response(AUDIT_OK)]
        folder=self.make(session,{**JOB,'_letter_revision':'second'})
        self.assertEqual(session.post.call_count,3)  # draft + failed audit + one new audit, no new draft
        self.assertIn(DRAFT['opening']['text'],(folder/'letter.txt').read_text(encoding='utf-8'))

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
        session=Mock();session.post.side_effect=[response(DRAFT),response(AUDIT_OK),
            response(DRAFT),response(AUDIT_OK)]
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
