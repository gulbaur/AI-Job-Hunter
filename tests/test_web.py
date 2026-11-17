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

    def test_empty_body_and_unknown_action_get_precise_answers(self):
        # Audit A37: an empty body was «Слишком большой запрос», an unknown action «Черновик уже обработан».
        request=Request(self.url+'/api/skip',data=b'',headers={'Content-Type':'application/json','X-Hunter-Token':self.app.csrf})
        with self.assertRaises(HTTPError) as exc:urlopen(request,timeout=3)
        self.assertEqual(exc.exception.code,400)
        with self.assertRaises(HTTPError) as exc:self.post('no-such-action',{'key':'hh:999'})
        self.assertIn('Неизвестное действие',json.loads(exc.exception.read())['error'])

    def test_second_server_cannot_share_the_port(self):
        # Audit A24: SO_REUSEADDR let a second copy bind the same port on Windows; requests went to the first one.
        with self.assertRaises(OSError):hunter_web.Server(('127.0.0.1',self.server.server_port),self.app)

    def test_client_gone_before_reply_is_silent(self):
        # Audit A23: a closed browser tab produced a 500 written into a dead socket and two tracebacks.
        handler=hunter_web.Handler.__new__(hunter_web.Handler)
        handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
        handler.wfile=Mock();handler.wfile.write.side_effect=ConnectionAbortedError(10053,'aborted')
        handler.reply(200,{'ok':True})

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

    def test_letter_save_survives_a_brief_file_lock_and_never_claims_a_failed_save(self):
        # Senior review 13.11 (web 2): os.replace on a letter another process reads raised WinError 5; the server
        # answered «Данные сохранены» although nothing was saved.
        import common,os
        real=os.replace;calls=[0]
        def flaky(src,dst):
            calls[0]+=1
            if calls[0]==1:raise PermissionError(5,'Access is denied')
            return real(src,dst)
        with patch('common.os.replace',side_effect=flaky),patch('time.sleep'):
            self.app.mutate('letter',{'key':'hh:1','text':'Новый текст'})
        self.assertEqual(json.loads(urlopen(self.url+'/api/state').read())['jobs'][0]['letter'],'Новый текст')
        with patch('common.os.replace',side_effect=PermissionError(5,'Access is denied')),patch('time.sleep'), \
             self.assertRaises(HTTPError) as exc:
            self.post('letter',{'key':'hh:1','text':'Ещё текст'})
        error=json.loads(exc.exception.read())['error']
        self.assertIn('НЕ сохран',error);self.assertNotIn('Данные сохранены',error)

    def test_confirmed_send_is_recorded_even_if_the_final_screenshot_fails(self):
        # Senior review 13.11 (web 5): a failing after-submit screenshot hid a confirmed delivery.
        from playwright.sync_api import sync_playwright
        from playwright.sync_api._generated import Page
        import contextlib
        import hh_response
        original=Page.screenshot
        def shot(page,*args,**kwargs):
            if 'after-submit' in str(kwargs.get('path','')):raise RuntimeError('window closed')
            return original(page,*args,**kwargs)
        with sync_playwright() as p,self.app.store() as store:
            packet=json.loads(self.path.read_text(encoding='utf-8'))
            browser=p.chromium.launch(headless=True)
            def context(*args,**kwargs):
                ctx=browser.new_context()
                def route(req):
                    url=req.request.url
                    if '/submit' in url:req.fulfill(body='ok');return
                    if '/applicant/vacancy_response' in url:
                        html='''<meta charset="utf-8"><form><div data-qa="resume-title">AI/ML Engineer</div>
                        <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
                        <button data-qa="vacancy-response-submit-popup">Send</button></form><script>
                        document.querySelector('form').onsubmit=async e=>{e.preventDefault();
                        await fetch('/submit',{method:'POST',body:'x'});document.body.innerHTML='<div>Резюме доставлено</div>';};</script>'''
                    else:html='<a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId=1">Open</a>'
                    req.fulfill(status=200,content_type='text/html',body=html)
                ctx.route('**/*',route);return ctx
            fake=Mock();fake.__enter__=Mock(return_value=fake);fake.__exit__=Mock(return_value=False)
            fake.chromium.launch_persistent_context.side_effect=context
            with patch('playwright.sync_api.sync_playwright',return_value=fake),patch('sys.stdin',None), \
                 patch.object(Page,'screenshot',autospec=True,side_effect=shot),contextlib.redirect_stdout(io.StringIO()):
                state=hh_response.run(packet,store.connection,allow_send=True,confirmation_channel=lambda review,token:token)
            browser.close()
            self.assertEqual(state,'confirmed_by_site')
            self.assertEqual(store.get('1')['status'],'applied')

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
                # The bot sends this picture of the filled form to the phone (user request 13.11.2026).
                self.assertTrue(Path(cancel.call_args.args[0]['form_image']).is_file())
                self.assertEqual(cancel.call_args.args[1],'ОТПРАВИТЬ 1')
                self.assertEqual(hh_response.run(packet,store.connection,allow_send=True,confirmation_channel=lambda review,token:token),'confirmed_by_site')
                self.assertEqual(submitted,['Письмо пользователя'])
                with self.assertRaises(CheckError):hh_response.run(packet,store.connection,allow_send=True,confirmation_channel=lambda review,token:token)
            browser.close()

    def test_unconfirmed_send_keeps_browser_open_until_user_checked_then_rechecks(self):
        # Audit A14: after a click without visible confirmation (captcha, slow HH) the window closed in 15 s.
        from playwright.sync_api import sync_playwright
        import contextlib
        import hh_response
        with sync_playwright() as p,self.app.store() as store:
            packet=json.loads(self.path.read_text(encoding='utf-8'))
            browser=p.chromium.launch(headless=True)
            def context(*args,**kwargs):
                ctx=browser.new_context()
                def route(req):
                    url=req.request.url
                    if '/submit' in url:req.fulfill(body='ok');return
                    if '/applicant/vacancy_response' in url:
                        html='''<meta charset="utf-8"><form><div data-qa="resume-title">AI/ML Engineer</div>
                        <textarea data-qa="vacancy-response-popup-form-letter-input"></textarea>
                        <button data-qa="vacancy-response-submit-popup">Send</button></form><script>
                        document.querySelector('form').onsubmit=async e=>{e.preventDefault();
                        await fetch('/submit',{method:'POST',body:'x'});document.body.innerHTML='<div>Пройдите капчу</div>';
                        setTimeout(()=>{document.body.innerHTML='<div>Резюме доставлено</div>';},700);};</script>'''
                    else:html='<a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId=1">Open</a>'
                    req.fulfill(status=200,content_type='text/html',body=html)
                ctx.route('**/*',route);return ctx
            fake=Mock();fake.__enter__=Mock(return_value=fake);fake.__exit__=Mock(return_value=False)
            fake.chromium.launch_persistent_context.side_effect=context
            checked=Mock(side_effect=lambda review:time.sleep(1.2))
            with patch('playwright.sync_api.sync_playwright',return_value=fake),patch('sys.stdin',None), \
                 patch.object(hh_response,'SUCCESS_TIMEOUT_MS',200),contextlib.redirect_stdout(io.StringIO()):
                state=hh_response.run(packet,store.connection,allow_send=True,
                    confirmation_channel=lambda review,token:token,result_channel=checked)
            browser.close()
        checked.assert_called_once()
        self.assertEqual(state,'confirmed_by_site')

    def attempt(self,state):
        import hh_response
        with self.app.store() as store:
            store.connection.executescript(hh_response.SCHEMA)
            with store.connection:store.connection.execute('INSERT INTO application_attempts VALUES (?,?,?,?)',
                ('https://hh.kz/vacancy/1','hash',state,'2026-11-09T10:00:00+00:00'))

    def attempt_state(self):
        with self.app.store() as store:
            return store.connection.execute('SELECT state FROM application_attempts WHERE target_url=?',('https://hh.kz/vacancy/1',)).fetchone()[0]

    def test_user_can_record_that_unverified_hh_response_was_not_sent(self):
        self.attempt('submitted_unverified')
        with self.assertRaises(CheckError):self.app.mutate('resolve',{'url':'https://hh.kz/vacancy/1','text':'да'})
        self.app.mutate('resolve',{'url':'https://hh.kz/vacancy/1','text':'ОТКЛИКА НЕТ 1'})
        self.assertEqual(self.attempt_state(),'hh_form_closed')
        history=self.app.state()['history'][0]
        self.assertEqual(history['vacancy_id'],'1')

    def test_user_can_record_that_unverified_hh_response_was_delivered(self):
        self.attempt('submit_started')
        self.app.mutate('resolve',{'url':'https://hh.kz/vacancy/1','text':'ОТКЛИК ЕСТЬ 1'})
        self.assertEqual(self.attempt_state(),'reported_by_user')
        with self.app.store() as store:self.assertEqual(store.get('1')['status'],'applied')

    def test_confirmed_attempt_cannot_be_reopened_by_user_mark(self):
        self.attempt('confirmed_by_site')
        with self.assertRaises(CheckError):self.app.mutate('resolve',{'url':'https://hh.kz/vacancy/1','text':'ОТКЛИКА НЕТ 1'})
        self.assertEqual(self.attempt_state(),'confirmed_by_site')

    def test_result_check_dialog_waits_for_user_and_is_single_use(self):
        done=[]
        thread=threading.Thread(target=lambda:done.append(self.app.result_channel({'hh_resume_title':'AI Engineer'})))
        thread.start()
        for _ in range(200):
            if self.app.confirmation:break
            time.sleep(.005)
        c=self.app.state()['confirmation'];self.assertTrue(c['result'])
        self.assertEqual(done,[])
        self.app.confirm({'attempt':c['attempt'],'checked':True})
        thread.join(2);self.assertEqual(done,[None])
        with self.assertRaises(CheckError):self.app.confirm({'attempt':c['attempt'],'checked':True})

    def test_manual_sent_allowed_for_hh_with_exact_phrase(self):
        with self.assertRaises(HTTPError):self.post('manual-sent',{'key':'hh:1','text':'да'})
        self.post('manual-sent',{'key':'hh:1','text':'ОТПРАВЛЕНО 1'}).close()
        self.assertEqual(self.app.state()['history'][0]['state'],'reported_by_user')

    def test_one_broken_packet_does_not_blank_the_whole_ui(self):
        # Audit A15: one packet with a missing letter or an http:// link made /api/state answer 500.
        for identity,breakage in (('2','letter'),('3','url')):
            folder=self.root/f'drafts/bad{identity}';folder.mkdir(parents=True)
            letter=folder/'letter.txt';letter.write_text('x',encoding='utf-8')
            packet=json.loads(self.path.read_text(encoding='utf-8'))
            packet.update(vacancy_id=identity,url=f'https://hh.kz/vacancy/{identity}',letter_path=str(letter),output_dir=str(folder))
            path=folder/'application.json';path.write_text(json.dumps(packet),encoding='utf-8')
            with self.app.store() as store:store.save(vacancy(identity));draft_queue.register(store.connection,path)
            if breakage=='letter':letter.unlink()
            else:path.write_text(json.dumps({**packet,'url':f'http://hh.kz/vacancy/{identity}'}),encoding='utf-8')
        state=json.loads(urlopen(self.url+'/api/state',timeout=3).read())
        self.assertEqual([j['id'] for j in state['jobs']],['1'])
        self.assertEqual(sorted(b['key'] for b in state['broken']),['hh:2','hh:3'])
        self.assertTrue(all(b['error'] for b in state['broken']))

    def test_state_reports_user_edited_letter(self):
        self.assertFalse(self.app.state()['jobs'][0]['edited'])
        self.post('letter',{'key':'hh:1','text':'Моя правка'}).close()
        self.assertTrue(self.app.state()['jobs'][0]['edited'])

    def test_state_shows_dead_bot(self):
        import os
        from datetime import datetime,timedelta,timezone
        (self.root/'.env').write_text('TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_CHAT_ID=42\n',encoding='utf-8')
        (self.root/'data').mkdir(exist_ok=True)
        beat=self.root/'data/telegram_bot_heartbeat.json'
        beat.write_text(json.dumps({'pid':1,'updated_at':(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat(),'last_error':''}),encoding='utf-8')
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'','TELEGRAM_CHAT_ID':''}):
            telegram=self.app.state()['telegram']
            self.assertTrue(telegram['configured']);self.assertFalse(telegram['alive'])
            beat.write_text(json.dumps({'pid':1,'updated_at':datetime.now(timezone.utc).isoformat(),'last_error':''}),encoding='utf-8')
            self.assertTrue(self.app.state()['telegram']['alive'])

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
