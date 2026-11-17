"""Personal Telegram bot: notifications, letter cards and the same actions as the web UI. No paid actions."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from html import escape
import json
from pathlib import Path
import sqlite3
import threading

import actions
from common import CheckError
from preferences import load_preferences
import run_lock
from run_lock import RunBusy
import run_status
import search_settings
import sources
from storage import JobStore
import telegram_api
import telegram_format as fmt
import telegram_store
import work_queue

ROOT=Path(__file__).resolve().parent
QUEUED='Программа сейчас занята (подготовка писем или отклик HH). Применю, как только освободится.'
EDIT_MINUTES=30
MUTATING={'df','sk','rs','my','sv'}
HELP=('Команды:\n/letters — письма в очереди\n/status — последний и следующий запуск, проблемы\n'
      '/attention — что требует внимания\n/help — эта подсказка\n\n'
      'После каждой подготовки приходит одно сообщение со списком вакансий: нажмите на вакансию — откроются вакансия и письмо.\n'
      '«🚀 Откликнуться на HH»: ноутбук заполняет форму HH, бот присылает скриншот и 4-значный код; отклик уходит только после кода. '
      'Ноутбук должен быть включён.\n'
      'Кнопки карточки: изменить письмо, «🤖 Переписать с ИИ» по вашему указанию (платно, ~$0.02–0.03), отложить, не подходит, отправлено вручную, CV, вакансия, «⬅️ К списку писем».\n'
      'Меню внизу чата: письма, внимание, состояние, помощь.')
# Persistent menu under the input field; each button sends its text, which works like the command.
MENU=[['📬 Письма','⚠️ Внимание'],['ℹ️ Состояние','❓ Помощь']]
MENU_COMMANDS={'📬 Письма':'/letters','⚠️ Внимание':'/attention','ℹ️ Состояние':'/status','❓ Помощь':'/help'}
COMMANDS=[('letters','Письма в очереди'),('attention','Что требует внимания'),('status','Последний и следующий запуск'),
          ('help','Подсказка и меню')]
MENU_VERSION=2
CODE_SECONDS=600   # time to answer with the one-time code; also the wait for «Проверить ещё раз»
CODE_ATTEMPTS=3
HH_RESULTS={'confirmed_by_site':'✅ Отклик отправлен: HH подтвердил доставку.',
            'already_applied_on_hh':'На эту вакансию уже был отклик на HH. Отмечено в истории.',
            'cancelled_before_send':'Отправка отменена — ничего не отправлено. Письмо осталось в очереди.',
            'submitted_unverified':'⚠️ Кнопка на HH нажата, но HH не показал подтверждение. Проверьте «Отклики» на HH. '
                                   'Повторная отправка заблокирована.'}


class BotApp:
    def __init__(self,root,api,chat_id,db_path=None,now=lambda:datetime.now(timezone.utc)):
        self.root=Path(root);self.api=api;self.chat_id=str(chat_id)
        self.db_path=Path(db_path or self.root/'data/jobs.sqlite');self.now=now
        self.lock_path=self.root/'profile/hunt.lock'
        self.sticky_error=''  # 'forbidden' stays visible until a message is delivered again
        self.rewriter=self.ai_rewrite
        self.sender=None       # hh_response.run; replaceable in tests
        self.sending=None      # the one HH response in progress (in memory: a bot restart closes the browser unsent)
        self.hh_lock=threading.Lock()
        self.worker=None

    # --- infrastructure -------------------------------------------------
    def store(self):return JobStore(self.db_path)

    @contextmanager
    def db(self):
        # sqlite3's own context manager commits but never closes; Windows then cannot remove/replace the file.
        connection=sqlite3.connect(self.db_path)
        try:yield connection
        finally:connection.close()

    def state(self,name,default=None):
        with self.db() as db:return telegram_store.get_state(db,name,default)

    def owner(self,chat):
        return str((chat or {}).get('id'))==self.chat_id and (chat or {}).get('type')=='private'

    def say(self,text,keyboard=None):
        return self.api.send_message(self.chat_id,text,keyboard)

    def show(self,message_id,text,keyboard=None,chat_id=None):
        """Replace the pressed message in place (one message per run); a new message only if it cannot be edited."""
        chat_id=chat_id or self.chat_id
        if message_id:
            # Whatever the message showed before, it no longer shows that card (review 13.11, finding 9).
            with self.db() as db:telegram_store.forget_message(db,chat_id,message_id)
            try:self.api.edit_message(chat_id,message_id,text,keyboard);return message_id
            except (telegram_api.TelegramUnauthorized,telegram_api.TelegramUnavailable,telegram_api.TelegramRetryAfter):
                raise  # the edit may have happened: a new message could duplicate it
            except telegram_api.TelegramError:pass  # deleted or too old to edit
        return self.api.send_message(chat_id,text,keyboard)['message_id']

    def list_view(self,store,head='',undo=None):
        import draft_queue
        entries=actions.pending(self.root,store)
        unread=draft_queue.pending(store,load_preferences(self.root/'profile'),search_settings.load(self.root/'profile'),unread_only=True,root=self.root)
        unread_keys={item['source']+':'+item['vacancy_id'] for _,item in unread}
        rows=[(item['source']+':'+item['vacancy_id'],item.get('title') or '',item.get('company') or '') for _,item in entries]
        return fmt.letter_list(rows,{key for key,_,_ in rows if key not in unread_keys},head=head,undo=undo)

    def open_card(self,store,key,message_id):
        _,text,keyboard,parts=self.card_for(store,key)
        shown=self.show(message_id,text,keyboard)
        with self.db() as db:telegram_store.remember_card(db,key,self.chat_id,shown)
        for part in parts:self.say(part)

    # --- updates --------------------------------------------------------
    def handle_update(self,update):
        if 'callback_query' in update:
            query=update['callback_query'];message=query.get('message') or {}
            if self.owner(message.get('chat')):self.on_callback(query)
        elif 'message' in update:
            message=update['message']
            if not self.owner(message.get('chat')):return
            text=str(message.get('text') or '')
            if text.startswith('/'):self.on_command(text.split()[0].split('@')[0].lower())
            else:self.on_text(text)

    def on_command(self,command):
        handlers={'/letters':self.cmd_letters,'/status':self.cmd_status,'/attention':self.cmd_attention,
                  '/start':lambda:self.api.send_message(self.chat_id,'Бот подключён. Меню — внизу чата.',menu=MENU)}
        handlers.get(command,lambda:self.api.send_message(self.chat_id,escape(HELP),menu=MENU))()

    def announce_menu(self):
        """Register the Telegram «Menu» commands on every start; show the bottom menu once per menu version."""
        self.api.set_commands(COMMANDS)
        if self.state('menu_version')!=MENU_VERSION:
            self.api.send_message(self.chat_id,'🆕 Бот обновлён.\n• После подготовки приходит одно сообщение со списком '
                                  'вакансий; вакансия и письмо открываются в нём же.\n• «🚀 Откликнуться на HH» в карточке: '
                                  'скриншот формы и 4-значный код, отклик уходит только после кода.\nМеню — внизу чата.',menu=MENU)
            with self.db() as db:telegram_store.set_state(db,'menu_version',MENU_VERSION)

    def on_text(self,text):
        if text.strip() in MENU_COMMANDS:
            # A menu tap is never the new letter text, even in edit mode; the edit stays open.
            self.on_command(MENU_COMMANDS[text.strip()]);return
        sending=self.sending
        if sending and sending.get('stage')=='code':return self.on_code(text,sending)
        if self.state('ai_edit'):return self.on_instruction(text)
        edit=self.state('edit')
        if not edit:
            self.say('Чтобы изменить письмо, нажмите «✏️ Изменить» в его карточке. /help — команды.');return
        if datetime.fromisoformat(edit['expires_at'])<self.now():
            self.clear_edit()
            self.say('Режим правки истёк. Нажмите «✏️ Изменить» в карточке ещё раз.');return
        if not text.strip() or len(text)>actions.MAX_LETTER:
            self.say('Письмо должно содержать от 1 до 12 000 символов. Отправьте текст ещё раз.');return
        keyboard=[[{'text':'💾 Сохранить','callback_data':fmt.callback('sv',edit['key'])},
                   {'text':'Отмена','callback_data':fmt.callback('cx',edit['key'])}]]
        self.preview(edit['key'],text,'Новый текст письма:\n\n'+escape(text),keyboard)

    def preview(self,key,text,shown,keyboard):
        """Show a draft; only the «Сохранить» under the LAST preview saves (an older button would save other text)."""
        with self.db() as db:telegram_store.set_state(db,'edit_draft',{'key':key,'text':text})
        parts=fmt.split_text(shown);message=None
        for index,part in enumerate(parts):message=self.say(part,keyboard if index==len(parts)-1 else None)
        with self.db() as db:
            telegram_store.set_state(db,'edit_draft',{'key':key,'text':text,'message_id':(message or {}).get('message_id')})

    def current_draft(self,key,message_id):
        draft=self.state('edit_draft')
        if not draft or draft.get('key')!=key:raise CheckError('Нет нового текста для сохранения. Нажмите «✏️ Изменить».')
        if message_id and draft.get('message_id') and int(message_id)!=int(draft['message_id']):
            raise CheckError('Это не последний вариант письма. Нажмите «💾 Сохранить» под последним вариантом.')
        return draft

    def clear_edit(self):
        with self.db() as db:
            for name in ('edit','edit_draft','ai_edit'):telegram_store.clear_state(db,name)

    # --- AI rewrite by the user's instruction (paid) --------------------------
    @staticmethod
    def ai_rewrite(root,store,key,instruction,base_text=None):
        import os,requests,letter_rewrite
        session=requests.Session()
        try:
            return letter_rewrite.rewrite(root,store,key,instruction,base_text=base_text,session=session,
                api_key=os.getenv('OPENROUTER_API_KEY','').strip(),
                model=os.getenv('OPENROUTER_MODEL','').strip() or __import__('assess').DEFAULT_MODEL,
                audit_model=os.getenv('OPENROUTER_AUDIT_MODEL','').strip() or None)
        finally:session.close()

    def on_instruction(self,text):
        mode=self.state('ai_edit')
        if datetime.fromisoformat(mode['expires_at'])<self.now():
            self.clear_edit();self.say('Режим «Переписать с ИИ» истёк. Нажмите кнопку в карточке ещё раз.');return
        key=mode['key'];draft=self.state('edit_draft')
        base=draft['text'] if draft and draft.get('key')==key else None
        self.say('🤖 Переписываю и проверяю факты… (до минуты)')
        try:
            with self.store() as store:result=self.rewriter(self.root,store,key,text,base_text=base)
        except CheckError as exc:
            self.say('Не получилось: '+escape(str(exc))+'\nПисьмо не изменено. Напишите другое указание или нажмите «Отмена».',
                     [[{'text':'Отмена','callback_data':fmt.callback('cx',key)}]]);return
        cost=f" · стоимость ${result['cost']:.3f}" if isinstance(result.get('cost'),(int,float)) else ''
        keyboard=[[{'text':'💾 Сохранить','callback_data':fmt.callback('sv',key)},{'text':'Отмена','callback_data':fmt.callback('cx',key)}]]
        self.preview(key,result['letter'],f'🤖 Новый вариант (проверен по CV{cost}):\n\n'+escape(result['letter'])+
                     '\n\n<i>Можно написать ещё указание — изменю этот вариант.</i>',keyboard)

    # --- HH response from the bot: letter text, screenshot, one-time code, one click ---------------
    # self.sending is shared by the worker thread and the main loop; every change goes through self.hh_lock.
    # The first decision (correct code, cancel, attempt limit, timeout) is final: nothing later can approve.
    def quiet_say(self,text,keyboard=None):
        try:return self.say(text,keyboard)
        except telegram_api.TelegramUnauthorized:raise
        except Exception as exc:log.warning('Сообщение об отправке HH не доставлено: %s',type(exc).__name__)

    def say_retry(self,text,keyboard=None,tries=3,pause=2):
        import time
        for attempt in range(tries):
            try:return self.say(text,keyboard)
            except (telegram_api.TelegramUnavailable,telegram_api.TelegramRetryAfter):
                if attempt==tries-1:raise
                time.sleep(pause)

    def start_hh_send(self,key):
        if self.sending:raise CheckError('Уже идёт отправка отклика. Дождитесь её завершения или нажмите «Отмена».')
        if run_lock.is_busy(self.lock_path):raise CheckError('Сейчас идёт подготовка писем. Попробуйте через несколько минут.')
        with self.store() as store:_,item=actions.packet(self.root,store,key)
        if item['source']!='hh':raise CheckError('На сайте компании откликаются вручную.')
        label=f"{item.get('title') or 'Вакансия'} · {item.get('company') or ''}"
        with self.hh_lock:
            if self.sending:raise CheckError('Уже идёт отправка отклика. Дождитесь её завершения или нажмите «Отмена».')
            self.sending={'key':key,'label':label,'event':threading.Event(),'answer':'','code':'','attempts':0,
                          'stage':'preparing','decided':False}
        try:
            self.worker=threading.Thread(target=self.hh_send_work,args=(key,),daemon=True,name='hh-send')
            self.worker.start()
        except Exception:
            with self.hh_lock:self.sending=None
            raise
        self.quiet_say(f'🚀 «{escape(label)}»: открываю HH на ноутбуке и заполняю форму (1–2 минуты). '
                       'Без вашего кода ничего не отправится.')
        return 'Готовлю форму HH…'

    def hh_send_work(self,key):
        sending=self.sending;text=''
        try:
            with run_lock.one_run(self.lock_path),self.store() as store:
                _,packet=actions.packet(self.root,store,key)
                packet={**packet,'output_dir':str(self.root/'drafts/runs'/('tg-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')))}
                sending['letter_path']=packet['letter_path']
                if self.sender is None:
                    import hh_response
                    self.sender=hh_response.run
                state=self.sender(packet,store.connection,allow_send=True,profile_dir=self.root/'profile/hh_browser_check',
                                  confirmation_channel=self.hh_confirm,result_channel=self.hh_result)
            text=HH_RESULTS.get(state,str(state))
        except RunBusy:text='Программа занята подготовкой писем; отправка не начата. Попробуйте позже.'
        except CheckError as exc:
            text='Отклик не отправлен: '+str(exc)
            if 'на вход' in str(exc):
                text+=(' Один раз войдите в HH на ноутбуке: двойной щелчок по «hh_login.cmd» в папке программы, '
                       'войдите и закройте окно. Потом нажмите «🚀 Откликнуться на HH» ещё раз.')
        except Exception as exc:
            log.error('Отправка HH остановлена ошибкой %s.',type(exc).__name__)
            text=(f'Отклик не отправлен: ошибка {type(exc).__name__}. Если HH просит вход или капчу — '
                  'откройте программу на ноутбуке. Повторная отправка разрешена, только если кнопка на HH не нажималась.')
        finally:
            with self.hh_lock:self.sending=None
        try:
            with self.store() as store:keyboard=self.list_view(store)[1]
            self.say(f"«{escape(sending['label'])}»: {escape(text)}",keyboard)
        except telegram_api.TelegramError as exc:log.warning('Итог отправки HH не доставлен: %s',exc)
        except Exception as exc:log.warning('Итог отправки HH: %s',type(exc).__name__)

    def hh_decide(self,sending,answer):
        """Record the one decision; False when it was already taken (or a code arrives after a cancel)."""
        with self.hh_lock:
            if sending.get('decided') or sending.get('stage')!='code':return False
            if answer=='ok' and sending.get('cancelled'):return False
            sending['decided']=True;sending['answer']=answer
        sending['event'].set()
        return True

    def hh_confirm(self,review,token):
        import secrets
        sending=self.sending
        with self.hh_lock:
            if sending is None or sending.get('cancelled'):return ''
            sending['event'].clear()
            # Set before anything is sent: a fast reply must already be understood.
            sending.update(code=f'{secrets.randbelow(10000):04d}',attempts=0,answer='',decided=False,stage='code')
        letter=''
        try:letter=Path(sending.get('letter_path') or review.get('letter_path') or '').read_text(encoding='utf-8')
        except OSError:pass
        if letter:
            for part in fmt.split_text('✉️ Это письмо уйдёт работодателю:\n\n'+escape(letter)):self.say_retry(part)
        caption=(f"📝 Форма HH заполнена: {escape(sending['label'])}\nРезюме на HH: {escape(str(review.get('hh_resume_title') or ''))}\n"
                 f"Письмо — в сообщении выше. Чтобы отправить отклик, пришлите код <b>{sending['code']}</b> "
                 f"({CODE_SECONDS//60 or 1} мин). Отправка — один раз, без повторов.")
        keyboard=[[{'text':'Отмена','callback_data':fmt.callback('hx',sending['key'])}]]
        try:self.api.send_photo(self.chat_id,review['form_image'],caption,keyboard)
        except Exception as exc:
            log.warning('Скриншот формы не отправлен: %s',type(exc).__name__)
            self.say_retry(caption+'\n(Скриншот не отправился — форма открыта на ноутбуке.)',keyboard)
        sending['event'].wait(CODE_SECONDS)
        with self.hh_lock:
            timed_out=not sending['decided']
            if timed_out:sending['decided']=True;sending['answer']=''
            approved=sending['answer']=='ok' and not sending.get('cancelled')
            sending['stage']='sending' if approved else 'closed'
        if timed_out:self.quiet_say('Время на код истекло. Отклик не отправлен.')
        return token if approved else ''

    def on_code(self,text,sending):
        if not text.strip():
            self.quiet_say('Пришлите 4-значный код цифрами или нажмите «Отмена».');return
        if sending.get('decided'):
            self.quiet_say('Решение по этому отклику уже принято. Если отправка отменена — нажмите «🚀 Откликнуться на HH» заново.');return
        if text.strip()==sending['code']:
            if self.hh_decide(sending,'ok'):self.quiet_say('Код верный. Отправляю отклик…')
            else:self.quiet_say('Отправка уже отменена — отклик не отправлен.')
            return
        with self.hh_lock:
            sending['attempts']+=1;left=CODE_ATTEMPTS-sending['attempts']
        if left<=0:
            self.hh_decide(sending,'');self.quiet_say('Три неверных кода — отправка отменена.');return
        self.quiet_say(f'Неверный код. Осталось попыток: {left}. Отмена — кнопкой под скриншотом.')

    def hh_result(self,review):
        """HH did not confirm at once (slow page, captcha): keep the browser open until the user checks."""
        sending=self.sending
        if sending is None:return
        with self.hh_lock:sending['event'].clear();sending['stage']='result'
        self.quiet_say('HH не показал подтверждение сразу — возможно, капча. Пройдите её на ноутбуке или проверьте «Отклики» в HH, '
                       'затем нажмите кнопку. Повторно отклик не отправляется.',
                       [[{'text':'🔄 Проверить ещё раз','callback_data':fmt.callback('hc',sending['key'])}]])
        sending['event'].wait(CODE_SECONDS)

    def hh_control(self,code,key):
        sending=self.sending
        if not sending:return 'Отправка уже завершена.'
        if sending['key']!=key:return 'Кнопка устарела: она от другой отправки.'
        if code=='hc':
            with self.hh_lock:
                if sending.get('stage')!='result':return 'Сейчас нечего проверять.'
            sending['event'].set();return 'Проверяю страницу HH…'
        with self.hh_lock:
            stage=sending.get('stage')
            if stage in {'preparing','code'} and not sending.get('decided'):sending['cancelled']=True
        if stage=='code':
            self.hh_decide(sending,'')
            return 'Отменяю.' if sending['answer']!='ok' else 'Код уже принят — отклик отправляется.'
        return {'preparing':'Отменю, как только форма будет готова — ничего не отправится.',
                'sending':'Отклик уже отправляется — отменить нельзя.',
                'result':'Отклик уже отправлен, HH проверяется.'}.get(stage,'Отправка уже остановлена.')

    def on_callback(self,query):
        parsed=fmt.parse_callback(query.get('data'))
        message=query.get('message') or {}
        if not parsed:
            self.api.answer_callback(query['id'],'Кнопка устарела.');return
        code,key=parsed;chat=self.chat_id;message_id=message.get('message_id')
        try:
            if code in MUTATING:result=self.perform(code,key,chat_id=chat,message_id=message_id)
            else:result=self.view(code,key,message_id)
        except telegram_api.TelegramUnauthorized:raise
        except telegram_api.TelegramError as exc:
            log.warning('Сообщение после действия не обновлено: %s',exc)
            self.api.answer_callback(query['id'],'Готово, но обновить сообщение не удалось.');return
        except CheckError as exc:
            self.api.answer_callback(query['id'],str(exc))
            if str(exc)==actions.GONE and message_id:
                with self.store() as store:self.show(message_id,*self.list_view(store,head='<i>Это письмо уже обработано.</i>'))
            return
        self.api.answer_callback(query['id'],result or '')

    # --- actions ---------------------------------------------------------
    def perform(self,code,key,*,chat_id=None,message_id=None,data=None):
        """Change data under the shared program lock; if a preparation runs, remember and apply later."""
        try:
            with run_lock.one_run(self.lock_path),self.store() as store:
                return self.apply(store,code,key,chat_id or self.chat_id,message_id,data or {})
        except RunBusy:
            data=dict(data or {})
            if code=='sv':
                data['text']=self.current_draft(key,message_id)['text'];self.clear_edit()
            with self.db() as db:
                telegram_store.queue_action(db,code,{'key':key,'chat_id':chat_id or self.chat_id,'message_id':message_id,**data})
            return QUEUED

    def apply(self,store,code,key,chat_id,message_id,data):
        """After an action the same message goes back to the list, with the result on top."""
        def label(item):return f"{escape(item.get('title') or 'Вакансия')} · {escape(item.get('company') or '')}"
        def back_to_list(note,undo=None):
            self.show(message_id,*self.list_view(store,head=note,undo=undo),chat_id=chat_id)
        if code=='df':
            _,item=actions.packet(self.root,store,key);actions.defer(self.root,store,key)
            back_to_list('⏸ Отложено: '+label(item)+'. Письмо осталось в списке внизу.')
            return 'Отложено.'
        if code=='sk':
            info=actions.skip(self.root,store,key)
            back_to_list('❌ Исключено: '+label(info),undo=(key,info.get('title') or 'вакансию'))
            return 'Исключено. Можно вернуть.'
        if code=='rs':
            actions.restore(self.root,store,key)
            source,identity=key.split(':');row=store.get(identity,source)
            back_to_list('↩️ Возвращено в отбор: '+label(dict(row) if row else {}))
            return 'Возвращено в отбор.'
        if code=='my':
            _,item=actions.packet(self.root,store,key);actions.manual_sent(self.root,store,key)
            back_to_list('✅ Отмечено: отклик отправлен вами — '+label(item))
            return 'Отклик отмечен в истории.'
        if code=='sv':return self.save_edit(store,key,chat_id,message_id,data.get('text'))
        raise CheckError('Неизвестное действие.')

    def save_edit(self,store,key,chat_id,message_id,text=None):
        from_draft=text is None  # a queued save carries its own text; a newer edit in progress is left alone
        if from_draft:text=self.current_draft(key,message_id)['text']
        actions.save_letter(self.root,store,key,text)
        if from_draft:self.clear_edit()
        if message_id:
            try:self.api.edit_message(chat_id,message_id,'💾 Письмо сохранено.',None)
            except telegram_api.TelegramUnauthorized:raise
            except telegram_api.TelegramError:pass
        with self.db() as db:known=telegram_store.card(db,key)
        if known:
            _,text,keyboard,parts=self.card_for(store,key)
            try:
                self.api.edit_message(known[0],known[1],text,keyboard)
                for part in parts:self.say(part)
            except telegram_api.TelegramUnauthorized:raise
            except telegram_api.TelegramError:self.send_key(store,key)  # the old card was deleted: send a fresh one
        else:self.send_key(store,key)
        return 'Письмо сохранено.'

    # --- read-only views --------------------------------------------------
    def card_for(self,store,key):
        path,item=actions.packet(self.root,store,key)
        letter=Path(item['letter_path']).read_text(encoding='utf-8')
        source,identity=key.split(':');row=store.get(identity,source)
        text,keyboard,parts=fmt.card(item,letter,actions.is_user_edited(store,item),
            url=sources.canonical_url(item['url']),region=(row['region'] if row else '') or '',
            published=(row['published_at'] if row else '') or '')
        return path,text,keyboard,parts

    def send_key(self,store,key):
        _,text,keyboard,parts=self.card_for(store,key)
        message=self.say(text,keyboard)
        with self.db() as db:telegram_store.remember_card(db,key,self.chat_id,message['message_id'])
        for part in parts:
            try:self.say(part)
            except telegram_api.TelegramUnauthorized:raise
            except telegram_api.TelegramError as exc:
                # The card itself is delivered; a lost part is recoverable via «Изменить» (shows the full text).
                log.warning('Часть письма не отправлена: %s',exc);break

    def send_card(self,packet_path):
        """False when the letter is no longer in the queue (processed on the site before sending)."""
        target=Path(packet_path).resolve()
        with self.store() as store:
            for path,item in actions.pending(self.root,store):
                if path.resolve()==target:
                    self.send_key(store,item['source']+':'+item['vacancy_id']);return True
        return False

    def view(self,code,key,message_id):
        if code=='hs':return self.start_hh_send(key)
        if code in {'hx','hc'}:return self.hh_control(code,key)
        with self.store() as store:
            if code=='ls':self.show(message_id,*self.list_view(store));return ''
            if code=='op':self.open_card(store,key,message_id);return ''
            if code=='cv':
                _,item=actions.packet(self.root,store,key)
                cv=actions.cv_for_sending(item).resolve()
                if not cv.is_file() or not cv.is_relative_to((self.root/'profile').resolve()):
                    raise CheckError('Файл CV вне папки profile или отсутствует — не отправляю.')
                self.api.send_document(self.chat_id,cv,caption=f"CV для: {item.get('title') or 'вакансии'}")
                return 'CV отправлен.'
            if code=='ms':
                _,item=actions.packet(self.root,store,key)
                self.api.edit_message(self.chat_id,message_id,
                    f"Отметить отклик на «{escape(item.get('title') or 'вакансию')}» как отправленный вами?\n"
                    'После отметки программа не будет сама отправлять отклик на эту вакансию.',
                    [[{'text':'Да, отправлено','callback_data':fmt.callback('my',key)},
                      {'text':'Отмена','callback_data':fmt.callback('mn',key)}]])
                return ''
            if code=='mn':
                _,text,keyboard,_=self.card_for(store,key)
                self.api.edit_message(self.chat_id,message_id,text,keyboard);return 'Отменено.'
            if code in {'ed','cx'}:return self.edit_view(store,code,key,message_id)
            if code=='ai':
                _,item=actions.packet(self.root,store,key)
                with self.db() as db:
                    for name in ('edit','edit_draft'):telegram_store.clear_state(db,name)
                    telegram_store.set_state(db,'ai_edit',{'key':key,'expires_at':(self.now()+timedelta(minutes=EDIT_MINUTES)).isoformat()})
                self.say(f"🤖 Письмо «{escape(item.get('title') or 'вакансия')}». Напишите, что изменить: например, "
                         '«убери второе предложение» или «добавь, что хочу работать в сильной команде». '
                         'Каждая попытка платная, примерно $0.02–0.03. Письмо изменится только после «💾 Сохранить».',
                         [[{'text':'Отмена','callback_data':fmt.callback('cx',key)}]])
                return 'Жду указание.'
        raise CheckError('Неизвестное действие.')

    def edit_view(self,store,code,key,message_id):
        if code=='cx':
            self.clear_edit()
            if message_id:self.api.edit_message(self.chat_id,message_id,'Правка отменена. Письмо не изменено.',[[fmt.back(key)]])
            return 'Отменено.'
        _,item=actions.packet(self.root,store,key)
        letter=Path(item['letter_path']).read_text(encoding='utf-8')
        with self.db() as db:
            for name in ('edit_draft','ai_edit'):telegram_store.clear_state(db,name)
            telegram_store.set_state(db,'edit',{'key':key,'expires_at':(self.now()+timedelta(minutes=EDIT_MINUTES)).isoformat()})
        self.say(f"✏️ Правка письма «{escape(item.get('title') or 'вакансия')}». Скопируйте текст ниже, исправьте "
                 f'и отправьте целиком одним сообщением ({EDIT_MINUTES} минут).')
        for part in fmt.split_text(escape(letter)):self.say(part)
        return 'Жду новый текст письма.'

    # --- commands ----------------------------------------------------------
    def cmd_letters(self):
        with self.store() as store:self.say(*self.list_view(store))

    def next_window(self):
        local=self.now().astimezone(fmt.LOCAL)
        for hour in (10,18):
            if local.hour<hour:return f'{hour}:00'
        return 'завтра 10:00'

    def cmd_status(self):
        report={}
        try:report=json.loads((self.root/'data/last_scheduled_run.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        lines=['ℹ️ <b>Состояние</b>']
        if report.get('started_at'):lines.append(f"Последний плановый запуск: {fmt.local_time(report['started_at'])} · {escape(str(report.get('state') or ''))}")
        lines.append('Следующее окно: '+self.next_window())
        if run_lock.is_busy(self.lock_path):lines.append('Сейчас идёт подготовка.')
        else:
            problem=run_status.problem(report)
            lines.append('⚠️ '+escape(problem) if problem else 'Проблем нет.')
        self.say('\n'.join(lines))

    def cmd_attention(self):
        with self.store() as store:
            items=work_queue.issues(store,load_preferences(self.root/'profile'),search_settings.load(self.root/'profile'))
        if not items:self.say('Ничего не требует внимания.');return
        lines=['⚠️ <b>Требуют внимания</b>']
        for item in items[:20]:
            error=' '.join(str(item.get('error') or '').split())
            error=error[:200]+('…' if len(error)>200 else '')
            lines.append(f"• {escape(item.get('title') or 'Вакансия')} · {escape(item.get('company') or '')} — {escape(error)}")
        if len(items)>20:lines.append(f'…и ещё {len(items)-20} — на сайте программы.')
        lines.append('\nПовтор подготовки платный и запускается на сайте.')
        for part in fmt.split_text('\n'.join(lines)):self.say(part)

    # --- background duties ------------------------------------------------
    def heartbeat(self,error=''):
        from common import write_json
        import os
        if error=='forbidden':self.sticky_error=error
        try:
            write_json(self.root/'data/telegram_bot_heartbeat.json',
                       {'pid':os.getpid(),'updated_at':datetime.now(timezone.utc).isoformat(),'last_error':error or self.sticky_error})
        except OSError as exc:log.warning('Отметка «жив» не записана: %s',type(exc).__name__)

    GIVE_UP=3

    def send_outbox(self):
        """Deliver queued notifications in order. Network trouble pauses the queue; a broken item is skipped
        and given up after GIVE_UP attempts, so it never blocks later notifications."""
        sent=0
        with self.db() as db:items=telegram_store.due_outbox(db)
        for item in items:
            try:
                payload=item['payload']
                if item['kind']=='digest':
                    # 'head': plain text of a non-run event, e.g. a recheck of saved vacancies (17.11.2026).
                    head=fmt.escape(str(payload['head'])) if payload.get('head') else fmt.summary(payload)
                    with self.store() as store:self.say(*self.list_view(store,head=head))
                elif item['kind']=='summary':self.say(fmt.summary(payload))
                elif item['kind']=='problem':self.say(fmt.problem(payload.get('message','')))
                elif item['kind']=='card':
                    if not self.send_card(Path(payload['packet_path'])):log.info('Карточка пропущена: письмо уже обработано.')
                else:log.warning('Неизвестный тип уведомления: %s',item['kind'])
            except telegram_api.TelegramForbidden as exc:
                with self.db() as db:telegram_store.mark_failed(db,item['id'],str(exc))
                self.heartbeat('forbidden');log.warning('Telegram: бот заблокирован пользователем.');break
            except (telegram_api.TelegramUnavailable,telegram_api.TelegramRetryAfter) as exc:
                with self.db() as db:telegram_store.mark_failed(db,item['id'],str(exc),count=False)
                log.warning('Уведомление не отправлено, повтор позже: %s',exc);break
            except telegram_api.TelegramUnauthorized:raise
            except Exception as exc:
                detail=str(exc) if isinstance(exc,CheckError) else type(exc).__name__
                with self.db() as db:
                    telegram_store.mark_failed(db,item['id'],detail)
                    attempts=db.execute('SELECT attempts FROM telegram_outbox WHERE id=?',(item['id'],)).fetchone()[0]
                    if attempts>=self.GIVE_UP:telegram_store.mark_sent(db,item['id'])
                log.warning('Уведомление %s не отправлено (попытка %s): %s',item['kind'],attempts,detail)
                continue
            with self.db() as db:telegram_store.mark_sent(db,item['id'])
            if self.sticky_error:self.sticky_error=''
            sent+=1
        return sent

    def run_actions(self):
        """Apply button presses that arrived while a preparation held the program lock."""
        done=0
        with self.db() as db:items=telegram_store.due_actions(db)
        for item in items:
            data=item['data'];note=''
            try:
                with run_lock.one_run(self.lock_path),self.store() as store:
                    result=self.apply(store,item['action'],data['key'],data.get('chat_id') or self.chat_id,None,data)
            except RunBusy:break
            except telegram_api.TelegramUnauthorized:raise
            except telegram_api.TelegramError as exc:
                # Data changes happen before any message: the action is done, only the notice was lost.
                result='выполнено (сообщение не доставлено)';log.warning('Отложенное действие выполнено, сообщение не доставлено: %s',exc)
            except CheckError as exc:
                result='не выполнено: '+str(exc);note='Отложенное действие не выполнено: '+escape(str(exc))
            except Exception as exc:
                result='не выполнено: ошибка '+type(exc).__name__;log.error('Отложенное действие: ошибка %s',type(exc).__name__)
                note='Отложенное действие не выполнено (ошибка программы). Нажмите кнопку ещё раз.'
            with self.db() as db:telegram_store.finish_action(db,item['id'],result)
            done+=1
            if note:self.quiet_say(note)
        return done


import logging
log=logging.getLogger('telegram_bot')


def setup_logging(root):
    from logging.handlers import RotatingFileHandler
    folder=Path(root)/'data/telegram_logs';folder.mkdir(parents=True,exist_ok=True)
    handler=RotatingFileHandler(folder/'bot.log',maxBytes=1_000_000,backupCount=5,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(handler);log.setLevel(logging.INFO)
    return handler


def run_forever(app,*,sleep=None,clock=None,stop=lambda:False):
    """Long-poll Telegram; periodic outbox (5 s), deferred actions (10 s), heartbeat (60 s). 2 = bad token."""
    import time
    import telegram_api
    sleep=sleep or time.sleep;clock=clock or time.monotonic
    offset=None
    for attempt in range(60):
        try:offset=app.state('offset');break
        except Exception as exc:
            log.warning('Состояние бота не прочитано (%s); повтор через 5 с.',type(exc).__name__);sleep(5)
    delay=0;last={'outbox':float('-inf'),'actions':float('-inf'),'beat':clock()}
    app.heartbeat()  # alive at once, not after the first 50-second poll
    try:app.announce_menu()
    except telegram_api.TelegramUnauthorized:
        app.heartbeat('unauthorized');log.error('Токен Telegram неверен; бот остановлен.');return 2
    except Exception as exc:log.warning('Меню не обновлено: %s',type(exc).__name__)  # retried on the next start
    while not stop():
        try:
            updates=app.api.get_updates(offset,timeout=50);delay=0
            for update in updates:
                offset=int(update['update_id'])+1
                with app.db() as db:telegram_store.set_state(db,'offset',offset)  # before handling: a crash never replays a press
                try:app.handle_update(update)
                except telegram_api.TelegramUnauthorized:raise
                except Exception as exc:log.error('Ошибка обработки апдейта: %s',type(exc).__name__)
            now=clock()
            for name,every,duty in (('outbox',5,app.send_outbox),('actions',10,app.run_actions),('beat',60,app.heartbeat)):
                if now-last[name]>=every:
                    last[name]=now
                    try:duty()
                    except telegram_api.TelegramUnauthorized:raise
                    except Exception as exc:log.error('Ошибка фоновой задачи %s: %s',name,type(exc).__name__)
        except telegram_api.TelegramUnauthorized:
            app.heartbeat('unauthorized');log.error('Токен Telegram неверен; бот остановлен.');return 2
        except telegram_api.TelegramRetryAfter as exc:
            sleep(exc.seconds)
        except telegram_api.TelegramError as exc:
            delay=min(60,delay*2 or 5);app.heartbeat('network');log.warning('%s; пауза %s с.',exc,delay);sleep(delay)
        except Exception as exc:
            # e.g. "database is locked" while saving the offset: never let it kill the bot.
            log.error('Непредвиденная ошибка цикла: %s; пауза 5 с.',type(exc).__name__);sleep(5)
    return 0


def main():
    import subprocess,sys
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env',override=False,encoding='utf-8-sig')
    handler=setup_logging(ROOT)
    try:
        token,chat=telegram_store.settings(ROOT)
        if not token or not chat:
            log.info('Telegram не настроен (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID); бот не запущен.');return 0
        try:
            with run_lock.one_run(ROOT/'profile/telegram_bot.lock'):
                code=run_forever(BotApp(ROOT,telegram_api.Bot(token),chat))
        except RunBusy:
            log.info('Бот уже запущен; второй экземпляр завершён.');return 0
        except Exception as exc:
            import traceback
            where=', '.join(f'{Path(f.filename).name}:{f.lineno}' for f in traceback.extract_tb(exc.__traceback__))
            log.error('Бот остановлен ошибкой %s (%s).',type(exc).__name__,where);return 1
        if code==2 and sys.platform=='win32':
            subprocess.Popen(['powershell.exe','-NoProfile','-File',str(ROOT/'tools/notify_ready.ps1'),'-Problem',
                              '-Text','Токен Telegram неверен — бот остановлен. См. README, раздел «Telegram-бот».'],
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return code
    finally:
        log.removeHandler(handler);handler.close()


if __name__=='__main__':raise SystemExit(main())
