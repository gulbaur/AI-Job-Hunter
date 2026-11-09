"""Local HTTP/UI integration; no employer or model requests."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from common import CheckError
import draft_queue
import hunter_web
import search_settings
import sources
from storage import JobStore
from test_hunt import PREF,vacancy


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);profile=self.root/'profile';profile.mkdir()
        (profile/'preferences.json').write_text(json.dumps(PREF),encoding='utf-8')
        search_settings.save(profile,{})
        sources.save(profile,{'enabled':['hh'],'boards':[],'career_pages':[]})
        folder=self.root/'drafts/test';folder.mkdir(parents=True)
        cv=profile/'cv.docx';cv.write_bytes(b'fixture')
        letter=folder/'letter.txt';letter.write_text('Письмо пользователя',encoding='utf-8')
        packet={'source':'hh','vacancy_id':'1','title':'AI Engineer','company':'Example',
            'decision':'stretch','url':'https://hh.kz/vacancy/1','gaps':[],
            'letter_path':str(letter),'local_cv_path':str(cv),'output_dir':str(folder)}
        self.path=folder/'application.json';self.path.write_text(json.dumps(packet),encoding='utf-8')
        self.app=hunter_web.App(self.root)
        with self.app.store() as store:store.save(vacancy());draft_queue.register(store.connection,self.path)
        self.server=hunter_web.Server(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close_server)
        self.url=f'http://127.0.0.1:{self.server.server_port}'

    def close_server(self):self.server.shutdown();self.server.server_close();self.thread.join(2)

    def post(self,action,data,**headers):
        return urlopen(Request(self.url+'/api/'+action,data=json.dumps(data).encode(),headers={
            'Content-Type':'application/json','X-Hunter-Token':self.app.csrf,**headers}),timeout=3)

    def test_opening_ui_only_reads_existing_packets_and_never_opens_employer(self):
        with patch('hh_response.run') as send,patch('requests.Session.post') as model:
            with urlopen(self.url+'/api/state') as r:state=json.load(r)
            with urlopen(self.url+'/') as r:
                self.assertIn('Готовые отклики',r.read().decode())
                self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
            self.assertEqual(state['jobs'][0]['letter'],'Письмо пользователя')
            self.assertTrue(state['jobs'][0]['unread'])
            send.assert_not_called();model.assert_not_called()

    def test_failed_hh_source_is_visible_in_main_status_without_losing_letters(self):
        data=self.root/'data';data.mkdir(exist_ok=True)
        (data/'last_discovery.json').write_text(json.dumps({'sources':[
            {'source':'HH','status':'partial','details':{'errors':1,'error':'Профиль браузера занят.'}}]}),encoding='utf-8')
        state=self.app.state()
        self.assertTrue(state['task']['error'])
        self.assertIn('HH',state['task']['message'])
        self.assertIn('Профиль браузера занят',state['task']['message'])
        self.assertEqual(len(state['jobs']),1)

    def test_local_origin_csrf_and_static_allowlist(self):
        for headers in [{'X-Hunter-Token':'wrong'},{'Origin':'https://evil.example'},{'Host':'evil.example'},
                        {'Sec-Fetch-Site':'cross-site'}]:
            with self.subTest(headers=headers):
                with self.assertRaises(HTTPError) as exc:self.post('skip',{'key':'hh:1'},**headers)
                self.assertEqual(exc.exception.code,403)
        for path in ['/.env','/profile/preferences.json','/../.env']:
            with self.subTest(path=path),self.assertRaises(HTTPError) as exc:urlopen(self.url+path)
            self.assertEqual(exc.exception.code,404)

    def test_letter_edit_is_persistent_and_does_not_send_or_set_applied(self):
        with patch('hh_response.run') as sender:
            self.post('letter',{'key':'hh:1','text':'Моя редакция'}).close()
            second=hunter_web.App(self.root).state()
        self.assertEqual(second['jobs'][0]['letter'],'Моя редакция')
        self.assertFalse(second['jobs'][0]['unread']);sender.assert_not_called()
        with self.app.store() as store:self.assertEqual(store.get('1')['status'],'new')

    def test_settings_save_actual_profile_and_reject_invalid_before_writing(self):
        state=self.app.state();settings={**state['settings'],'custom_roles':['Speech Engineer'],'budget_usd':.015}
        self.post('settings',{'settings':settings,'sources':state['sources'],'excluded_companies':['Kaspi.kz','Other']}).close()
        self.assertEqual(search_settings.load(self.root/'profile')['custom_roles'],['Speech Engineer'])
        with self.assertRaises(HTTPError):self.post('settings',{'settings':{**settings,'budget_usd':-1},'sources':state['sources']})
        self.assertEqual(search_settings.load(self.root/'profile')['budget_usd'],.015)

    def test_confirmation_requires_current_nonce_exact_token_and_is_single_use(self):
        result=[]
        thread=threading.Thread(target=lambda:result.append(self.app.confirm_channel({'hh_resume_title':'AI Engineer'},'ОТПРАВИТЬ 1')))
        thread.start()
        for _ in range(200):
            if self.app.confirmation:break
            time.sleep(.005)
        attempt=self.app.confirmation['attempt']
        with self.assertRaises(CheckError):self.app.confirm({'attempt':attempt,'text':'да'})
        with self.assertRaises(CheckError):self.app.confirm({'attempt':'old','text':'ОТПРАВИТЬ 1'})
        self.assertEqual(result,[])
        self.app.confirm({'attempt':attempt,'text':'ОТПРАВИТЬ 1'})
        thread.join(2);self.assertEqual(result,['ОТПРАВИТЬ 1'])
        with self.assertRaises(CheckError):self.app.confirm({'attempt':attempt,'text':'ОТПРАВИТЬ 1'})

    def test_defer_and_skip_preserve_files_and_never_send(self):
        with patch('hh_response.run') as sender:
            self.post('defer',{'key':'hh:1'}).close()
            self.assertFalse(self.app.state()['jobs'][0]['unread'])
            self.post('skip',{'key':'hh:1'}).close()
            self.assertEqual(self.app.state()['jobs'],[]);sender.assert_not_called()
        self.assertTrue((self.root/'drafts/test/letter.txt').exists())

    def test_search_worker_uses_pipeline_queue_without_any_send_flag(self):
        process=Mock();process.stdout=io.StringIO('1/4. Поиск\n2/4. Оценка\n');process.wait.return_value=0
        with patch('subprocess.Popen',return_value=process) as launch:
            self.app.search();self.app.worker.join(2)
        args=launch.call_args.args[0]
        self.assertEqual(args[-2:],['--run','--queue']);self.assertNotIn('--send',args)
        self.assertFalse(self.app.task['busy'])

    def test_letter_cannot_be_changed_during_preparation(self):
        self.app.task['busy']=True
        with self.assertRaises(CheckError):self.app.mutate('letter',{'key':'hh:1','text':'changed'})
        self.assertEqual(self.app.state()['jobs'][0]['letter'],'Письмо пользователя')

    def test_browser_channel_cancel_and_exact_approval_submit_local_form_once(self):
        from playwright.sync_api import sync_playwright
        import contextlib
        import hh_response
        with sync_playwright() as p,self.app.store() as store:
            packet=json.loads(self.path.read_text(encoding='utf-8'))
            browser=p.chromium.launch(headless=True)
            submitted=[]
            def context(*args,**kwargs):
                ctx=browser.new_context()
                def route(req):
                    url=req.request.url
                    if '/submit' in url:
                        submitted.append(req.request.post_data);req.fulfill(body='ok');return
                    if '/applicant/vacancy_response' in url:
                        html='''<meta charset="utf-8"><form><div data-qa="resume-title">AI/ML Engineer</div>
                        <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
                        <button data-qa="vacancy-response-submit-popup">Send</button></form><script>
                        document.querySelector('form').onsubmit=async e=>{e.preventDefault();
                        await fetch('/submit',{method:'POST',body:document.querySelector('textarea').value});
                        document.body.innerHTML='<div>Резюме доставлено</div>';};</script>'''
                    else:html='<a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId=1">Open</a>'
                    req.fulfill(status=200,content_type='text/html',body=html)
                ctx.route('**/*',route);return ctx
            fake=Mock();fake.__enter__=Mock(return_value=fake);fake.__exit__=Mock(return_value=False)
            fake.chromium.launch_persistent_context.side_effect=context
            with patch('playwright.sync_api.sync_playwright',return_value=fake),patch('sys.stdin',None),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(CheckError):hh_response.run(packet,store.connection,allow_send=True)
                cancel=Mock(return_value='')
                self.assertEqual(hh_response.run(packet,store.connection,allow_send=True,confirmation_channel=cancel),'cancelled_before_send')
                self.assertEqual(submitted,[])
                self.assertEqual(cancel.call_args.args[0]['hh_resume_title'],'AI/ML Engineer')
                self.assertEqual(cancel.call_args.args[1],'ОТПРАВИТЬ 1')
                self.assertEqual(hh_response.run(packet,store.connection,allow_send=True,confirmation_channel=lambda review,token:token),'confirmed_by_site')
                self.assertEqual(submitted,['Письмо пользователя'])
                with self.assertRaises(CheckError):hh_response.run(packet,store.connection,allow_send=True,confirmation_channel=lambda review,token:token)
            browser.close()

    def test_failed_preparation_ui_retries_only_selected_job_without_send(self):
        import work_queue
        from playwright.sync_api import sync_playwright
        with self.app.store() as store:work_queue.stage(store,('hh','1'),'letter_failed','Тестовая временная ошибка модели.')
        with patch.object(self.app,'pipeline') as pipeline,sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                page=browser.new_page();page.goto(self.url)
                page.locator('#attention-panel').wait_for(state='visible')
                page.locator('#attention-panel summary').click()
                self.assertIn('Тестовая временная ошибка',page.locator('#attention-list').inner_text())
                with page.expect_response(lambda r:r.url.endswith('/api/retry')) as response:
                    page.get_by_role('button',name='Повторить подготовку',exact=True).click()
                self.assertEqual(response.value.status,200)
                pipeline.assert_called_once_with(['--run','--queue','--retry-key','hh:1'],kind='retry')
            finally:browser.close()


if __name__=='__main__':unittest.main()
