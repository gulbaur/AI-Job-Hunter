"""Offline acceptance tests for public sources, coverage and source isolation."""
from datetime import datetime,timezone,timedelta
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch

import discovery
import draft_queue
import hunt
import prepare
import search_settings
import sources
from common import CheckError
from storage import JobStore
from test_hunt import vacancy,PREF,response

BODY='Develop and evaluate machine learning models with Python, design tests and production APIs. '*4


class SourceTests(unittest.TestCase):
    def test_canonical_url_removes_tracking_but_keeps_job_identity(self):
        self.assertEqual(sources.canonical_url('https://example.com/jobs?id=12&utm_source=x#apply'),'https://example.com/jobs?id=12')
        for url in ['file:///tmp/a','http://example.com','https://u:p@example.com','https://example.com:3000/']:
            with self.assertRaises(CheckError):sources.canonical_url(url)

    def test_private_address_and_redirect_are_rejected_before_second_request(self):
        with patch('socket.getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',443))]):
            with self.assertRaises(CheckError):sources.public_url('https://example.com/')
        first=Mock();first.__enter__=Mock(return_value=first);first.__exit__=Mock(return_value=False)
        first.status_code=302;first.headers={'Location':'http://127.0.0.1/'}
        session=Mock();session.get.return_value=first
        with patch('socket.getaddrinfo',return_value=[(None,None,None,None,('8.8.8.8',443))]):
            with self.assertRaises(CheckError):sources.PublicClient(session).get('https://example.com/')
        self.assertEqual(session.get.call_count,1)

    def test_all_three_public_boards_normalize_full_descriptions(self):
        fixtures={
            'ashby':{'jobs':[{'title':'AI Engineer','jobUrl':'https://jobs.example.com/1','location':'Almaty',
                            'descriptionHtml':BODY,'isRemote':False,'isListed':True}]},
            'greenhouse':{'jobs':[{'title':'ML Engineer','absolute_url':'https://jobs.example.com/2',
                                  'location':{'name':'Worldwide'},'content':BODY}]},
            'lever':[{'text':'LLM Engineer','hostedUrl':'https://jobs.example.com/3',
                      'categories':{'location':'Almaty'},'description':BODY,'lists':[]}]}
        for kind,payload in fixtures.items():
            client=Mock();client.json.return_value=payload
            items=list(sources.board_jobs(client,{'kind':kind,'slug':'example','name':'Example'}))
            self.assertEqual(len(items),1);self.assertIn(BODY,items[0]['description'])
            self.assertTrue(items[0]['id'].isdigit())

    def test_unlisted_or_empty_board_description_is_not_invented(self):
        client=Mock();client.json.return_value={'jobs':[{'isListed':False}]}
        self.assertEqual(list(sources.board_jobs(client,{'kind':'ashby','slug':'example','name':'Example'})),[])
        client.json.return_value={'jobs':[{'title':'AI Engineer','jobUrl':'https://example.com/1'}]}
        with self.assertRaises(CheckError):list(sources.board_jobs(client,{'kind':'ashby','slug':'example','name':'Example'}))

    def test_jobposting_graph_remote_and_expiry_are_respected(self):
        item={'@type':'JobPosting','title':'AI Engineer','hiringOrganization':{'name':'Example'},
              'description':BODY,'jobLocationType':'TELECOMMUTE',
              'applicantLocationRequirements':{'@type':'Country','name':'Kazakhstan'}}
        client=Mock();client.page.return_value=('https://example.com/job',
                    '<script type="application/ld+json">'+json.dumps({'@graph':[item]})+'</script>')
        job=list(sources.page_jobs(client,'https://example.com/job'))[0]
        self.assertIn('Kazakhstan',job['description']);self.assertIn('Remote',job['description'])
        item['validThrough']='2000-01-01T00:00:00Z'
        client.page.return_value=('https://example.com/job','<script type="application/ld+json">'+json.dumps(item)+'</script>')
        self.assertEqual(list(sources.page_jobs(client,'https://example.com/job')),[])

    def test_remote_only_is_not_global_eligibility(self):
        for description,state in [('Remote. US only.','blocked'),('Remote. Worldwide.','remote_review'),
                                  ('Remote.','unconfirmed')]:
            self.assertEqual(sources.external_location({'region':'','description_text':description},PREF)[0],state)

    def test_custom_role_reaches_hh_and_title_filter_without_new_builtin_roles(self):
        import browser_check
        settings=search_settings.validate({'role_groups':[],'custom_roles':['Speech Research Engineer']})
        plan=browser_check.search_plan(junior=True,expanded=True,pages=1,settings=settings)
        self.assertEqual(len(plan),1)
        self.assertIn('Speech',plan[0][1])
        self.assertTrue(search_settings.relevant('Speech Research Engineer',settings))
        self.assertFalse(search_settings.relevant('Director of Speech Research Engineer',settings))
        queries=sources.web_plan(settings)
        self.assertEqual(len(queries),6)
        self.assertTrue(all('Speech Research Engineer' in q for q in queries))
        self.assertTrue(all('Data Scientist' not in q for q in queries))

    def test_single_job_visible_body_fills_abbreviated_structured_description(self):
        item={'@type':'JobPosting','title':'AI Engineer','hiringOrganization':{'name':'Example'},
              'description':BODY,'jobLocationType':'TELECOMMUTE'}
        html='<script type="application/ld+json">'+json.dumps(item)+'</script><main><h1>AI Engineer</h1><p>Location: Remote, worldwide.</p><p>'+BODY*2+'</p></main>'
        client=Mock();client.page.return_value=('https://example.com/job',html)
        result=list(sources.page_jobs(client,'https://example.com/job'))[0]
        self.assertIn('Remote, worldwide',result['description'])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=JobStore(self.root/'jobs.sqlite');self.addCleanup(self.store.connection.close)
        self.settings=search_settings.validate({})

    def web(self,links,**overrides):
        queries=sources.web_plan(self.settings)
        data={'generated_at':datetime.now(timezone.utc).isoformat(),
              'plan_hash':hashlib.sha256(json.dumps(queries,ensure_ascii=False).encode()).hexdigest(),
              'queries':[{'q':q,'status':'ok'} for q in queries],
              'candidates':[{'url':url} for url in links]}
        data.update(overrides);path=self.root/'web.json';discovery.write_json(path,data);return path

    def test_failed_board_does_not_hide_other_sources_and_linkedin_is_not_fetched(self):
        item=sources.vacancy('https://company.example/job','AI Engineer','Example','Алматы',BODY)
        def boards(client,board):
            if board['slug']=='bad':raise CheckError('not available')
            return iter([item])
        client=Mock()
        config=sources.validate({'enabled':['companies','web'],'boards':[
            {'kind':'ashby','slug':'bad','name':'Bad'}, {'kind':'ashby','slug':'good','name':'Good'}], 'career_pages':[]})
        with patch.object(sources,'board_jobs',side_effect=boards):
            report=discovery.collect(self.store,self.settings,PREF,config=config,client=client,include_hh=False,
                web_path=self.web(['https://www.linkedin.com/jobs/view/example-1']),report_path=self.root/'report.json')
        self.assertEqual([v['status'] for v in report['sources']],['error','checked','fresh','manual_link'])
        self.assertEqual(report['job_keys'],[['web',item['id']]])
        client.page.assert_not_called()

    def test_full_description_and_duplicate_tracking_are_required(self):
        entry={};keys=[]
        item=sources.vacancy('https://company.example/job?utm_source=x','AI Engineer','Example','Алматы',BODY)
        discovery.accept(self.store,item,self.settings,PREF,entry,keys)
        discovery.accept(self.store,item,self.settings,PREF,entry,keys)
        self.assertEqual(len(keys),1);self.assertEqual(entry['known'],1)
        short=sources.vacancy('https://company.example/other','AI Engineer','Example','Алматы','Snippet')
        with self.assertRaises(CheckError):discovery.accept(self.store,short,self.settings,PREF,entry,keys)

    def test_new_discoveries_remain_in_backlog_until_processed(self):
        item=sources.vacancy('https://company.example/job','AI Engineer','Example','Алматы',BODY)
        config=sources.validate({'enabled':['companies'],'boards':[{'kind':'ashby','slug':'example','name':'Example'}],'career_pages':[]})
        with patch.object(sources,'board_jobs',return_value=iter([item])):
            discovery.collect(self.store,self.settings,PREF,config=config,client=Mock(),include_hh=False,
                              report_path=self.root/'report.json')
        self.assertEqual(discovery.pending_keys(self.store),[('web',item['id'])])
        discovery.processed(self.store,[dict(self.store.get(item['id'],'web'))])
        self.assertEqual(discovery.pending_keys(self.store),[])

    def test_stale_or_changed_web_plan_cannot_claim_fresh_coverage(self):
        old=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        self.assertEqual(discovery.web_input(self.web([],generated_at=old),self.settings)[2],'stale')
        self.assertEqual(discovery.web_input(self.web([],plan_hash='different'),self.settings)[2],'settings_changed')
        self.assertEqual(discovery.web_input(self.web([],queries=[]),self.settings)[2],'incomplete_plan')

    def test_source_collision_and_legacy_queue_migration_preserve_both_packets(self):
        self.store.save(vacancy('1'))
        item=vacancy('1');item['alternate_url']='https://example.com/jobs/1';self.store.save(item,source='web')
        self.store.connection.execute('CREATE TABLE draft_queue(vacancy_id TEXT PRIMARY KEY,packet_path TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        letter=self.root/'letter.txt';letter.write_text(BODY,encoding='utf-8');cv=self.root/'cv.docx';cv.write_bytes(b'fixture')
        paths=[]
        for source in ['hh','web']:
            packet={'source':source,'vacancy_id':'1','letter_path':str(letter),'local_cv_path':str(cv)}
            path=self.root/(source+'.json');path.write_text(json.dumps(packet),encoding='utf-8');paths.append(path)
        with self.store.connection:self.store.connection.execute('INSERT INTO draft_queue(vacancy_id,packet_path) VALUES (?,?)',('1',str(paths[0])))
        draft_queue.register(self.store.connection,paths[1])
        self.assertEqual(len(draft_queue.pending(self.store,PREF)),2)

    def test_external_assessment_and_english_letter_use_correct_source(self):
        from test_hunt import EN
        self.store.save(vacancy('1'))
        item=vacancy('1');item['alternate_url']='https://example.com/jobs/1';self.store.save(item,source='web')
        jobs=[dict(self.store.get('1','web'))]
        profile=self.root/'profile';profile.mkdir()
        for filename in [*hunt.assess.CV_FILES.values(),*prepare.RU_FILES.values()]:
            (profile/filename).write_bytes(b'fixture')
        english={'ml_engineer':'Evaluated image classification models and implemented inference using PyTorch.\nTested model latency and load handling with labeled data.',
                 'applied_ai':'Developed Python endpoints with FastAPI and RAG retrieval.\nImplemented structured output validation and schema checks.'}
        russian='Разрабатывала FastAPI-эндпоинты и JSON-схемы для сервиса заявок.\nДорабатывала RAG-поиск по документации с индексами SQLite и кэшем эмбеддингов.'
        assessment={'decision':'apply','cv_variant':'applied_ai','summary':'Подтверждено в CV.',
            'matches':[{'requirement_id':'J001','cv_id':'AP001','reason':'CV','coverage':'full'}],'gaps':[],'questions':[]}
        session=Mock();session.post.side_effect=[response(assessment),response({'cv_variant':'applied_ai','fact_ids':['AI001','AI002']})]
        with patch.object(hunt,'ROOT',self.root),patch.object(hunt.assess,'load_profiles',return_value=english), \
             patch.object(hunt.assess,'read_cv',return_value=russian),contextlib.redirect_stdout(io.StringIO()):
            packets=hunt.prepare_batch(self.store,jobs,model='test',profile_dir=profile,output=self.root/'run',session=session,
                run=True,budget=.02,preferences=PREF,key='fixture')
        self.assertEqual(len(packets),1)
        self.assertEqual(packets[0]['source'],'web')
        self.assertTrue(packets[0]['local_cv_path'].endswith('_EN.docx'))
        letter=Path(packets[0]['letter_path']).read_text(encoding='utf-8')
        self.assertIn('Hello!',letter);self.assertIn(english['applied_ai'].splitlines()[0],letter)
        row=self.store.connection.execute('SELECT source FROM assessments').fetchone()
        self.assertEqual(row[0],'web')

    def test_external_queue_opens_link_without_hh_sender_or_personal_data_entry(self):
        import hunter_menu
        item=sources.vacancy('https://company.example/job','AI Engineer','Example','Алматы',BODY)
        self.store.save(item,source='web')
        profile=self.root/'profile';profile.mkdir();(profile/'preferences.json').write_text(json.dumps(PREF),encoding='utf-8')
        folder=self.root/'drafts/runs/test';folder.mkdir(parents=True)
        letter=folder/'letter.txt';letter.write_text('A verified fact.',encoding='utf-8')
        cv=folder/'cv.docx';cv.write_bytes(b'fixture')
        packet={'source':'web','vacancy_id':item['id'],'url':item['alternate_url'],'title':'AI Engineer','company':'Example',
                'decision':'stretch','gaps':[],'local_cv_path':str(cv),'letter_path':str(letter)}
        path=folder/'application.json';path.write_text(json.dumps(packet),encoding='utf-8')
        draft_queue.register(self.store.connection,path)
        answers=iter(['о'])
        with patch.object(hunter_menu,'ROOT',self.root),patch.object(hunter_menu,'JobStore',side_effect=lambda:JobStore(self.root/'jobs.sqlite')), \
             patch('webbrowser.open') as opened,patch.object(hunt,'main') as sender,contextlib.redirect_stdout(io.StringIO()):
            hunter_menu.review(lambda _:next(answers))
        opened.assert_called_once_with(item['alternate_url']);sender.assert_not_called()


if __name__=='__main__':unittest.main()
