"""Regression scenarios from the project review; temporary files, no paid/employer requests."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import requests
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

import assess
import browser_check
import company_research
import discovery
import hunt
import hunter_web
import personalized
import preferences
import run_lock
import scheduled_run
import scheduled_runner
import search_settings
import sources
import work_queue
from common import CheckError
from storage import JobStore
from test_hunt import vacancy,PREF,EN,RU,response
from test_autonomy import RESEARCH,DRAFT
from test_prepare import PROFILES,JOB,ASSESSMENT


class ResilienceTests(unittest.TestCase):
    def test_hh_sending_does_not_depend_on_mad_devs_pilot(self):
        # Audit A38: hh_response imported SCHEMA/now from an archived site-form pilot that could send contacts and CV.
        root=Path(__file__).resolve().parents[1]
        code="import sys,hh_response,hunter_menu,telegram_bot; print('apply' in sys.modules)"
        result=subprocess.run([sys.executable,'-B','-c',code],cwd=root,capture_output=True,text=True,timeout=60)
        self.assertEqual(result.stdout.strip(),'False',result.stderr[-500:])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        # Other tests run code that does load_dotenv(real .env); a paired real bot must not leak into these.
        env=patch.dict(os.environ);env.start();self.addCleanup(env.stop)
        for name in ('TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID'):os.environ.pop(name,None)
        self.root=Path(self.temp.name);self.profile=self.root/'profile';self.profile.mkdir()
        self.db=self.root/'jobs.sqlite'
        (self.profile/'preferences.json').write_text(json.dumps(PREF),encoding='utf-8')
        search_settings.save(self.profile,{'letter_mode':'literal'})

    def hold_in_child(self,path):
        code="import sys,time;from pathlib import Path;from run_lock import one_run\nwith one_run(Path(sys.argv[1])):\n print('ready',flush=True)\n time.sleep(40)"
        child=subprocess.Popen([sys.executable,'-u','-c',code,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda: self.stop_child(child))
        self.assertEqual(child.stdout.readline().strip(),'ready')
        return child

    @staticmethod
    def stop_child(child):
        if child.poll() is None:child.terminate()
        child.communicate(timeout=5)

    def test_dead_legacy_lock_and_killed_owner_do_not_block_future_runs(self):
        path=self.profile/'hunt.lock';path.write_text('99999999')
        with hunt.one_run(path):
            with hunt.one_run(path):pass  # Same-context scheduler handoff is safe.
        child=self.hold_in_child(path)
        with self.assertRaises(run_lock.RunBusy):
            with hunt.one_run(path):pass
        self.stop_child(child)
        with hunt.one_run(path):pass

    def test_busy_cli_does_not_replace_report_and_scheduler_does_not_claim_slot(self):
        child=self.hold_in_child(self.profile/'hunt.lock')
        last=self.root/'data/last.json';last.parent.mkdir();last.write_text('{"previous":true}')
        state=self.root/'data/scheduled.json'
        with patch.object(hunt,'ROOT',self.root),patch.object(hunt,'LAST',last), \
             patch.object(scheduled_run,'ROOT',self.root),patch.object(scheduled_run,'STATE',state), \
             patch('scheduled_run.schedule_slot',return_value='2099-01-01/10:00'),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hunt.main(['--run','--queue']),75)
            self.assertEqual(scheduled_run.main(),75)
            self.assertFalse(state.exists())
            self.assertEqual(json.loads(last.read_text()),{'previous':True})
            self.stop_child(child)
            last.write_text(json.dumps({'state':'no_eligible_jobs','report_path':'immutable/run.json','run_id':'one'}))
            with patch('hunt.main',return_value=0):self.assertEqual(scheduled_run.main(),0)
        self.assertEqual(json.loads(state.read_text())['report_path'],'immutable/run.json')

    def test_closed_hh_job_is_archived_and_other_job_is_refreshed(self):
        with JobStore(self.db) as store:
            store.save(vacancy('1'));store.save(vacancy('2'))
            jobs=[dict(store.get('1')),dict(store.get('2'))]
            p=Mock();p.__enter__=Mock(return_value=p);p.__exit__=Mock(return_value=False)
            with patch('browser_check.sync_playwright',return_value=p), \
                 patch('browser_check.navigate',side_effect=[browser_check.VacancyClosed('404'),None]), \
                 patch('browser_check.check_blocker'),patch('browser_check.already_applied',return_value=False), \
                 patch('browser_check.read_job_snapshot',return_value=(vacancy('2'),{})):
                outcome=browser_check.refresh_saved(store,jobs)
            self.assertEqual(outcome['failed'],[])
            self.assertEqual(store.get('1')['status'],'archived')
            self.assertEqual([j['vacancy_id'] for j in hunt.eligible(store,PREF)],['2'])

    def test_browser_stop_returns_unrefreshed_jobs_to_next_run_not_to_manual_retry(self):
        # Audit A12: a closed Chromium window or lost network sent the whole queue to a manual paid retry.
        from playwright.sync_api import Error as PlaywrightError
        with JobStore(self.db) as store:
            store.save(vacancy('1'));store.save(vacancy('2'))
            jobs=[dict(store.get('1')),dict(store.get('2'))]
            p=Mock();p.__enter__=Mock(return_value=p);p.__exit__=Mock(return_value=False)
            p.chromium.launch_persistent_context.side_effect=PlaywrightError('Target page, context or browser has been closed')
            with patch('browser_check.sync_playwright',return_value=p):
                outcome=browser_check.refresh_saved(store,jobs)
            self.assertEqual(sorted(outcome['failed']),[('hh','1'),('hh','2')])  # skipped in this run
            self.assertEqual(work_queue.retry_required(store),set())
            self.assertEqual(work_queue.issues(store,PREF,search_settings.load(self.profile)),[])
            self.assertEqual(sorted(j['vacancy_id'] for j in hunt.eligible(store,PREF)),['1','2'])

    def test_unreadable_job_is_exposed_but_does_not_stop_other_refreshes(self):
        with JobStore(self.db) as store:
            store.save(vacancy('1'));store.save(vacancy('2'))
            p=Mock();p.__enter__=Mock(return_value=p);p.__exit__=Mock(return_value=False)
            with patch('browser_check.sync_playwright',return_value=p),patch('browser_check.navigate'), \
                 patch('browser_check.check_blocker'),patch('browser_check.already_applied',return_value=False), \
                 patch('browser_check.read_job_snapshot',side_effect=[browser_check.VacancyReadError('Field missing'),(vacancy('2'),{})]):
                outcome=browser_check.refresh_saved(store,[dict(store.get('1')),dict(store.get('2'))])
            self.assertEqual(outcome['failed'],[('hh','1')])
            self.assertEqual(work_queue.issues(store,PREF,search_settings.validate({}))[0]['vacancy_id'],'1')
            self.assertEqual(discovery.pending_keys(store),[('hh','2')])

    def test_failed_letter_is_visible_and_explicit_retry_reuses_assessment(self):
        with JobStore(self.db) as store:store.save(vacancy())
        match={'decision':'apply','cv_variant':'applied_ai','summary':'Python подтверждён.',
               'matches':[{'requirement_id':'J001','cv_id':'AP001','reason':'CV','coverage':'full'}],'gaps':[],'questions':[]}
        unavailable=response({});unavailable.status_code=503
        for filename in hunt.prepare.RU_FILES.values():(self.profile/filename).write_bytes(b'fixture')
        with patch.object(hunt,'ROOT',self.root),patch.object(hunt,'LAST',self.root/'data/last.json'), \
             patch.object(hunt,'LAST_PACKET',self.root/'data/packet.json'),patch.object(hunt,'JobStore',side_effect=lambda:JobStore(self.db)), \
             patch('assess.load_profiles',return_value=EN),patch('assess.read_cv',return_value=RU), \
             patch('browser_check.refresh_saved'),patch.dict(os.environ,{'OPENROUTER_API_KEY':'fixture','OPENROUTER_MODEL':'test'}), \
             patch('requests.Session.post',side_effect=[response(match),unavailable,response({'cv_variant':'applied_ai','fact_ids':['AI001','AI002']})]) as model, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hunt.main(['--run','--from-db','--queue']),0)
            with JobStore(self.db) as store:self.assertEqual(work_queue.issues(store,PREF,search_settings.validate({}))[0]['stage'],'letter_failed')
            self.assertEqual(hunt.main(['--run','--from-db','--queue']),0)
            self.assertEqual(model.call_count,2)  # No automatic paid retry.
            self.assertEqual(hunt.main(['--run','--queue','--retry-key','hh:1']),0)
            self.assertEqual(model.call_count,3)  # Assessment was reused.
        report=json.loads((self.root/'data/last.json').read_text())
        self.assertEqual(len(report['packet_paths']),1)
        with JobStore(self.db) as store:self.assertEqual(work_queue.issues(store,PREF,search_settings.validate({})),[])

    def test_office_negation_and_structured_country_restriction_take_priority(self):
        self.assertEqual(preferences.location_fit({'region':'Astana','description_text':'This is not a remote position. Work on-site in Astana.'},PREF)[0],'blocked')
        job={'region':'Germany','description_text':'Remote worldwide team. Applicants must be located in: Germany.'}
        self.assertEqual(sources.external_location(job,PREF)[0],'blocked')
        item=sources.vacancy('https://company.example/job','AI Engineer','Example','Worldwide','Remote worldwide role. '*10,applicant_locations=['Germany'])
        with JobStore(self.db) as store:
            store.save(item,source='web')
            self.assertEqual(sources.external_location(dict(store.get(item['id'],'web')),PREF)[0],'blocked')
        self.assertEqual(sources.external_location({**job,'description_text':'Remote worldwide. Work with us.'},PREF)[0],'remote_review')

    def test_company_page_reached_by_two_links_is_one_source(self):
        # Audit A29: kaspi.kz/ and a search hit both redirected to kaspi.kz/kz — the same text went in twice.
        body='<main>'+'<p>Kaspi.kz строит финтех и маркетплейс; команда развивает AI-продукты для миллионов клиентов.</p>'*4+'</main>'
        client=Mock();client.page.return_value=('https://kaspi.kz/kz',body)
        found={'hits':[{'url':'https://kaspi.kz/kz'}],'cached':False}
        with patch('company_research.cached_query',return_value=found):
            result=company_research.research({'company':'Kaspi.kz','url':'https://hh.kz/vacancy/1','source':'hh'},
                profile_dir=self.profile,session=Mock(),key='k',model='m',client=client)
        self.assertEqual(len(result['sources']),1)
        texts=[f['text'] for f in result['fragments'].values()]
        self.assertEqual(len(texts),len(set(texts)))

    def test_generic_domain_and_wrong_brand_content_are_not_company_sources(self):
        self.assertFalse(company_research.first_party('https://technologies.example/about','Dreamlab Technologies Ltd','Other company'))
        self.assertFalse(company_research.first_party('https://credit.example','Home Credit Bank','Home Credit'))
        self.assertFalse(company_research.first_party('https://dreamlab.example','Dreamlab Technologies Ltd','Other company'))
        self.assertTrue(company_research.first_party('https://dreamlab.example','Dreamlab Technologies Ltd','Dreamlab creates AI products.'))

    def test_web_hh_findings_are_read_by_enabled_hh_and_manual_when_disabled(self):
        config={'enabled':['hh','web'],'boards':[],'career_pages':[],'max_web_links':30}
        manifest=self.root/'browser.json'
        with JobStore(self.db) as store:
            def collect_hh(*args,**kwargs):
                self.assertIn('https://hh.kz/vacancy/1',kwargs['vacancy_urls'])
                store.save(vacancy())
                manifest.write_text(json.dumps({'vacancy_ids':['1'],'saved':1,'errors':0}))
                return 0
            with patch('discovery.web_input',return_value=([{'url':'https://hh.kz/vacancy/1'}],[],'fresh')), \
                 patch('browser_check.run',side_effect=collect_hh),patch.object(browser_check,'RUN_MANIFEST',manifest):
                result=discovery.collect(store,search_settings.validate({}),PREF,config=config,client=Mock(),report_path=self.root/'report.json')
            self.assertEqual(result['job_keys'],[['hh','1']])
            self.assertEqual(store.connection.execute('SELECT state FROM discovered_links').fetchone()[0],'hh_checked')
            with patch('discovery.web_input',return_value=([{'url':'https://hh.kz/vacancy/2'}],[],'fresh')),patch('browser_check.run') as browser:
                discovery.collect(store,search_settings.validate({}),PREF,config={**config,'enabled':['web']},client=Mock(),report_path=self.root/'report.json')
            browser.assert_not_called()
            self.assertEqual(store.connection.execute("SELECT state FROM discovered_links WHERE url LIKE '%/2'").fetchone()[0],'manual_link')

    def test_web_hh_links_are_read_once_and_never_loop(self):
        # Audit A22: a link skipped by the title filter stayed hh_pending and was reopened every run;
        # a checked link went back to hh_pending whenever web search listed it again.
        config={'enabled':['hh','web'],'boards':[],'career_pages':[],'max_web_links':30}
        manifest=self.root/'browser.json';opened=[]
        links=[{'url':'https://hh.kz/vacancy/1'},{'url':'https://hh.kz/vacancy/5'}]
        with JobStore(self.db) as store:
            def collect_hh(*args,**kwargs):
                opened.append(sorted(kwargs['vacancy_urls']))
                if 'https://hh.kz/vacancy/1' in kwargs['vacancy_urls']:store.save(vacancy())
                manifest.write_text(json.dumps({'vacancy_ids':[],'saved':0,'errors':0,'skipped_ids':['5']}))
                return 0
            for _ in range(2):
                with patch('discovery.web_input',return_value=(links,[],'fresh')),                      patch('browser_check.run',side_effect=collect_hh),patch.object(browser_check,'RUN_MANIFEST',manifest):
                    discovery.collect(store,search_settings.validate({}),PREF,config=config,client=Mock(),report_path=self.root/'report.json')
            states=dict(store.connection.execute('SELECT url,state FROM discovered_links'))
        self.assertEqual(opened,[['https://hh.kz/vacancy/1','https://hh.kz/vacancy/5'],[]])
        self.assertEqual(states,{'https://hh.kz/vacancy/1':'hh_checked','https://hh.kz/vacancy/5':'hh_skipped'})

    def test_transient_personal_letter_error_is_not_recorded_as_factual_rejection(self):
        unavailable=response({});unavailable.status_code=503
        session=Mock();session.post.return_value=unavailable
        with patch('company_research.research',return_value=RESEARCH),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(CheckError):personalized.prepare_letter(JOB,PROFILES,ASSESSMENT,'test',profile_dir=self.profile,output_dir=self.root/'drafts',run=True,budget=1,key='fixture',session=session)
        record=json.loads(next((self.root/'drafts').rglob('personalized.json')).read_text(encoding='utf-8'))
        self.assertEqual(record['status'],'failed')

    def test_notifications_deduplicate_failures_and_wait_only_before_work(self):
        data=self.root/'data';data.mkdir()
        counter=[0]
        def failed():
            counter[0]+=1
            (data/'last_scheduled_run.json').write_text(json.dumps({'started_at':str(counter[0]),'state':'stopped','exit_code':1,'error':'Model unavailable'}))
            return 1
        with patch.object(scheduled_runner,'ROOT',self.root),patch('scheduled_run.main',side_effect=failed),patch('subprocess.Popen') as notify:
            scheduled_runner.main();scheduled_runner.main()
            self.assertEqual(notify.call_count,1);self.assertIn('-Problem',notify.call_args.args[0])
        with patch.object(scheduled_runner,'ROOT',self.root),patch('scheduled_run.main',side_effect=[75,0]) as run, \
             patch('scheduled_runner.time.sleep') as wait,patch('subprocess.Popen'):
            scheduled_runner.main()
            self.assertEqual(run.call_count,2);wait.assert_called_once_with(30)

    def test_invalid_settings_exit_is_recorded_and_notified(self):
        # Audit A19: SystemExit from parser.error() bypassed the runner: no invocation record, no notice.
        data=self.root/'data';data.mkdir()
        with patch.object(scheduled_runner,'ROOT',self.root),patch('scheduled_run.main',side_effect=SystemExit(2)),              patch('subprocess.Popen') as notify:
            self.assertEqual(scheduled_runner.main(),2)
        self.assertEqual(json.loads((data/'last_scheduler_invocation.json').read_text(encoding='utf-8'))['exit_code'],2)
        self.assertTrue(notify.called)

    def test_report_replace_retries_while_file_is_briefly_locked(self):
        # Audit A20: os.replace fails with WinError 5 while the UI or antivirus reads the JSON.
        import common
        real=os.replace;calls=[0]
        def flaky(src,dst):
            calls[0]+=1
            if calls[0]<3:raise PermissionError(5,'Access is denied')
            return real(src,dst)
        with patch('os.replace',side_effect=flaky),patch('time.sleep'):
            common.write_json(self.root/'r.json',{'ok':1})
        self.assertEqual(json.loads((self.root/'r.json').read_text(encoding='utf-8')),{'ok':1})

    def test_odd_web_search_answer_never_stops_hh_and_boards(self):
        # Audit A18: `server_tool_use_details: null` or a non-dict annotation crashed the whole collection.
        import web_search
        odd=requests.Response();odd.status_code=200;odd._content_consumed=True
        odd._content=json.dumps({'choices':[{'message':{'content':'','annotations':['x',None]}}],
                                 'usage':{'server_tool_use_details':None}}).encode()
        session=Mock();session.post.return_value=odd
        with self.assertRaises(CheckError):web_search.search_query('q',session=session,key='k',model='m')
        odd._content=json.dumps({'choices':[{'message':{'content':'','annotations':['x',{'type':'url_citation','url_citation':None}]}}],
                                 'usage':{'server_tool_use_details':{'web_search_requests':1}}}).encode()
        self.assertEqual(web_search.search_query('q',session=session,key='k',model='m')['hits'],[])
        config={'enabled':['hh','web'],'boards':[],'career_pages':[],'max_web_links':30}
        with JobStore(self.db) as store, patch('web_search.refresh',side_effect=AttributeError('boom')),              patch('browser_check.run',side_effect=CheckError('stop')) as browser:
            report=discovery.collect(store,search_settings.validate({}),PREF,config=config,client=Mock(),
                                     report_path=self.root/'report.json',refresh_web=True,web_path=self.root/'web.json')
        browser.assert_called_once()
        web=next(e for e in report['sources'] if e.get('source')=='Веб-поиск')
        self.assertEqual(web['status'],'refresh_error')
        self.assertTrue(any('Веб-поиск' in w for w in discovery.coverage_warnings(report)))
        self.assertTrue(any('Веб-поиск' in str(w) for w in report.get('warnings',[])+[json.dumps(report,ensure_ascii=False)]))

    def test_some_unreadable_hh_cards_do_not_suggest_a_login_problem(self):
        # Senior review 13.11 (pipeline 9): one unreadable card produced «проверьте вход в HH».
        warnings=discovery.coverage_warnings({'sources':[{'source':'HH','status':'partial','details':{'errors':2}}]})
        self.assertIn('2',warnings[0]);self.assertNotIn('вход',warnings[0])

    def test_invalid_settings_stop_the_scheduled_run_with_the_real_reason(self):
        # Senior review 13.11 (pipeline 10): parser.error after the slot was claimed looked like an interrupted run.
        import scheduled_run
        data=self.root/'data';data.mkdir(exist_ok=True)
        (self.root/'profile/hunt.lock').touch()
        with patch.object(scheduled_run,'ROOT',self.root),patch.object(scheduled_run,'STATE',data/'last_scheduled_run.json'), \
             patch.object(scheduled_run,'schedule_slot',return_value='2026-11-13T18'),patch('hunt.main',side_effect=SystemExit(2)), \
             patch.object(hunt,'save_report',side_effect=lambda value,path:Path(path).write_text(json.dumps(value),encoding='utf-8')):
            code=scheduled_run.main()
        state=json.loads((data/'last_scheduled_run.json').read_text(encoding='utf-8'))
        self.assertEqual(code,2);self.assertEqual(state['state'],'stopped');self.assertIn('настройки',state['error'].lower())

    def test_lock_taken_for_a_moment_by_a_status_probe_is_waited_for(self):
        # Senior review 13.11 (web 8 / audit A21): the UI's 2.5 s busy probe holds the real lock for milliseconds;
        # a run starting in that instant failed with RunBusy instead of waiting a moment.
        lock=self.root/'probe.lock';calls=[0];real=run_lock._lock
        def contended(stream):
            calls[0]+=1
            if calls[0]==1:raise OSError('held by the probe')
            return real(stream)
        with patch('run_lock._lock',side_effect=contended),patch('run_lock.time.sleep'):
            with run_lock.one_run(lock):pass
        self.assertEqual(calls[0],2)

    def test_status_names_search_failure_together_with_work_issues(self):
        # Since 05.11.2026 "Требуют внимания: 3" hid the broken web search in the headline and notifications.
        import run_status
        message=run_status.problem({'state':'needs_attention','work_issues':[{'vacancy_id':str(i)} for i in range(3)],
            'coverage_warnings':['Веб-поиск не работает: выполнено 0 из 6 запросов.']})
        self.assertIn('Веб-поиск не работает',message);self.assertIn('Требуют внимания вакансии: 3',message)

    def test_killed_run_left_in_started_state_is_reported_as_interrupted(self):
        import run_status
        self.assertIn('прервал',run_status.problem({'state':'started','started_at':'2026-11-09T05:00:00+00:00'}))

    def test_runner_notifies_about_previous_run_killed_mid_work(self):
        data=self.root/'data';data.mkdir()
        (data/'last_scheduled_run.json').write_text(json.dumps({'started_at':'2026-11-09T05:00:00+00:00','state':'started'}))
        with patch.object(scheduled_runner,'ROOT',self.root),patch('scheduled_run.main',return_value=0), \
             patch('subprocess.Popen') as notify:
            scheduled_runner.main()
        self.assertIn('-Problem',notify.call_args.args[0])

    def test_window_given_up_after_waiting_for_busy_program_is_a_visible_problem(self):
        data=self.root/'data';data.mkdir()
        with patch.object(scheduled_runner,'ROOT',self.root),patch('scheduled_run.main',return_value=75), \
             patch('scheduled_runner.time.sleep'),patch('subprocess.Popen') as notify:
            self.assertEqual(scheduled_runner.main(),75)
        self.assertIn('-Problem',notify.call_args.args[0])
        state=hunter_web.App(self.root,db_path=self.db).state()['task']
        self.assertTrue(state['error']);self.assertIn('занята',state['message'])

    def test_each_lost_window_notifies_but_same_failure_report_does_not(self):
        data=self.root/'data';data.mkdir()
        with patch.object(scheduled_runner,'ROOT',self.root),patch('scheduled_run.main',return_value=75), \
             patch('scheduled_runner.time.sleep'),patch('subprocess.Popen') as notify:
            scheduled_runner.main();scheduled_runner.main()
        self.assertEqual(notify.call_count,2)

    def test_scheduled_run_keeps_one_daily_database_copy_for_two_weeks(self):
        # Audit A13: no automatic backup of jobs.sqlite (applications history, user decisions) existed.
        import sqlite3,common
        data=self.root/'data';data.mkdir()
        db=sqlite3.connect(data/'jobs.sqlite');db.execute('CREATE TABLE jobs(x)');db.execute("INSERT INTO jobs VALUES ('мой отклик')");db.commit();db.close()
        backups=data/'backups';backups.mkdir()
        for day in range(1,20):(backups/f'jobs-2026-10-{day:02d}.sqlite').write_bytes(b'old')
        (backups/'before-review-fixes-20261027T085511Z.sqlite').write_bytes(b'manual')
        common.daily_backup(data/'jobs.sqlite',backups,today='2026-11-10')
        common.daily_backup(data/'jobs.sqlite',backups,today='2026-11-10')
        copy=sqlite3.connect(backups/'jobs-2026-11-10.sqlite')
        self.assertEqual(copy.execute('SELECT x FROM jobs').fetchone()[0],'мой отклик');copy.close()
        daily=sorted(p.name for p in backups.glob('jobs-*.sqlite'))
        self.assertEqual(len(daily),14);self.assertEqual(daily[-1],'jobs-2026-11-10.sqlite')
        self.assertTrue((backups/'before-review-fixes-20261027T085511Z.sqlite').exists())  # manual copies untouched
        with patch.object(scheduled_runner,'ROOT',self.root),patch('scheduled_run.main',return_value=0), \
             patch('common.daily_backup') as backup,patch('subprocess.Popen'):
            scheduled_runner.main()
        backup.assert_called_once()

    def test_runner_uses_telegram_instead_of_popup_when_configured(self):
        import sqlite3,run_status
        data=self.root/'data';data.mkdir()
        with patch.object(scheduled_runner,'ROOT',self.root),patch('telegram_events.configured',return_value=True), \
             patch('scheduled_run.main',return_value=75),patch('scheduled_runner.time.sleep'),patch('subprocess.Popen') as popup:
            scheduled_runner.main()
        popup.assert_not_called()
        db=sqlite3.connect(data/'jobs.sqlite')
        try:rows=db.execute('SELECT kind,payload_json FROM telegram_outbox').fetchall()
        finally:db.close()
        self.assertEqual(rows[0][0],'problem');self.assertIn(run_status.WAIT_EXPIRED,json.loads(rows[0][1])['message'])

    def test_runner_keeps_popup_when_telegram_not_configured(self):
        data=self.root/'data';data.mkdir()
        with patch.object(scheduled_runner,'ROOT',self.root),patch('telegram_events.configured',return_value=False), \
             patch('scheduled_run.main',return_value=75),patch('scheduled_runner.time.sleep'),patch('subprocess.Popen') as popup:
            scheduled_runner.main()
        self.assertIn('-Problem',popup.call_args.args[0])

    def test_interrupted_run_is_shown_even_after_earlier_ui_action(self):
        app=hunter_web.App(self.root,db_path=self.db)
        app.task['updated_at']='2026-11-09T04:00:00+00:00'
        data=self.root/'data';data.mkdir()
        (data/'last_scheduled_run.json').write_text(json.dumps({'started_at':'2026-11-09T05:00:00+00:00','state':'started'}))
        with patch('hunter_web.is_busy',return_value=False):task=app.state()['task']
        self.assertTrue(task['error']);self.assertIn('прервал',task['message'])

    def test_schedule_failure_is_visible_and_manual_skip_can_be_restored(self):
        app=hunter_web.App(self.root,db_path=self.db)
        data=self.root/'data';data.mkdir()
        (data/'last_scheduled_run.json').write_text(json.dumps({'state':'stopped','exit_code':1,'error':'Model unavailable'}))
        self.assertTrue(app.state()['task']['error'])
        with JobStore(self.db) as store:
            store.save(vacancy());store.connection.execute("UPDATE jobs SET status='skipped'");store.connection.commit()
        app.mutate('restore',{'key':'hh:1'})
        with JobStore(self.db) as store:self.assertEqual(store.get('1')['status'],'new')


if __name__=='__main__':unittest.main()
