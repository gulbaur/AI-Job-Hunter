"""Loopback-only desktop UI for the existing personal Job Hunter."""
import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
import webbrowser

import actions
import assess
from common import CheckError
import draft_queue
import hh_response
import hunt
from preferences import load_preferences
import search_settings
import sources
import telegram_store
import work_queue
import run_status
from run_lock import is_busy
from storage import JobStore

ROOT=Path(__file__).resolve().parent
ASSETS=ROOT/'web'


def stamp():
    return datetime.now(timezone.utc).isoformat()


class App:
    def __init__(self,root=ROOT,db_path=None):
        self.root=Path(root)
        self.db_path=db_path or self.root/'data/jobs.sqlite'
        self.csrf=secrets.token_urlsafe(32)
        self.guard=threading.RLock()
        self.worker=None
        self.logs=deque(maxlen=250)
        self.task={'busy':False,'kind':None,'message':'Всё готово к работе','error':False,'updated_at':''}
        self.confirmation=None
        self.confirm_event=threading.Event()
        self.answer=''

    def store(self):return JobStore(self.db_path)

    def entries(self,store):return actions.pending(self.root,store)

    def telegram_status(self):
        """Configured and alive: the bot rewrites its heartbeat every minute; >5 min old means it is not running."""
        result={'configured':telegram_store.configured(self.root),'alive':False,'error':''}
        try:
            beat=json.loads((self.root/'data/telegram_bot_heartbeat.json').read_text(encoding='utf-8'))
            age=datetime.now(timezone.utc)-datetime.fromisoformat(beat['updated_at'])
            result.update(alive=age.total_seconds()<300,error=str(beat.get('last_error') or ''))
        except (OSError,ValueError,KeyError,TypeError):pass
        return result

    def packet(self,store,key):return actions.packet(self.root,store,key)

    def state(self):
        external_busy=is_busy(self.root/'profile/hunt.lock')
        with self.store() as store:
            if not external_busy:work_queue.recover_legacy(store)
            entries=self.entries(store)
            lost=[]
            unread={p for p,_ in draft_queue.pending(store,load_preferences(self.root/'profile'),search_settings.load(self.root/'profile'),unread_only=True,problems=lost,root=self.root)}
            jobs=[];broken=[]
            for item in lost:
                row=store.connection.execute('SELECT title,company FROM jobs WHERE source=? AND vacancy_id=?',(item['source'],item['vacancy_id'])).fetchone()
                broken.append({'key':item['source']+':'+item['vacancy_id'],'title':row['title'] if row else 'Вакансия',
                    'company':row['company'] if row else '','path':item['packet_path'],'error':'Черновик не открыт: '+item['error']+'.'})
            for path,packet in entries:
              # One unreadable packet (deleted letter, bad link) is listed apart instead of failing the whole UI.
              try:
                if not Path(packet['letter_path']).resolve().is_relative_to((self.root/'drafts').resolve()):continue
                row=store.connection.execute('SELECT region FROM jobs WHERE source=? AND vacancy_id=?',
                    (packet['source'],packet['vacancy_id'])).fetchone()
                jobs.append({'key':packet['source']+':'+packet['vacancy_id'],'source':packet['source'],
                    'id':packet['vacancy_id'],'title':packet['title'] or 'Вакансия','company':packet.get('company') or 'Компания не указана',
                    'url':sources.canonical_url(packet['url']),'decision':packet['decision'],
                    'summary':packet.get('assessment_summary',''),'gaps':packet.get('gaps',[]),
                    'cv':actions.cv_for_sending(packet).name,'letter':Path(packet['letter_path']).read_text(encoding='utf-8'),
                    'region':row['region'] if row else '', 'unread':path in unread,
                    'letter_mode':packet.get('letter_mode','literal'),'research':packet.get('company_research'),
                    'edited':actions.is_user_edited(store,packet)})
              except (OSError,ValueError,KeyError,TypeError,AttributeError,CheckError) as exc:
                reason='файл письма не найден' if isinstance(exc,FileNotFoundError) else str(exc) if isinstance(exc,CheckError) else type(exc).__name__
                broken.append({'key':f"{packet.get('source','?')}:{packet.get('vacancy_id','?')}",'title':packet.get('title') or 'Вакансия',
                    'company':packet.get('company') or '','path':str(path),'error':'Черновик не открыт: '+reason+'.'})
            store.connection.executescript(hh_response.SCHEMA)
            history=[dict(r) for r in store.connection.execute('''SELECT a.state,a.updated_at,a.target_url,
                j.title,j.company,j.source,j.vacancy_id FROM application_attempts a LEFT JOIN jobs j ON j.url=a.target_url
                ORDER BY a.updated_at DESC LIMIT 100''')]
            failures=work_queue.issues(store,load_preferences(self.root/'profile'),search_settings.load(self.root/'profile'),running=external_busy)
            excluded=[dict(r) for r in store.connection.execute("SELECT source,vacancy_id,title,company,status_reason FROM jobs WHERE status='skipped' ORDER BY last_seen_at DESC")]
            leads=[]
            exists=store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovered_links'").fetchone()
            if exists:leads=[dict(r) for r in store.connection.execute("SELECT url,note,state FROM discovered_links WHERE state IN ('manual_link','hh_pending','hh_link') ORDER BY last_seen_at DESC LIMIT 30")]
        report={}
        try:report=json.loads((self.root/'data/last_discovery.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        with self.guard:
            task=dict(self.task);confirmation=dict(self.confirmation) if self.confirmation else None
            logs=list(self.logs)
        schedule={};last_scheduled={};last_run={};active={};invocation={};scheduled_log=''
        try:schedule=json.loads((self.root/'data/windows_schedule.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        try:last_scheduled=json.loads((self.root/'data/last_scheduled_run.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        try:last_run=json.loads((self.root/'data/last_hunt_run.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        try:active=json.loads((self.root/'data/active_run.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        try:
            invocation=json.loads((self.root/'data/last_scheduler_invocation.json').read_text(encoding='utf-8'))
            log=Path(invocation['log_path']).resolve()
            if log.is_relative_to((self.root/'data/scheduled_logs').resolve()):scheduled_log=log.read_text(encoding='utf-8')[-16000:]
        except (OSError,ValueError,KeyError):pass
        latest=max([last_run,last_scheduled,active],key=lambda v:(v.get('started_at') or v.get('finished_at',''),bool(v)))
        if not task['busy'] and external_busy:
            task.update(busy=True,kind='background',message='Программа выполняет обработку в другом окне или по расписанию…')
        # An interrupted run has no finished_at; compare by the time it started so it is not hidden by an older UI action.
        if not task['busy'] and (latest.get('finished_at') or latest.get('started_at') or '')>=task.get('updated_at',''):
            message=run_status.problem(latest)
            if message:task.update(error=True,message=message)
        if not task['busy'] and failures and not task['error']:
            task.update(error=True,message=f'Требуют внимания вакансии: {len(failures)}. Можно явно повторить подготовку ниже.')
        if not task['busy'] and invocation.get('exit_code') not in (None,0) and invocation.get('invoked_at','')>max(latest.get('finished_at',''),task.get('updated_at','')):
            task.update(error=True,message=run_status.WAIT_EXPIRED if invocation.get('exit_code')==75 else
                        'Последний плановый запуск остановился. Подробности — в журнале ниже.')
        if not task['busy'] and report:
            from discovery import coverage_warnings
            warnings=coverage_warnings(report)
            if warnings and not task['error']:
                task.update(error=True,message='Поиск неполный: '+warnings[0])
        return {'telegram':self.telegram_status(),'csrf':self.csrf,'jobs':jobs,'broken':broken,'history':history,'task':task,'confirmation':confirmation,
            'logs':logs,'coverage':report,'settings':search_settings.load(self.root/'profile'),
            'sources':sources.load(self.root/'profile'),'preferences':load_preferences(self.root/'profile'),
            'schedule':schedule,'last_scheduled':last_scheduled,'failures':failures,'excluded':excluded,
            'leads':leads,'scheduled_log':scheduled_log}

    def start(self,kind,work):
        with self.guard:
            if self.task['busy']:raise CheckError('Программа уже работает. Дождитесь завершения или отмените подготовку формы.')
            self.task={'busy':True,'kind':kind,'message':'Запускаю…','error':False}
            self.logs.clear()
            def run():
                error=False;message='Готово'
                try:message=work() or message
                except CheckError as exc:error=True;message=str(exc)
                except Exception as exc:
                    error=True
                    message=('Действие остановлено. Проверьте историю перед повторной отправкой.' if kind=='hh' else
                             'Вход не завершён: окно браузера закрыто или HH не ответил. Повторите «Вход в HH».' if kind=='login' else
                             'Действие остановлено из-за ошибки. Повторите позже.')
                    with self.guard:self.logs.append('Ошибка: '+type(exc).__name__)
                finally:
                    with self.guard:
                        self.confirmation=None
                        self.task={'busy':False,'kind':kind,'message':message,'error':error,'updated_at':stamp()}
            self.worker=threading.Thread(target=run,daemon=True)
            self.worker.start()

    def search(self):
        self.pipeline(['--run','--queue'])

    def rewrite(self,key):
        with self.store() as store:self.packet(store,key)
        self.pipeline(['--run','--queue','--rewrite-key',key],kind='rewrite')

    def retry(self,key):
        with self.store() as store:
            available=work_queue.issues(store,load_preferences(self.root/'profile'),search_settings.load(self.root/'profile'))
            if not any(v['key']==key for v in available):raise CheckError('Неудачная подготовка уже обработана или недоступна.')
        self.pipeline(['--run','--queue','--retry-key',key],kind='retry')

    def pipeline(self,arguments,kind='search'):
        def work():
            process=subprocess.Popen([sys.executable,'-X','utf8','-u',str(self.root/'hunt.py'),*arguments],
                cwd=self.root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            for line in process.stdout:
                with self.guard:
                    self.logs.append(line.rstrip())
                    if line.startswith('1/4.'):self.task['message']='Ищу вакансии и проверяю источники…'
                    elif line.startswith('2/4.'):self.task['message']='Сравниваю вакансии с твоим CV…'
                    elif line.startswith('3/4.'):self.task['message']='Сохраняю подготовленные письма…'
                    elif line.startswith('Поиск:'):self.task['message']=line.strip()
                    elif line.startswith('Веб-поиск OpenRouter:'):self.task['message']=line.strip()
                    elif line.startswith('Изучаю компанию'):self.task['message']='Изучаю компанию и пишу индивидуальное письмо…'
            if process.wait():raise CheckError('Поиск остановлен. Подробности — в журнале ниже; сохранённые письма доступны.')
            report={}
            try:report=json.loads((self.root/'data/last_hunt_run.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):pass
            count=len(report.get('packet_paths',[]))
            if run_status.problem(report):raise CheckError((f'Готово писем: {count}. ' if count else '')+run_status.problem(report))
            if count:return f'Подготовлено писем: {count}. Можно посмотреть и поправить.'
            if kind=='rewrite':raise CheckError('Новая версия не подготовлена. Прежнее письмо сохранено; причина — в журнале.')
            return 'Проверка завершена. Новых писем пока нет; подробности — в отчёте об источниках.'
        self.start(kind,work)

    def confirm_channel(self,review,token):
        with self.guard:
            self.answer='';self.confirm_event.clear()
            self.confirmation={'attempt':secrets.token_urlsafe(24),'review':review,'token':token}
            self.task['message']='Форма HH заполнена. Проверьте её в открытом окне и подтвердите здесь.'
        if not self.confirm_event.wait(600):
            with self.guard:self.confirmation=None
            return ''
        with self.guard:
            answer=self.answer;self.confirmation=None
        return answer

    def result_channel(self,review):
        """After a click without visible HH confirmation: keep the browser open until the user has looked."""
        with self.guard:
            self.answer='';self.confirm_event.clear()
            self.confirmation={'attempt':secrets.token_urlsafe(24),'review':review,'result':True}
            self.task['message']='HH не показал подтверждение. Проверьте открытое окно HH и нажмите «Проверила».'
        self.confirm_event.wait(600)
        with self.guard:self.confirmation=None

    def confirm(self,data):
        with self.guard:
            c=self.confirmation
            if not c or data.get('attempt')!=c['attempt']:raise CheckError('Подтверждение устарело. Повторная отправка не запускается.')
            if c.get('result'):
                # Only acknowledges that the user looked; it never clicks anything on HH.
                self.confirmation=None;self.task['message']='Проверяю результат на странице HH…';self.confirm_event.set()
                return
            if data.get('cancel') is not True and data.get('text')!=c['token']:
                raise CheckError('Введите фразу подтверждения точно как показано.')
            self.answer='' if data.get('cancel') is True else data['text']
            self.confirmation=None
            self.task['message']='Отменяю подготовку…' if not self.answer else 'Проверяю и отправляю один отклик…'
            self.confirm_event.set()

    def prepare_hh(self,key):
        def work():
            with hunt.one_run(self.root/'profile/hunt.lock'),self.store() as store:
                _,packet=self.packet(store,key)
                if packet['source']!='hh':raise CheckError('На этом сайте отправка выполняется вручную.')
                packet={**packet,'output_dir':str(self.root/'drafts/runs'/('web-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')))}
                state=hh_response.run(packet,store.connection,allow_send=True,
                    profile_dir=self.root/'profile/hh_browser_check',confirmation_channel=self.confirm_channel,
                    result_channel=self.result_channel)
                return {'confirmed_by_site':'HH подтвердил доставку отклика.',
                    'already_applied_on_hh':'На эту вакансию уже был отклик.',
                    'cancelled_before_send':'Отправка отменена. Письмо сохранено.',
                    'submitted_unverified':'Результат неизвестен. Проверьте отклики на HH и отметьте результат в «Истории».'}.get(state,state)
        self.start('hh',work)

    def login(self):
        def work():
            from playwright.sync_api import sync_playwright
            from browser_check import navigate,check_blocker
            with hunt.one_run(self.root/'profile/hunt.lock'),sync_playwright() as p:
                context=p.chromium.launch_persistent_context(str(self.root/'profile/hh_browser_check'),headless=False)
                try:
                    page=context.new_page();navigate(page,'https://hh.kz/','Ручной вход')
                    with self.guard:
                        self.answer='';self.confirm_event.clear()
                        self.confirmation={'attempt':secrets.token_urlsafe(24),'login':True}
                        self.task['message']='Войдите в HH в открытом браузере, затем нажмите «Готово» здесь.'
                    if not self.confirm_event.wait(600):return 'Время ожидания входа истекло.'
                    if not self.answer:return 'Вход закрыт.'
                    from browser_check import verify_login
                    if not verify_login(context):
                        return 'Вход не подтверждён: HH всё ещё просит войти. Нажмите «Вход в HH» ещё раз и войдите полностью.'
                    return 'Вход в HH проверен и сохранён.'
                finally:context.close()
        self.start('login',work)

    def login_done(self,data):
        with self.guard:
            if not self.confirmation or not self.confirmation.get('login') or data.get('attempt')!=self.confirmation['attempt']:
                raise CheckError('Это окно входа уже закрыто.')
            self.answer='' if data.get('cancel') else 'done'
            self.confirmation=None;self.confirm_event.set()

    ACTIONS={'confirm','login-done','search','rewrite','retry','prepare','login','view','settings','restore','resolve',
             'letter','defer','skip','manual-sent'}

    def mutate(self,action,data):
        if action not in self.ACTIONS:raise CheckError('Неизвестное действие.')  # before any key lookup (audit A37)
        if action=='confirm':return self.confirm(data)
        if action=='login-done':return self.login_done(data)
        if action=='search':return self.search()
        if action=='rewrite':return self.rewrite(data.get('key'))
        if action=='retry':return self.retry(data.get('key'))
        if action=='prepare':return self.prepare_hh(data.get('key'))
        if action=='login':return self.login()
        if action=='view':
            with self.store() as store:
                path,packet=self.packet(store,data.get('key'))
                draft_queue.mark_reviewed(store.connection,path,packet)
            return
        with self.guard:
            if self.task['busy']:raise CheckError('Дождитесь завершения текущего действия.')
            with hunt.one_run(self.root/'profile/hunt.lock'):
                if action=='settings':
                    cfg=search_settings.validate(data['settings'])
                    config=sources.validate(data['sources'])
                    excluded=data.get('excluded_companies',[])
                    if not isinstance(excluded,list) or len(excluded)>50 or not all(isinstance(v,str) and v.strip() for v in excluded):
                        raise CheckError('Укажите исключённые компании отдельными строками.')
                    prefs=load_preferences(self.root/'profile')
                    if not isinstance(prefs,dict):raise CheckError('Файл profile/preferences.json не найден; настройки не сохранены.')
                    search_settings.save(self.root/'profile',cfg)
                    sources.save(self.root/'profile',config)
                    prefs['excluded_companies']=excluded
                    temp=self.root/'profile/preferences.tmp'
                    temp.write_text(json.dumps(prefs,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                    from common import replace_file
                    replace_file(temp,self.root/'profile/preferences.json')
                    return
                with self.store() as store:
                    if action=='restore':return actions.restore(self.root,store,data.get('key'))
                    if action=='resolve':
                        # The user checked HH after an unknown result. Exact phrases; never a network action.
                        _,identity=actions.unresolved_attempt(store,data.get('url'))
                        outcome={f'ОТКЛИК ЕСТЬ {identity}':'delivered',f'ОТКЛИКА НЕТ {identity}':'not_sent'}.get(data.get('text'))
                        if not outcome:raise CheckError('Введите фразу точно как показано.')
                        return actions.resolve_attempt(self.root,store,data['url'],outcome)
                    path,packet=self.packet(store,data.get('key'))
                    key=data.get('key')
                    if action=='letter':actions.save_letter(self.root,store,key,data.get('text'))
                    elif action=='defer':actions.defer(self.root,store,key)
                    elif action=='skip':actions.skip(self.root,store,key)
                    elif action=='manual-sent':
                        if data.get('text')!=f"ОТПРАВЛЕНО {packet['vacancy_id']}":raise CheckError('Нужно точное подтверждение ручной отправки.')
                        actions.manual_sent(self.root,store,key)
                    else:raise CheckError('Неизвестное действие.')


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=False  # on Windows SO_REUSEADDR lets a second copy bind the same port (audit A24)

    def __init__(self,address,app):
        super().__init__(address,Handler)
        self.app=app

    def server_bind(self):
        import socket
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def reply(self,code,body,kind='application/json; charset=utf-8'):
        if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode('utf-8')
        try:self._send(code,body,kind)
        except (ConnectionAbortedError,ConnectionResetError,BrokenPipeError):pass  # the tab closed first (audit A23)

    def _send(self,code,body,kind):
        self.send_response(code)
        for key,value in {'Content-Type':kind,'Content-Length':str(len(body)),'Cache-Control':'no-store',
            'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():self.send_header(key,value)
        self.end_headers();self.wfile.write(body)

    def origin_ok(self):
        expected=f'127.0.0.1:{self.server.server_port}'
        return (self.headers.get('Host')==expected and self.headers.get('Origin') in (None,'http://'+expected)
            and self.headers.get('Sec-Fetch-Site') not in {'cross-site'})

    def do_GET(self):
        if not self.origin_ok():return self.reply(403,{'error':'Недопустимый адрес запроса.'})
        path=urlsplit(self.path).path
        try:
            if path=='/api/health':return self.reply(200,{'app':'personal-job-hunter','root':hashlib.sha256(str(self.server.app.root).encode()).hexdigest()})
            if path=='/api/state':return self.reply(200,self.server.app.state())
            static={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
            if path in static:
                name,kind=static[path];return self.reply(200,(ASSETS/name).read_bytes(),kind)
            self.reply(404,{'error':'Страница не найдена.'})
        except Exception:self.reply(500,{'error':'Не удалось прочитать локальные данные. Проверьте файлы настроек.'})

    def do_POST(self):
        # Drain bounded request bodies before rejecting a local client; otherwise
        # Windows may reset the socket before the client receives the error.
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:return self.reply(400,{'error':'Некорректная длина запроса.'})
        if length<=0:return self.reply(400,{'error':'Пустой запрос.'})
        if length>131072:return self.reply(413,{'error':'Слишком большой запрос.'})
        raw=self.rfile.read(length)
        if not self.origin_ok() or not secrets.compare_digest(self.headers.get('X-Hunter-Token',''),self.server.app.csrf):
            return self.reply(403,{'error':'Обновите страницу приложения.'})
        try:
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise CheckError('Нужен JSON.')
            data=json.loads(raw)
            if not isinstance(data,dict):raise CheckError('Некорректный запрос.')
            action=urlsplit(self.path).path.removeprefix('/api/')
            if action=='shutdown':
                if self.server.app.task['busy']:raise CheckError('Дождитесь завершения текущей операции.')
                self.reply(200,{'ok':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            self.server.app.mutate(action,data)
            self.reply(200,{'ok':True})
        except CheckError as exc:self.reply(409,{'error':str(exc)})
        except (ValueError,KeyError,TypeError):self.reply(400,{'error':'Проверьте введённые значения.'})
        except OSError:self.reply(409,{'error':'Изменение НЕ сохранено: файл занят другой программой. Повторите через несколько секунд.'})
        except Exception:self.reply(500,{'error':'Действие остановлено из-за ошибки программы. Обновите страницу и проверьте, сохранилось ли изменение.'})


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    root_id=hashlib.sha256(str(ROOT).encode()).hexdigest()
    if args.port:
        try:
            with urlopen(f'http://127.0.0.1:{args.port}/api/health',timeout=1) as response:health=json.load(response)
            if health.get('app')=='personal-job-hunter' and health.get('root')==root_id:
                if not args.no_browser:webbrowser.open(f'http://127.0.0.1:{args.port}/')
                return
        except Exception:pass
    try:server=Server(('127.0.0.1',args.port),App())
    except OSError:server=Server(('127.0.0.1',0),App())
    url=f'http://127.0.0.1:{server.server_port}/'
    if not args.no_browser:webbrowser.open(url)
    if sys.stdout:print(url,flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
