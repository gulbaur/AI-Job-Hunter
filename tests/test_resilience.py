"""Regression scenarios from the project review; temporary files, no paid/employer requests."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
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
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
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
