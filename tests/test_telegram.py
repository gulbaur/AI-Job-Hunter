"""Telegram bot: storage, API client, formatting, handlers, loop, events, setup. No network."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import telegram_store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=sqlite3.connect(Path(self.temp.name)/'jobs.sqlite');self.addCleanup(self.db.close)

    def test_enqueue_deduplicates_by_key(self):
        self.assertTrue(telegram_store.enqueue(self.db,'summary',{'a':1},'summary:run1'))
        self.assertFalse(telegram_store.enqueue(self.db,'summary',{'a':2},'summary:run1'))
        due=telegram_store.due_outbox(self.db)
        self.assertEqual(len(due),1);self.assertEqual(due[0]['payload'],{'a':1});self.assertEqual(due[0]['kind'],'summary')

    def test_failed_item_stays_due_and_counts_attempts(self):
        telegram_store.enqueue(self.db,'card',{'p':'x'},'card:1')
        item=telegram_store.due_outbox(self.db)[0]
        telegram_store.mark_failed(self.db,item['id'],'network')
        row=self.db.execute('SELECT attempts,last_error FROM telegram_outbox').fetchone()
        self.assertEqual(row,(1,'network'));self.assertEqual(len(telegram_store.due_outbox(self.db)),1)
        telegram_store.mark_sent(self.db,item['id'])
        self.assertEqual(telegram_store.due_outbox(self.db),[])

    def test_state_roundtrip_and_clear(self):
        self.assertIsNone(telegram_store.get_state(self.db,'edit'))
        self.assertEqual(telegram_store.get_state(self.db,'offset',0),0)
        telegram_store.set_state(self.db,'edit',{'key':'hh:1','expires_at':'2026-11-10T10:00:00+00:00'})
        self.assertEqual(telegram_store.get_state(self.db,'edit')['key'],'hh:1')
        telegram_store.clear_state(self.db,'edit');self.assertIsNone(telegram_store.get_state(self.db,'edit'))

    def test_actions_queue_order_and_finish(self):
        first=telegram_store.queue_action(self.db,'df',{'key':'hh:1'})
        telegram_store.queue_action(self.db,'sk',{'key':'hh:2'})
        self.assertEqual([a['action'] for a in telegram_store.due_actions(self.db)],['df','sk'])
        telegram_store.finish_action(self.db,first,'ok')
        self.assertEqual([a['data'] for a in telegram_store.due_actions(self.db)],[{'key':'hh:2'}])

    def test_card_mapping_overwrites(self):
        self.assertIsNone(telegram_store.card(self.db,'hh:1'))
        telegram_store.remember_card(self.db,'hh:1','42',10);telegram_store.remember_card(self.db,'hh:1','42',11)
        self.assertEqual(telegram_store.card(self.db,'hh:1'),('42',11))

    def test_configured_requires_token_and_chat(self):
        root=Path(self.temp.name)
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'','TELEGRAM_CHAT_ID':''}):
            (root/'.env').write_text('TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=\n',encoding='utf-8')
            self.assertFalse(telegram_store.configured(root))
            (root/'.env').write_text('TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=42\n',encoding='utf-8')
            self.assertTrue(telegram_store.configured(root))
            self.assertEqual(telegram_store.settings(root),('123:abc','42'))



def api_response(status=200,body=None):
    import requests
    r=requests.Response();r.status_code=status;r._content_consumed=True
    r._content=json.dumps(body if body is not None else {'ok':True,'result':{'message_id':1}}).encode()
    return r


class ApiTests(unittest.TestCase):
    TOKEN='123:ABC'

    def bot(self,*responses,error=None):
        from unittest.mock import Mock
        import telegram_api
        session=Mock()
        if error is not None:session.post.side_effect=error
        else:session.post.side_effect=list(responses)
        return telegram_api.Bot(self.TOKEN,session=session),session

    def test_scrub_removes_token_everywhere(self):
        import telegram_api
        text=telegram_api.scrub('https://api.telegram.org/bot123:ABC/sendMessage failed 123:ABC','123:ABC')
        self.assertNotIn('123:ABC',text)

    def test_network_error_message_never_contains_token(self):
        import requests,telegram_api
        bot,_=self.bot(error=requests.ConnectionError('https://api.telegram.org/bot123:ABC/getUpdates refused'))
        with self.assertRaises(telegram_api.TelegramUnavailable) as caught:bot.get_updates(None)
        self.assertNotIn('123:ABC',str(caught.exception))

    def test_error_codes_map_to_exceptions(self):
        import telegram_api
        cases=[(401,telegram_api.TelegramUnauthorized),(403,telegram_api.TelegramForbidden),
               (429,telegram_api.TelegramRetryAfter),(502,telegram_api.TelegramUnavailable)]
        for status,kind in cases:
            body={'ok':False,'error_code':status,'description':'x 123:ABC'}
            if status==429:body['parameters']={'retry_after':7}
            bot,_=self.bot(api_response(status,body))
            with self.subTest(status=status),self.assertRaises(kind) as caught:bot.send_message('42','hi')
            self.assertNotIn('123:ABC',str(caught.exception))
            if status==429:self.assertEqual(caught.exception.seconds,7)

    def test_send_message_uses_html_and_keyboard(self):
        bot,session=self.bot(api_response())
        message=bot.send_message('42','<b>x</b>',[[{'text':'A','callback_data':'df:hh:1'}]])
        payload=session.post.call_args.kwargs['json']
        self.assertEqual(payload['parse_mode'],'HTML');self.assertTrue(payload['disable_web_page_preview'])
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['callback_data'],'df:hh:1')
        self.assertEqual(message['message_id'],1)

    def test_edit_not_modified_is_ignored(self):
        bot,_=self.bot(api_response(400,{'ok':False,'error_code':400,'description':'Bad Request: message is not modified'}))
        bot.edit_message('42',5,'same')

    def test_get_updates_uses_long_poll_timeout(self):
        bot,session=self.bot(api_response(body={'ok':True,'result':[{'update_id':3}]}))
        self.assertEqual(bot.get_updates(3,timeout=50),[{'update_id':3}])
        self.assertEqual(session.post.call_args.kwargs['json']['offset'],3)
        self.assertEqual(session.post.call_args.kwargs['timeout'],60)



PACKET={'source':'hh','vacancy_id':'1','title':'ML Engineer','company':'Kolesa Group','decision':'stretch',
    'assessment_summary':'Подходит по PyTorch; закрывает J011, J012 задачи.','gaps':[{'reason':'Нет sklearn.'},{'reason':'Нет MLOps.'},{'reason':'Третий.'}],
    'local_cv_path':'C:/x/profile/CV_ML_ENGINEER_RU.docx','url':'https://hh.kz/vacancy/1'}


PARA=chr(10)*2


class FormatTests(unittest.TestCase):
    def test_summary_lists_each_source_status(self):
        import telegram_format
        text=telegram_format.summary({'started_at':'2026-11-10T05:00:00+00:00','prepared':2,'attention':1,
            'sources':[{'name':'HH','ok':True,'detail':''},{'name':'Веб-поиск','ok':False,'detail':'HTTP 404'}],'problem':''})
        self.assertIn('Подготовка 10:00 завершена',text);self.assertIn('Новых писем: 2',text)
        self.assertIn('требуют внимания: 1',text);self.assertIn('HH ✓',text);self.assertIn('веб-поиск: HTTP 404',text)

    def test_summary_shows_what_the_run_cost(self):
        # Audit A17: costs were visible nowhere but the console.
        import telegram_format
        base={'started_at':'2026-11-10T05:00:00+00:00','prepared':1,'attention':0,'sources':[],'problem':''}
        self.assertIn('Расход на ИИ: $0.027',telegram_format.summary({**base,'cost':0.0271}))
        self.assertNotIn('Расход',telegram_format.summary(base))

    def test_card_escapes_untrusted_text(self):
        import telegram_format
        text,_,_=telegram_format.card({**PACKET,'company':'<script>&'},'Письмо <b>',False,url=PACKET['url'])
        self.assertIn('&lt;script&gt;&amp;',text);self.assertNotIn('<script>',text);self.assertIn('Письмо &lt;b&gt;',text)

    def test_card_marks_user_edit(self):
        import telegram_format
        self.assertIn('✏️ изменено вами',telegram_format.card(PACKET,'x',True,url=PACKET['url'])[0])
        self.assertIn('✔ проверено по источникам',telegram_format.card(PACKET,'x',False,url=PACKET['url'])[0])

    def test_card_shows_publication_date_or_says_it_is_unknown(self):
        # Audit A33: a page without a date passed the period filter silently; the user should see it.
        import telegram_format
        self.assertIn('📅 опубликована 12.11.2026',telegram_format.card(PACKET,'x',False,url=PACKET['url'],published='2026-11-12T08:00:00+00:00')[0])
        self.assertIn('📅 дата публикации неизвестна',telegram_format.card(PACKET,'x',False,url=PACKET['url'],published='')[0])

    def test_why_has_no_hole_where_ids_were_removed(self):
        import telegram_format
        text=telegram_format._why('Опыт частично закрывает J004, J005. Однако классического ML нет.')
        self.assertIn('закрывает. Однако',text);self.assertNotIn(' .',text)

    def test_list_buttons_have_clean_titles_and_cut_with_ellipsis(self):
        import telegram_format
        _,keyboard=telegram_format.letter_list([('hh:1','AI Engineer  LLM   Engineer','G2i'),
            ('hh:2','Специалист по внедрению ИИ (AI), аналитика и базы данных и всего остального','ТОО Студия 40')],set())
        first,second=keyboard[0][0]['text'],keyboard[1][0]['text']
        self.assertEqual(first,'🟢 AI Engineer LLM Engineer · G2i')
        self.assertLessEqual(len(second),60);self.assertTrue(second.endswith('…'))

    def test_card_has_buttons_and_url(self):
        import telegram_format
        _,keyboard,_=telegram_format.card(PACKET,'x',False,url=PACKET['url'])
        buttons=[b for row in keyboard for b in row]
        codes={b['callback_data'].split(':')[0] for b in buttons if 'callback_data' in b}
        self.assertEqual(codes,{'hs','ed','ai','df','sk','ms','cv','ls'})
        self.assertEqual([b['url'] for b in buttons if 'url' in b],[PACKET['url']])

    def test_long_letter_goes_to_parts(self):
        import telegram_format
        letter=PARA.join(('Абзац %d. '%i)+'слово '*60 for i in range(14))
        self.assertGreater(len(letter),4096)
        text,_,parts=telegram_format.card(PACKET,letter,False,url=PACKET['url'])
        self.assertLessEqual(len(text),4096);self.assertTrue(parts)
        self.assertTrue(all(len(p)<=3500 for p in parts))
        self.assertEqual(PARA.join(parts),letter.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'))

    def test_split_text_hard_splits_without_paragraphs(self):
        import telegram_format
        parts=telegram_format.split_text('x'*8000,3500)
        self.assertEqual([len(p) for p in parts],[3500,3500,1000])

    def test_split_never_cuts_inside_html_entity(self):
        import telegram_format
        parts=telegram_format.split_text('x'*3498+'&amp;'+'y'*10,3500)
        self.assertTrue(parts[1].startswith('&amp;'));self.assertEqual(''.join(parts),'x'*3498+'&amp;'+'y'*10)

    def test_parse_callback_rejects_malformed(self):
        import telegram_format
        for bad in ['ed:hh:12a','ed:x:1','zz','ed:hh:'+'1'*60,'','ed:hh']:
            with self.subTest(bad=bad):self.assertIsNone(telegram_format.parse_callback(bad))
        self.assertEqual(telegram_format.parse_callback('ed:hh:123'),('ed','hh:123'))
        self.assertEqual(telegram_format.callback('ed','web:5'),'ed:web:5')

    def test_summary_strips_internal_ids(self):
        import telegram_format
        text,_,_=telegram_format.card(PACKET,'x',False,url=PACKET['url'])
        self.assertNotIn('J011',text);self.assertIn('Нет sklearn.',text);self.assertNotIn('Третий.',text)



class BotBase(unittest.TestCase):
    """Temp project with one HH letter (hh:1); FakeApi; owner chat 42."""
    def setUp(self):
        from datetime import datetime,timezone
        import draft_queue,search_settings,sources,telegram_bot
        from storage import JobStore
        from telegram_fake import FakeApi
        from test_hunt import PREF,vacancy
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);profile=self.root/'profile';profile.mkdir()
        (profile/'preferences.json').write_text(json.dumps(PREF),encoding='utf-8')
        search_settings.save(profile,{});sources.save(profile,{'enabled':['hh'],'boards':[],'career_pages':[]})
        self.cv=profile/'CV_ML_ENGINEER_RU.docx';self.cv.write_bytes(b'fixture')
        self.db=self.root/'data/jobs.sqlite';self.db.parent.mkdir()
        folder=self.root/'drafts/hh-1';folder.mkdir(parents=True)
        self.letter=folder/'letter.txt';self.letter.write_text('Здравствуйте! Письмо пользователя.',encoding='utf-8')
        self.packet={'source':'hh','vacancy_id':'1','title':'AI Engineer','company':'Example','decision':'stretch',
            'url':'https://hh.kz/vacancy/1','gaps':[],'letter_path':str(self.letter),'local_cv_path':str(self.cv),
            'output_dir':str(folder),'assessment_summary':'Подходит.'}
        self.packet_path=folder/'application.json';self.packet_path.write_text(json.dumps(self.packet),encoding='utf-8')
        with JobStore(self.db) as store:store.save(vacancy());draft_queue.register(store.connection,self.packet_path)
        self.api=FakeApi();self.now=datetime(2026,11,10,6,0,tzinfo=timezone.utc)
        self.app=telegram_bot.BotApp(self.root,self.api,'42',db_path=self.db,now=lambda:self.now)
        self.update_id=0

    def store(self):
        from storage import JobStore
        return JobStore(self.db)

    def message(self,text,chat=42,kind='private'):
        self.update_id+=1
        self.app.handle_update({'update_id':self.update_id,'message':{'message_id':self.update_id,
            'chat':{'id':chat,'type':kind},'text':text}})

    def press(self,data,chat=42,message_id=5,text='Карточка'):
        self.update_id+=1
        self.app.handle_update({'update_id':self.update_id,'callback_query':{'id':f'cb{self.update_id}','data':data,
            'message':{'message_id':message_id,'chat':{'id':chat,'type':'private'},'text':text}}})

    def keyboard_codes(self,params):
        return [b.get('callback_data') for row in (params.get('keyboard') or []) for b in row]


class BotTests(BotBase):
    def test_foreign_chat_is_ignored(self):
        self.message('/letters',chat=7);self.press('sk:hh:1',chat=7)
        self.assertEqual(self.api.sent,[])
        with self.store() as store:self.assertEqual(store.get('1')['status'],'new')

    def test_group_chat_of_owner_is_ignored(self):
        self.message('/letters',kind='group')
        self.assertEqual(self.api.sent,[])

    def test_defer_button_collapses_card(self):
        self.press('df:hh:1')
        with self.store() as store:
            self.assertTrue(store.connection.execute("SELECT reviewed_hash FROM draft_queue").fetchone()[0])
        edit=self.api.calls('editMessageText')[-1]
        self.assertIn('Отложено',edit['text']);self.assertIn('op:hh:1',self.keyboard_codes(edit))

    def test_skip_then_restore_buttons(self):
        self.press('sk:hh:1')
        with self.store() as store:self.assertEqual(store.get('1')['status'],'skipped')
        self.assertIn('rs:hh:1',self.keyboard_codes(self.api.calls('editMessageText')[-1]))
        self.press('rs:hh:1')
        with self.store() as store:self.assertEqual(store.get('1')['status'],'new')

    def test_restore_twice_is_harmless(self):
        self.press('sk:hh:1');self.press('rs:hh:1');self.press('rs:hh:1')
        self.assertIn('уже восстановлена',self.api.calls('answerCallbackQuery')[-1]['text'])
        with self.store() as store:self.assertEqual(store.get('1')['status'],'new')

    def test_manual_sent_needs_second_tap(self):
        import hh_response
        self.press('ms:hh:1')
        self.assertEqual(set(self.keyboard_codes(self.api.calls('editMessageText')[-1])),{'my:hh:1','mn:hh:1'})
        with self.store() as store:
            store.connection.executescript(hh_response.SCHEMA)
            self.assertIsNone(store.connection.execute('SELECT state FROM application_attempts').fetchone())
        self.press('my:hh:1')
        with self.store() as store:
            self.assertEqual(store.connection.execute('SELECT state FROM application_attempts').fetchone()[0],'reported_by_user')

    def test_cv_button_sends_only_profile_file(self):
        self.press('cv:hh:1')
        self.assertEqual(self.api.calls('sendDocument')[-1]['path'],str(self.cv))
        outside=Path(tempfile.mkdtemp());self.addCleanup(lambda:__import__('shutil').rmtree(outside,ignore_errors=True))
        (outside/'cv.docx').write_bytes(b'x')
        self.packet_path.write_text(json.dumps({**self.packet,'local_cv_path':str(outside/'cv.docx')}),encoding='utf-8')
        self.press('cv:hh:1')
        self.assertEqual(len(self.api.calls('sendDocument')),1)
        self.assertIn('CV',self.api.calls('answerCallbackQuery')[-1]['text'])

    def test_cv_button_sends_pdf_when_exported_and_card_names_it(self):
        pdf=Path(str(self.cv)+'.pdf');pdf.write_bytes(b'%PDF-1.7 fixture')
        self.press('cv:hh:1')
        self.assertEqual(self.api.calls('sendDocument')[-1]['path'],str(pdf.resolve()))
        self.press('op:hh:1')
        self.assertIn('CV_ML_ENGINEER_RU.docx.pdf',self.api.calls('editMessageText')[-1]['text'])

    def test_stale_card_removes_buttons(self):
        import actions
        with self.store() as store:actions.skip(self.root,store,'hh:1')
        self.press('df:hh:1')
        self.assertIn('уже обработан',self.api.calls('answerCallbackQuery')[-1]['text'])
        self.assertIsNone(self.api.calls('editMessageText')[-1]['keyboard'])

    def test_busy_program_queues_action(self):
        import run_lock,telegram_store
        with patch('run_lock.one_run',side_effect=run_lock.RunBusy('busy')):self.press('df:hh:1')
        import sqlite3
        db=sqlite3.connect(self.db);self.addCleanup(db.close)
        self.assertEqual([a['action'] for a in telegram_store.due_actions(db)],['df'])
        self.assertIn('Применю, как только освободится.',self.api.calls('answerCallbackQuery')[-1]['text'])

    def test_letters_command_lists_pending_with_open_buttons(self):
        self.message('/letters')
        sent=self.api.calls('sendMessage')[-1]
        self.assertIn('Писем в очереди: 1',sent['text']);self.assertIn('op:hh:1',self.keyboard_codes(sent))
        self.assertIn('AI Engineer',' '.join(b['text'] for row in sent['keyboard'] for b in row))

    def test_open_button_shows_card(self):
        self.press('op:hh:1')
        sent=self.api.calls('editMessageText')[-1]
        self.assertIn('Письмо пользователя',sent['text']);self.assertIn('ed:hh:1',self.keyboard_codes(sent))

    def test_attention_command_says_retry_is_on_site(self):
        import work_queue
        with self.store() as store:work_queue.stage(store,('hh','1'),'letter_failed','Причина X')
        self.message('/attention')
        text=self.api.calls('sendMessage')[-1]['text']
        self.assertIn('Причина X',text);self.assertIn('на сайте',text)

    def test_status_command_shows_last_run_next_window_and_problem(self):
        (self.root/'data/last_scheduled_run.json').write_text(json.dumps({'state':'started','started_at':'2026-11-10T05:00:00+00:00'}),encoding='utf-8')
        self.message('/status')
        text=self.api.calls('sendMessage')[-1]['text']
        self.assertIn('прервал',text);self.assertIn('18:00',text)

    def test_send_card_skips_processed_letter(self):
        import actions
        self.assertTrue(self.app.send_card(self.packet_path))
        with self.store() as store:actions.skip(self.root,store,'hh:1')
        self.assertFalse(self.app.send_card(self.packet_path))



class EditTests(BotBase):
    def state(self,name):
        import sqlite3
        db=sqlite3.connect(self.db)
        try:return telegram_store.get_state(db,name)
        finally:db.close()

    def test_edit_full_cycle(self):
        import actions
        self.press('ed:hh:1')
        self.assertIn('Письмо пользователя',self.api.calls('sendMessage')[-1]['text'])
        self.message('Мой новый текст')
        preview=self.api.calls('sendMessage')[-1]
        self.assertIn('Мой новый текст',preview['text'])
        self.assertEqual(set(self.keyboard_codes(preview)),{'sv:hh:1','cx:hh:1'})
        self.press('sv:hh:1',message_id=self.api.next_id)
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Мой новый текст')
        with self.store() as store:self.assertTrue(actions.is_user_edited(store,self.packet))
        self.assertTrue(any('✏️ изменено вами' in t for t in self.api.texts()))
        self.assertIsNone(self.state('edit'));self.assertIsNone(self.state('edit_draft'))

    def test_edit_cancel_keeps_file(self):
        self.press('ed:hh:1');self.message('Другой текст');self.press('cx:hh:1')
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Здравствуйте! Письмо пользователя.')
        self.assertIsNone(self.state('edit'));self.assertIsNone(self.state('edit_draft'))

    def test_edit_mode_expires_after_30_minutes(self):
        from datetime import timedelta
        self.press('ed:hh:1')
        self.now=self.now+timedelta(minutes=31)
        self.message('Поздний текст')
        self.assertIsNone(self.state('edit_draft'))
        self.assertIn('истёк',self.api.calls('sendMessage')[-1]['text'])

    def test_edit_with_html_characters_is_saved_verbatim_and_escaped(self):
        self.press('ed:hh:1');self.message('C++ & ML <3')
        preview=self.api.calls('sendMessage')[-1]['text']
        self.assertIn('C++ &amp; ML &lt;3',preview);self.assertNotIn('<3',preview)
        self.press('sv:hh:1',message_id=self.api.next_id)
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'C++ & ML <3')

    def test_edit_rejects_empty_or_oversized_text_and_stays_in_edit_mode(self):
        self.press('ed:hh:1');self.message('x'*12001)
        self.assertIn('12 000',self.api.calls('sendMessage')[-1]['text'])
        self.assertIsNotNone(self.state('edit'));self.assertIsNone(self.state('edit_draft'))
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Здравствуйте! Письмо пользователя.')

    def test_plain_text_outside_edit_mode_gets_hint(self):
        self.message('привет')
        self.assertIn('Изменить',self.api.calls('sendMessage')[-1]['text'])
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Здравствуйте! Письмо пользователя.')

    def test_save_without_draft_is_refused(self):
        self.press('sv:hh:1',message_id=self.api.next_id)
        self.assertIn('Нет нового текста',self.api.calls('answerCallbackQuery')[-1]['text'])



class LoopTests(BotBase):
    def conn(self):
        import sqlite3
        db=sqlite3.connect(self.db);self.addCleanup(db.close);return db

    def test_outbox_summary_and_card_sent_once(self):
        db=self.conn()
        telegram_store.enqueue(db,'summary',{'started_at':'2026-11-10T05:00:00+00:00','prepared':1,'attention':0,'sources':[],'problem':''},'summary:r1')
        telegram_store.enqueue(db,'card',{'packet_path':str(self.packet_path)},'card:hh:1:x')
        self.assertEqual(self.app.send_outbox(),2)
        self.assertEqual(self.app.send_outbox(),0)
        texts=[c['text'] for c in self.api.calls('sendMessage')]
        self.assertTrue(any('Подготовка 10:00' in t for t in texts));self.assertTrue(any('Письмо пользователя' in t for t in texts))

    def test_heartbeat_is_written_before_first_long_poll(self):
        # Live check 10.11.2026: the site showed «бот не работает» for up to 50 s after every start.
        import telegram_bot
        beat=self.root/'data/telegram_bot_heartbeat.json'
        seen=[]
        def first_poll(offset,timeout=50):
            seen.append(beat.exists());return []
        self.app.api.get_updates=first_poll
        telegram_bot.run_forever(self.app,sleep=lambda s:None,clock=lambda:0,stop=lambda:len(seen)>=1)
        self.assertEqual(seen,[True])

    def test_outbox_retries_after_network_error(self):
        import telegram_api
        db=self.conn();telegram_store.enqueue(db,'problem',{'message':'Сбой'},'problem:1')
        self.api.fail_next=telegram_api.TelegramUnavailable('x')
        self.assertEqual(self.app.send_outbox(),0)
        # Network trouble does not move the notice toward being given up (senior review 13.11, finding 9).
        self.assertEqual(db.execute('SELECT attempts FROM telegram_outbox').fetchone()[0],0)
        self.assertEqual(self.app.send_outbox(),1)

    def test_outbox_card_for_processed_letter_is_dropped(self):
        import actions
        with self.store() as store:actions.skip(self.root,store,'hh:1')
        db=self.conn();telegram_store.enqueue(db,'card',{'packet_path':str(self.packet_path)},'card:hh:1:x')
        self.app.send_outbox()
        self.assertEqual(self.api.calls('sendMessage'),[])
        self.assertIsNotNone(db.execute('SELECT sent_at FROM telegram_outbox').fetchone()[0])

    def test_queued_action_runs_after_lock_released(self):
        db=self.conn();telegram_store.queue_action(db,'df',{'key':'hh:1','chat_id':'42','message_id':5})
        self.assertEqual(self.app.run_actions(),1)
        with self.store() as store:self.assertTrue(store.connection.execute('SELECT reviewed_hash FROM draft_queue').fetchone()[0])
        self.assertTrue(any('Отложено' in t for t in self.api.texts()))
        self.assertEqual(telegram_store.due_actions(db),[])

    def run_loop(self,app,side_effects,iterations):
        import telegram_bot
        from unittest.mock import Mock
        app.api.get_updates=Mock(side_effect=side_effects)
        sleeps=[];count=[0]
        def stop():
            count[0]+=1;return count[0]>iterations
        code=telegram_bot.run_forever(app,sleep=sleeps.append,clock=lambda:0,stop=stop)
        return code,sleeps,app.api.get_updates

    def test_loop_backoff_sequence(self):
        import telegram_api
        down=telegram_api.TelegramUnavailable('down')
        code,sleeps,_=self.run_loop(self.app,[down,down,down,[],down],5)
        self.assertEqual(sleeps,[5,10,20,5])

    def test_loop_honours_retry_after(self):
        import telegram_api
        _,sleeps,_=self.run_loop(self.app,[telegram_api.TelegramRetryAfter('slow',7),[]],2)
        self.assertEqual(sleeps,[7])

    def test_unauthorized_stops_loop_with_code_2(self):
        import telegram_api
        code,_,_=self.run_loop(self.app,[telegram_api.TelegramUnauthorized('bad')],3)
        self.assertEqual(code,2)
        beat=json.loads((self.root/'data/telegram_bot_heartbeat.json').read_text(encoding='utf-8'))
        self.assertEqual(beat['last_error'],'unauthorized')

    def test_forbidden_keeps_item_and_marks_heartbeat(self):
        import telegram_api
        db=self.conn();telegram_store.enqueue(db,'problem',{'message':'x'},'problem:2')
        self.api.fail_next=telegram_api.TelegramForbidden('blocked')
        self.app.send_outbox()
        self.assertEqual(len(telegram_store.due_outbox(db)),1)
        beat=json.loads((self.root/'data/telegram_bot_heartbeat.json').read_text(encoding='utf-8'))
        self.assertEqual(beat['last_error'],'forbidden')

    def test_offset_persisted_between_restarts(self):
        import telegram_bot
        update={'update_id':10,'message':{'message_id':1,'chat':{'id':42,'type':'private'},'text':'/help'}}
        self.run_loop(self.app,[[update]],1)
        second=telegram_bot.BotApp(self.root,self.api,'42',db_path=self.db)
        _,_,calls=self.run_loop(second,[[]],1)
        self.assertEqual(calls.call_args.args[0],11)

    def test_handler_crash_does_not_stop_loop(self):
        update={'update_id':3,'message':{'message_id':1,'chat':{'id':42,'type':'private'},'text':'/letters'}}
        with patch.object(self.app,'cmd_letters',side_effect=RuntimeError('boom')):
            code,_,calls=self.run_loop(self.app,[[update],[]],2)
        self.assertEqual(calls.call_count,2)

    def test_log_never_contains_letter_or_token(self):
        import logging,telegram_api,telegram_bot
        handler=telegram_bot.setup_logging(self.root)
        self.addCleanup(lambda:(logging.getLogger('telegram_bot').removeHandler(handler),handler.close()))
        self.press('ed:hh:1');self.message('Секретный новый текст');self.press('sv:hh:1',message_id=self.api.next_id)
        self.run_loop(self.app,[telegram_api.TelegramUnavailable('Telegram недоступен: bot<скрыто>'),[]],2)
        handler.flush()
        log=(self.root/'data/telegram_logs/bot.log').read_text(encoding='utf-8')
        self.assertIn('недоступен',log)
        self.assertNotIn('Секретный новый текст',log);self.assertNotIn('Письмо пользователя',log)

    def test_main_exits_cleanly_when_not_configured(self):
        import telegram_bot
        with patch.object(telegram_bot,'ROOT',self.root),patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'','TELEGRAM_CHAT_ID':''}), \
             patch('telegram_api.Bot') as bot:
            self.assertEqual(telegram_bot.main(),0)
        bot.assert_not_called()



REPORT={'run_id':'20261110T050000Z','started_at':'2026-11-10T05:00:00+00:00','state':'needs_attention','model_usage':{'known_cost_usd':0.031},
    'packet_paths':['C:/x/drafts/hh-1/personal-a/application.json','C:/x/drafts/web-2/personal-b/application.json'],
    'work_issues':[{'vacancy_id':'3'}],'coverage_warnings':['Веб-поиск не работает: выполнено 0 из 6 запросов. HTTP 404.'],
    'coverage':{'sources':[{'source':'HH','status':'checked'},{'source':'Cohere','slug':'cohere','status':'checked'},
        {'source':'Canonical','slug':'canonical','status':'error'},{'source':'Веб-поиск','status':'failed'}]}}


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'jobs.sqlite'

    def configured_root(self):
        root=Path(self.temp.name)/'cfg';root.mkdir(exist_ok=True)
        (root/'.env').write_text('TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_CHAT_ID=42\n',encoding='utf-8');return root

    def rows(self):
        db=sqlite3.connect(self.db)
        try:return [(r[0],json.loads(r[1]),r[2]) for r in db.execute('SELECT kind,payload_json,dedupe_key FROM telegram_outbox ORDER BY id')]
        finally:db.close()

    def test_run_finished_enqueues_one_digest_once(self):
        import telegram_events
        telegram_events.run_finished(self.db,REPORT,root=self.configured_root());telegram_events.run_finished(self.db,REPORT,root=self.configured_root())
        rows=self.rows()
        self.assertEqual([r[0] for r in rows],['digest'])
        summary=rows[0][1]
        self.assertEqual((summary['prepared'],summary['attention']),(2,1))
        sources={s['name']:s for s in summary['sources']}
        self.assertTrue(sources['HH']['ok'])
        self.assertFalse(sources['Сайты компаний']['ok']);self.assertIn('Canonical',sources['Сайты компаний']['detail'])
        self.assertFalse(sources['Веб-поиск']['ok']);self.assertIn('HTTP 404',sources['Веб-поиск']['detail'])
        self.assertEqual(rows[0][2],'digest:'+REPORT['run_id'])
        self.assertEqual(summary['cost'],0.031)

    def test_run_finished_attaches_unread_links_once(self):
        # User request 17.11.2026: links the program could not read are sent to the bot instead of being lost.
        import telegram_events,discovery
        from datetime import datetime,timezone,timedelta
        now=datetime.now(timezone.utc);old=(now-timedelta(days=20)).isoformat();now=now.isoformat()
        db=sqlite3.connect(self.db);db.executescript(discovery.LINK_SCHEMA)
        db.executemany('INSERT INTO discovered_links VALUES (?,?,?,?,?)',[
            ('https://kz.linkedin.com/jobs/view/ai-engineer-at-armeta-kz-4434505492','web_search','manual_link','LinkedIn',now),
            ('https://www.linkedin.com/jobs/machine-learning-jobs-worldwide','web_search','manual_link','LinkedIn',now),
            ('https://rockethunt.ai/en/vacancies/86a9','web_search','needs_adapter','Страница без текста (нужен браузер); проверьте вручную.',now),
            ('https://hirify.me/jobs/1-ml','web_search','needs_adapter','ИИ не прочитал страницу в этом запуске; повтор позже.',now),
            ('https://jobgether.com/offer/6ac5','web_search','error','Сайт вернул HTTP 403.',now),
            ('https://kz.jobberbuzz.com/ru/site/view?id=1','web_search','error','Сайт вернул HTTP 404.',now),
            ('https://kz.linkedin.com/jobs/view/old-1','web_search','manual_link','LinkedIn',old)]);db.commit();db.close()
        telegram_events.run_finished(self.db,REPORT,root=self.configured_root())
        telegram_events.run_finished(self.db,{**REPORT,'run_id':'next'},root=self.configured_root())
        first,second=[r[1] for r in self.rows()]
        self.assertEqual(sorted(l['url'] for l in first['links']),['https://jobgether.com/offer/6ac5',
            'https://kz.linkedin.com/jobs/view/ai-engineer-at-armeta-kz-4434505492','https://rockethunt.ai/en/vacancies/86a9'])
        self.assertIn('ai engineer at armeta kz',' '.join(l['label'] for l in first['links']))
        self.assertEqual(second['links'],[])

    def test_summary_shows_unread_links_as_clickable(self):
        import telegram_format
        text=telegram_format.summary({'started_at':'2026-11-10T05:00:00+00:00','links':[{'url':'https://a.example/x?a=1&b=2','label':'a.example: <x>'}]})
        self.assertIn('<a href="https://a.example/x?a=1&amp;b=2">a.example: &lt;x&gt;</a>',text);self.assertIn('откройте сами',text)

    def test_events_never_break_run(self):
        import telegram_events
        telegram_events.run_finished(Path(self.temp.name)/'missing-dir'/'x'/'jobs.sqlite',REPORT,root=self.configured_root())
        telegram_events.run_finished(self.db,{'run_id':'r','packet_paths':['not-a-packet']},root=self.configured_root())

    def test_nothing_queued_while_telegram_not_configured(self):
        import telegram_events
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'','TELEGRAM_CHAT_ID':''}):
            telegram_events.run_finished(self.db,REPORT,root=Path(self.temp.name))
        self.assertFalse(self.db.exists() and self.rows())



class SetupTests(unittest.TestCase):
    def setUp(self):
        import importlib.util,sys
        root=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('telegram_setup',root/'tools/telegram_setup.py')
        self.setup=importlib.util.module_from_spec(spec);sys.modules['telegram_setup']=self.setup;spec.loader.exec_module(self.setup)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)

    def update(self,uid,text,chat=42,kind='private'):
        return {'update_id':uid,'message':{'message_id':uid,'chat':{'id':chat,'type':kind},'text':text}}

    def test_pair_accepts_only_matching_code_from_private_chat(self):
        from telegram_fake import FakeApi
        api=FakeApi()
        api.updates=[self.update(1,'/start 000000'),self.update(2,'/start 123456',chat=-5,kind='group'),self.update(3,'/start 123456')]
        self.assertEqual(self.setup.pair(api,'123456',deadline_s=60,sleep=lambda s:None,clock=lambda:0),'42')

    def test_pair_times_out(self):
        from common import CheckError
        from telegram_fake import FakeApi
        api=FakeApi();ticks=iter(range(0,10000,100))
        with self.assertRaises(CheckError):self.setup.pair(api,'123456',deadline_s=600,sleep=lambda s:None,clock=lambda:next(ticks))

    def test_write_env_preserves_other_lines(self):
        env=Path(self.temp.name)/'.env'
        env.write_bytes(b'OPENROUTER_API_KEY=x\r\n# note\r\nTELEGRAM_CHAT_ID=\r\nHH_USER_AGENT=a\r\n')
        self.setup.write_env(env,'TELEGRAM_CHAT_ID','42')
        self.assertEqual(env.read_bytes(),b'OPENROUTER_API_KEY=x\r\n# note\r\nTELEGRAM_CHAT_ID=42\r\nHH_USER_AGENT=a\r\n')
        env.write_bytes(b'OPENROUTER_API_KEY=x\n')
        self.setup.write_env(env,'TELEGRAM_CHAT_ID','42')
        self.assertEqual(env.read_bytes(),b'OPENROUTER_API_KEY=x\nTELEGRAM_CHAT_ID=42\n')



class MenuTests(BotBase):
    """User request 10.11.2026: a menu and a way back from any card."""
    def test_start_and_help_attach_persistent_menu(self):
        import telegram_bot
        for command in ('/start','/help'):
            self.message(command)
            self.assertEqual(self.api.calls('sendMessage')[-1]['menu'],telegram_bot.MENU)

    def test_menu_buttons_work_as_commands(self):
        self.message('📬 Письма')
        self.assertIn('Писем в очереди',self.api.calls('sendMessage')[-1]['text'])
        self.message('⚠️ Внимание');self.assertIn('внимания',self.api.calls('sendMessage')[-1]['text'])
        self.message('ℹ️ Состояние');self.assertIn('Состояние',self.api.calls('sendMessage')[-1]['text'])
        self.message('❓ Помощь');self.assertIn('Команды',self.api.calls('sendMessage')[-1]['text'])

    def test_menu_button_during_edit_is_a_command_not_the_new_letter(self):
        self.press('ed:hh:1');self.message('📬 Письма')
        self.assertIn('Писем в очереди',self.api.calls('sendMessage')[-1]['text'])
        self.assertIsNone(self.app.state('edit_draft'))
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Здравствуйте! Письмо пользователя.')

    def test_card_and_action_results_lead_back_to_the_list(self):
        self.press('op:hh:1')
        card=self.api.calls('editMessageText')[-1]
        self.assertIn('ls:hh:1',self.keyboard_codes(card))
        self.press('ls:hh:1')
        self.assertIn('Писем в очереди',self.api.calls('editMessageText')[-1]['text'])
        self.press('df:hh:1')  # the result is the list itself
        result=self.api.calls('editMessageText')[-1]
        self.assertIn('Отложено',result['text']);self.assertIn('op:hh:1',self.keyboard_codes(result))

    def test_startup_registers_commands_and_shows_menu_once(self):
        import telegram_bot
        self.app.api.get_updates=lambda offset,timeout=50:[]
        for _ in range(2):
            telegram_bot.run_forever(self.app,sleep=lambda s:None,clock=lambda:0,stop=iter([False,True]).__next__)
        self.assertEqual(len(self.api.calls('setMyCommands')),2)  # cheap and idempotent: every start
        menus=[c for c in self.api.calls('sendMessage') if c.get('menu')]
        self.assertEqual(len(menus),1)


class AiRewriteTests(BotBase):
    """User request 10.11.2026: «🤖 Переписать с ИИ» by her own instruction; paid, checked, saved only on «Сохранить»."""
    def setUp(self):
        super().setUp()
        self.calls=[]
        def rewriter(root,store,key,instruction,base_text=None):
            self.calls.append((key,instruction,base_text))
            if instruction=='сломай':
                from common import CheckError
                raise CheckError('Проверка фактов письма не пройдена: «10 лет опыта» — нет в CV.')
            return {'letter':f'Здравствуйте! Новый вариант {len(self.calls)}.','cost':0.021}
        self.app.rewriter=rewriter

    def test_card_offers_ai_rewrite(self):
        self.press('op:hh:1')
        self.assertIn('ai:hh:1',self.keyboard_codes(self.api.calls('editMessageText')[-1]))

    def test_instruction_gives_checked_proposal_saved_only_on_save(self):
        self.press('ai:hh:1')
        self.assertIn('Напишите',self.api.calls('sendMessage')[-1]['text'])
        self.message('Добавь, что хочу в сильную команду')
        self.assertEqual(self.calls,[('hh:1','Добавь, что хочу в сильную команду',None)])
        proposal=self.api.calls('sendMessage')[-1]
        self.assertIn('Новый вариант 1',proposal['text']);self.assertIn('sv:hh:1',self.keyboard_codes(proposal))
        self.assertIn('0.02',proposal['text'])  # the user sees what it cost
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Здравствуйте! Письмо пользователя.')
        self.press('sv:hh:1',message_id=self.api.next_id)
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Здравствуйте! Новый вариант 1.')

    def test_next_instruction_refines_the_last_proposal(self):
        self.press('ai:hh:1');self.message('короче');self.message('ещё теплее')
        self.assertEqual(self.calls[1],('hh:1','ещё теплее','Здравствуйте! Новый вариант 1.'))

    def test_rejected_rewrite_keeps_letter_and_waits_for_another_instruction(self):
        self.press('ai:hh:1');self.message('сломай')
        self.assertIn('10 лет опыта',self.api.calls('sendMessage')[-1]['text'])
        self.message('короче')
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Здравствуйте! Письмо пользователя.')

    def test_cancel_and_menu_leave_ai_mode_without_paid_call(self):
        self.press('ai:hh:1');self.message('📬 Письма')
        self.assertEqual(self.calls,[])
        self.press('cx:hh:1');self.message('обычный текст')
        self.assertEqual(self.calls,[])


class ReviewFixTests(BotBase):
    """Final-review findings 2, 3, 5, 8, 10 (1 and 4 are in test_actions / EventTests)."""
    def conn(self):
        db=sqlite3.connect(self.db);self.addCleanup(db.close);return db

    def test_permanent_400_does_not_block_later_notifications(self):
        import telegram_api
        db=self.conn()
        telegram_store.enqueue(db,'problem',{'message':'первое'},'problem:a')
        telegram_store.enqueue(db,'problem',{'message':'второе'},'problem:b')
        # Round 1: the first item gets a permanent 400; the second must still be delivered in the same round.
        self.api.fail_methods['sendMessage']=[telegram_api.TelegramError('HTTP 400 — chat not found')]
        self.app.send_outbox()
        self.assertTrue(any('второе' in c['text'] for c in self.api.calls('sendMessage')))
        self.assertEqual(db.execute("SELECT attempts FROM telegram_outbox WHERE dedupe_key='problem:a'").fetchone()[0],1)
        # The broken item is given up after 3 attempts instead of being retried forever.
        for _ in range(2):
            self.api.fail_methods['sendMessage']=[telegram_api.TelegramError('HTTP 400')];self.app.send_outbox()
        self.assertEqual(telegram_store.due_outbox(db),[])

    def test_failed_letter_part_does_not_resend_card(self):
        import telegram_api
        long=PARA.join(('Абзац %d. '%i)+'слово '*60 for i in range(14))
        self.letter.write_text(long,encoding='utf-8')
        db=self.conn();telegram_store.enqueue(db,'card',{'packet_path':str(self.packet_path)},'card:hh:1:r')
        self.api.fail_methods['sendMessage']=[]
        original=self.api.send_message;count=[0]
        def flaky(chat_id,text,keyboard=None):
            count[0]+=1
            if count[0]==2:raise telegram_api.TelegramUnavailable('down')
            return original(chat_id,text,keyboard)
        self.api.send_message=flaky
        self.app.send_outbox();self.app.send_outbox()
        cards=[c for c in self.api.calls('sendMessage') if c['keyboard']]
        self.assertEqual(len(cards),1)

    def test_deferred_action_with_deleted_card_is_reported_done(self):
        import telegram_api
        db=self.conn();telegram_store.queue_action(db,'sk',{'key':'hh:1','chat_id':'42','message_id':5})
        self.api.fail_methods['editMessageText']=[telegram_api.TelegramError('HTTP 400 — message to edit not found')]
        self.app.run_actions()
        with self.store() as store:self.assertEqual(store.get('1')['status'],'skipped')
        texts=' '.join(c['text'] for c in self.api.calls('sendMessage'))
        self.assertIn('Исключено',texts);self.assertNotIn('не выполнено',texts.lower())

    def test_live_action_with_deleted_card_answers_success(self):
        import telegram_api
        self.api.fail_methods['editMessageText']=[telegram_api.TelegramError('HTTP 400 — message to edit not found')]
        self.press('sk:hh:1')
        self.assertNotIn('HTTP 400',self.api.calls('answerCallbackQuery')[-1]['text'])
        self.assertTrue(any('Исключено' in c['text'] for c in self.api.calls('sendMessage')))

    def test_save_with_deleted_card_sends_new_card(self):
        import telegram_api
        db=self.conn();telegram_store.remember_card(db,'hh:1','42',77)
        self.press('ed:hh:1');self.message('Новый текст')
        self.api.fail_methods['editMessageText']=[telegram_api.TelegramError('ok'),telegram_api.TelegramError('HTTP 400 — message to edit not found')]
        self.press('sv:hh:1',message_id=self.api.next_id)
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Новый текст')
        self.assertEqual(self.api.calls('answerCallbackQuery')[-1]['text'],'Письмо сохранено.')
        self.assertTrue(any('Новый текст' in c['text'] and c['keyboard'] for c in self.api.calls('sendMessage')))

    def test_forbidden_mark_survives_regular_heartbeat_until_success(self):
        import telegram_api
        db=self.conn();telegram_store.enqueue(db,'problem',{'message':'x'},'problem:f')
        self.api.fail_next=telegram_api.TelegramForbidden('blocked')
        self.app.send_outbox();self.app.heartbeat()
        beat=lambda:json.loads((self.root/'data/telegram_bot_heartbeat.json').read_text(encoding='utf-8'))['last_error']
        self.assertEqual(beat(),'forbidden')
        self.app.send_outbox();self.app.heartbeat()
        self.assertEqual(beat(),'')

    def test_unexpected_error_in_loop_body_does_not_kill_bot(self):
        import telegram_bot
        from unittest.mock import Mock
        update={'update_id':4,'message':{'message_id':1,'chat':{'id':42,'type':'private'},'text':'/help'}}
        self.app.api.get_updates=Mock(side_effect=[[update],[]])
        count=[0]
        def stop():
            count[0]+=1;return count[0]>2
        with patch('telegram_store.set_state',side_effect=[sqlite3.OperationalError('database is locked'),None,None]):
            code=telegram_bot.run_forever(self.app,sleep=lambda s:None,clock=lambda:0,stop=stop)
        self.assertEqual(code,0);self.assertEqual(self.app.api.get_updates.call_count,2)

    def test_malformed_token_404_is_unauthorized(self):
        import telegram_api
        from unittest.mock import Mock
        session=Mock();session.post.return_value=api_response(404,{'ok':False,'error_code':404,'description':'Not Found'})
        with self.assertRaises(telegram_api.TelegramUnauthorized):telegram_api.Bot('bad token',session=session).get_me()




class SingleMessageTests(BotBase):
    """User request 13.11.2026: one message per run with a button per vacancy; tapping opens the letter in place."""
    def conn(self):
        db=sqlite3.connect(self.db);self.addCleanup(db.close);return db

    def digest(self):
        db=self.conn()
        telegram_store.enqueue(db,'digest',{'started_at':'2026-11-10T05:00:00+00:00','prepared':1,'attention':0,
                                            'sources':[{'name':'HH','ok':True,'detail':''}],'problem':''},'digest:r1')

    def test_run_finished_enqueues_one_digest_instead_of_summary_and_cards(self):
        import telegram_events
        (self.root/'.env').write_text('TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_CHAT_ID=42\n',encoding='utf-8')
        telegram_events.run_finished(self.db,REPORT,root=self.root);telegram_events.run_finished(self.db,REPORT,root=self.root)
        kinds=[r[0] for r in self.conn().execute('SELECT kind FROM telegram_outbox')]
        self.assertEqual(kinds,['digest'])

    def test_digest_is_one_message_with_a_button_per_vacancy(self):
        self.digest()
        self.assertEqual(self.app.send_outbox(),1)
        sent=self.api.calls('sendMessage')
        self.assertEqual(len(sent),1)
        self.assertIn('Подготовка 10:00',sent[0]['text']);self.assertIn('op:hh:1',self.keyboard_codes(sent[0]))
        self.assertNotIn('Письмо пользователя',sent[0]['text'])
        self.assertIn('AI Engineer',' '.join(b['text'] for row in sent[0]['keyboard'] for b in row))

    def test_recheck_digest_uses_its_own_heading(self):
        # User request 17.11.2026: after a recheck of saved vacancies, send the fresh list with the recheck summary.
        telegram_store.enqueue(self.conn(),'digest',{'head':'🔄 Перепроверка <вакансий>\nЗакрыто: 2'},'digest:recheck')
        self.assertEqual(self.app.send_outbox(),1)
        sent=self.api.calls('sendMessage')[0]
        self.assertIn('🔄 Перепроверка &lt;вакансий&gt;\nЗакрыто: 2',sent['text']);self.assertNotIn('Подготовка',sent['text'])
        self.assertIn('op:hh:1',self.keyboard_codes(sent))

    def test_open_turns_the_same_message_into_the_card(self):
        self.press('op:hh:1',message_id=9)
        self.assertEqual(self.api.calls('sendMessage'),[])
        edit=self.api.calls('editMessageText')[-1]
        self.assertEqual(edit['message_id'],9);self.assertIn('Письмо пользователя',edit['text'])
        self.assertIn('ed:hh:1',self.keyboard_codes(edit))

    def test_back_turns_the_same_message_into_the_list(self):
        self.press('op:hh:1',message_id=9);self.press('ls:hh:1',message_id=9)
        self.assertEqual(self.api.calls('sendMessage'),[])
        edit=self.api.calls('editMessageText')[-1]
        self.assertEqual(edit['message_id'],9);self.assertIn('op:hh:1',self.keyboard_codes(edit))

    def test_skip_returns_to_the_list_with_undo(self):
        self.press('sk:hh:1',message_id=9)
        edit=self.api.calls('editMessageText')[-1]
        self.assertIn('Исключено',edit['text']);self.assertIn('rs:hh:1',self.keyboard_codes(edit))
        self.assertNotIn('op:hh:1',self.keyboard_codes(edit))

    def test_deferred_letter_stays_reachable_and_is_marked(self):
        self.press('df:hh:1',message_id=9)
        edit=self.api.calls('editMessageText')[-1]
        self.assertIn('Отложено',edit['text'])
        buttons=[b['text'] for row in edit['keyboard'] for b in row if b.get('callback_data')=='op:hh:1']
        self.assertTrue(buttons and buttons[0].startswith('⏸'))

    def test_saved_edit_updates_the_card_opened_in_place(self):
        self.press('op:hh:1',message_id=9);self.press('ed:hh:1',message_id=9);self.message('Новый текст')
        self.press('sv:hh:1',message_id=self.api.next_id)
        self.assertTrue(any(e['message_id']==9 and 'Новый текст' in e['text'] for e in self.api.calls('editMessageText')))


class HhSendTests(BotBase):
    """User request 13.11.2026: send the HH response from the bot after a screenshot and a one-time 4-digit code."""
    def setUp(self):
        super().setUp()
        self.image=self.root/'form.png';self.image.write_bytes(b'\x89PNG fixture')
        self.runs=[]
        def sender(packet,db,*,allow_send,profile_dir,confirmation_channel,result_channel):
            self.runs.append(packet['vacancy_id'])
            answer=confirmation_channel({'hh_resume_title':'AI/ML Engineer','form_image':str(self.image)},'ОТПРАВИТЬ 1')
            return 'confirmed_by_site' if answer=='ОТПРАВИТЬ 1' else 'cancelled_before_send'
        self.app.sender=sender

    def wait(self,condition):
        import time
        for _ in range(250):
            if condition():return
            time.sleep(.02)
        self.fail('timeout')

    def code(self):
        self.wait(lambda:self.api.calls('sendPhoto'))
        import re
        return re.search(r'<b>(\d{4})</b>',self.api.calls('sendPhoto')[-1]['caption'])[1]

    def finish(self):
        self.app.worker.join(5);self.assertFalse(self.app.worker.is_alive())
        return self.api.calls('sendMessage')[-1]['text']

    def test_card_offers_hh_send_only_for_hh_vacancies(self):
        import telegram_format
        _,keyboard,_=telegram_format.card(self.packet,'Письмо',False,url='https://hh.kz/vacancy/1')
        self.assertIn('hs:hh:1',[b.get('callback_data') for row in keyboard for b in row])
        _,keyboard,_=telegram_format.card({**self.packet,'source':'web'},'Письмо',False,url='https://x.example/1')
        self.assertNotIn('hs:web:1',[b.get('callback_data') for row in keyboard for b in row])

    def test_full_cycle_screenshot_then_code_then_confirmed(self):
        self.press('hs:hh:1')
        code=self.code()
        photo=self.api.calls('sendPhoto')[-1]
        self.assertEqual(photo['path'],str(self.image));self.assertIn('AI/ML Engineer',photo['caption'])
        wrong='0000' if code!='0000' else '1111'
        self.message(wrong);self.assertIn('Неверный код',self.api.calls('sendMessage')[-1]['text'])
        self.message(code)
        self.assertIn('HH подтвердил',self.finish())
        self.assertEqual(self.runs,['1']);self.assertIsNone(self.app.sending)

    def test_cancel_button_stops_before_send(self):
        self.press('hs:hh:1');self.code();self.press('hx:hh:1')
        self.assertIn('отменена',self.finish())

    def test_three_wrong_codes_cancel(self):
        self.press('hs:hh:1');code=self.code()
        wrong='0000' if code!='0000' else '1111'
        for _ in range(3):self.message(wrong)
        self.assertIn('отменена',self.finish())

    def test_code_expires(self):
        import telegram_bot
        with patch.object(telegram_bot,'CODE_SECONDS',0.05):
            self.press('hs:hh:1')
            self.assertIn('отменена',self.finish())
        self.assertTrue(any('истекло' in t for t in self.api.texts()))

    def test_refused_while_preparation_runs_or_another_send_is_open(self):
        with patch('run_lock.is_busy',return_value=True):self.press('hs:hh:1')
        self.assertIn('подготовка',self.api.calls('answerCallbackQuery')[-1]['text'])
        self.assertEqual(self.runs,[])
        self.press('hs:hh:1');self.code();self.press('hs:hh:1')
        self.assertIn('Уже идёт отправка',self.api.calls('answerCallbackQuery')[-1]['text'])
        self.press('hx:hh:1');self.finish()
        self.assertEqual(self.runs,['1'])

    def test_sender_error_is_reported_and_state_cleared(self):
        from common import CheckError
        def broken(*args,**kwargs):raise CheckError('HH требует вход.')
        self.app.sender=broken
        self.press('hs:hh:1')
        self.assertIn('HH требует вход',self.finish());self.assertIsNone(self.app.sending)

    def test_login_problem_tells_how_to_log_in(self):
        from browser_check import BrowserStop
        def logged_out(*args,**kwargs):raise BrowserStop('HH перенаправил на вход: ручная авторизация не завершена или сессия истекла.')
        self.app.sender=logged_out
        self.press('hs:hh:1')
        self.assertIn('hh_login.cmd',self.finish())


class HhSendReviewTests(HhSendTests):
    """Final review 13.11.2026 of the HH send from the bot: the decision on the code is final."""
    def armed(self,key='hh:1'):
        import threading
        self.app.sending={'key':key,'label':'AI Engineer · Example','event':threading.Event(),'answer':'','code':'4321',
                          'attempts':0,'stage':'code','decided':False}
        return self.app.sending

    def test_code_after_cancel_does_not_approve(self):
        sending=self.armed()
        self.app.hh_control('hx','hh:1');self.app.on_text('4321')
        self.assertEqual(sending['answer'],'');self.assertTrue(sending['decided'])

    def test_correct_code_after_three_wrong_does_not_approve(self):
        sending=self.armed()
        for _ in range(3):self.app.on_text('0000')
        self.app.on_text('4321')
        self.assertEqual(sending['answer'],'')

    def test_decision_is_signalled_even_if_telegram_fails(self):
        import telegram_api
        sending=self.armed()
        self.api.fail_methods['sendMessage']=[telegram_api.TelegramUnavailable('down')]*3
        self.app.on_text('4321')
        self.assertTrue(sending['event'].is_set());self.assertEqual(sending['answer'],'ok')

    def test_stale_cancel_button_of_another_vacancy_is_ignored(self):
        sending=self.armed()
        self.app.hh_control('hx','hh:2')
        self.assertFalse(sending.get('cancelled'));self.assertFalse(sending['event'].is_set())

    def test_failed_first_message_does_not_leave_send_stuck(self):
        import telegram_api
        self.api.fail_methods['sendMessage']=[telegram_api.TelegramUnavailable('down')]
        self.press('hs:hh:1');self.press('hx:hh:1');self.finish()
        self.assertIsNone(self.app.sending)

    def test_exact_letter_text_is_shown_before_the_code(self):
        self.app.sender=lambda packet,db,**kw:(self.runs.append(1),kw['confirmation_channel'](
            {'hh_resume_title':'R','form_image':str(self.image),'letter_path':packet['letter_path']},'ОТПРАВИТЬ 1'))[1] and 'confirmed_by_site' or 'cancelled_before_send'
        self.press('hs:hh:1');self.code()
        self.assertTrue(any('Здравствуйте! Письмо пользователя.' in t for t in self.api.texts()))
        self.press('hx:hh:1');self.finish()


class ReviewFixes2Tests(BotBase):
    def test_save_queued_while_busy_keeps_the_text_shown(self):
        import run_lock
        self.press('ed:hh:1');self.message('Первый текст')
        with patch('run_lock.one_run',side_effect=run_lock.RunBusy('busy')):self.press('sv:hh:1',message_id=self.api.next_id)
        self.press('ed:hh:1')  # clears the draft while the save waits
        self.app.run_actions()
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Первый текст')

    def test_unavailable_edit_does_not_send_a_duplicate(self):
        import telegram_api
        self.api.fail_methods['editMessageText']=[telegram_api.TelegramUnavailable('timeout')]
        self.press('op:hh:1',message_id=9)
        self.assertEqual([c for c in self.api.calls('sendMessage') if 'Письмо пользователя' in c['text']],[])

    def test_saved_edit_does_not_overwrite_a_message_that_now_shows_the_list(self):
        self.press('op:hh:1',message_id=9);self.press('ed:hh:1',message_id=9);self.message('Новый текст')
        self.press('ls:hh:1',message_id=9);self.press('sv:hh:1',message_id=self.api.next_id)
        self.assertFalse(any(e['message_id']==9 and 'Новый текст' in e['text'] for e in self.api.calls('editMessageText')))
        self.assertTrue(any('Новый текст' in c['text'] and c['keyboard'] for c in self.api.calls('sendMessage')))


class SeniorReviewTests(AiRewriteTests):
    """Senior review 13.11.2026 of the bot (findings 1-7, 9, 10)."""
    def conn(self):
        db=sqlite3.connect(self.db);self.addCleanup(db.close);return db

    def test_manual_edit_after_ai_button_is_not_sent_to_the_paid_model(self):
        self.press('ai:hh:1');self.press('ed:hh:1');self.message('Здравствуйте! Мой новый текст письма.')
        self.assertEqual(self.calls,[])
        self.assertIn('sv:hh:1',self.keyboard_codes(self.api.calls('sendMessage')[-1]))

    def test_save_under_an_older_preview_does_not_save_the_newer_text(self):
        self.press('ed:hh:1');self.message('Текст один');first=self.api.next_id
        self.message('Текст два');second=self.api.next_id
        self.press('sv:hh:1',message_id=first)
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Здравствуйте! Письмо пользователя.')
        self.assertIn('не последний',self.api.calls('answerCallbackQuery')[-1]['text'])
        self.press('sv:hh:1',message_id=second)
        self.assertEqual(self.letter.read_text(encoding='utf-8'),'Текст два')

    def test_queued_action_is_finished_once_even_if_the_notice_fails(self):
        import telegram_api
        db=self.conn();telegram_store.queue_action(db,'sk',{'key':'hh:1','chat_id':'42','message_id':5})
        self.api.fail_methods['sendMessage']=[telegram_api.TelegramUnavailable('down')]*3
        self.app.run_actions();self.api.fail_methods['sendMessage']=[];self.app.run_actions()
        self.assertEqual(telegram_store.due_actions(db),[])
        self.assertFalse(any('не выполнено' in t for t in self.api.texts()))
        with self.store() as store:self.assertEqual(store.get('1')['status'],'skipped')

    def test_queued_action_does_not_overwrite_what_the_message_shows_now(self):
        db=self.conn();telegram_store.queue_action(db,'sk',{'key':'hh:1','chat_id':'42','message_id':5})
        self.app.run_actions()
        self.assertEqual([e for e in self.api.calls('editMessageText') if e['message_id']==5],[])
        self.assertTrue(any('Исключено' in c['text'] for c in self.api.calls('sendMessage')))

    def test_attention_with_many_long_errors_still_answers_within_limits(self):
        items=[{'title':'Вакансия %d'%i,'company':'X','error':'Ошибка '*100} for i in range(25)]
        with patch('work_queue.issues',return_value=items):self.message('/attention')
        sent=self.api.calls('sendMessage')
        self.assertTrue(sent);self.assertTrue(all(len(c['text'])<=4096 for c in sent))
        self.assertIn('Требуют внимания',sent[0]['text'])

    def test_unavailable_telegram_does_not_count_toward_giving_up(self):
        import telegram_api
        db=self.conn();telegram_store.enqueue(db,'problem',{'message':'x'},'problem:a')
        for _ in range(4):
            self.api.fail_methods['sendMessage']=[telegram_api.TelegramUnavailable('down')];self.app.send_outbox()
        self.assertEqual(db.execute('SELECT attempts FROM telegram_outbox').fetchone()[0],0)
        self.api.fail_methods['sendMessage']=[];self.app.send_outbox()
        self.assertTrue(any(c['text'].endswith('x') for c in self.api.calls('sendMessage')))

    def test_locked_database_at_startup_does_not_stop_the_bot(self):
        import telegram_bot
        real=self.app.state;calls=[0]
        def flaky(name,default=None):
            calls[0]+=1
            if calls[0]==1:raise sqlite3.OperationalError('database is locked')
            return real(name,default)
        self.app.state=flaky;self.app.api.get_updates=lambda offset,timeout=50:[]
        code=telegram_bot.run_forever(self.app,sleep=lambda s:None,clock=lambda:0,stop=iter([False,True]).__next__)
        self.assertEqual(code,0)


class SeniorReviewHhTests(HhSendReviewTests):
    def test_cancel_after_accepted_code_is_not_contradictory(self):
        sending=self.armed()
        self.app.on_text('4321');self.app.hh_control('hx','hh:1')
        self.assertEqual(sending['answer'],'ok');self.assertFalse(sending.get('cancelled'))

    def test_sticker_or_photo_does_not_use_up_a_code_attempt(self):
        sending=self.armed()
        for _ in range(3):self.app.on_text('')
        self.assertFalse(sending['decided']);self.assertEqual(sending['attempts'],0)


if __name__=='__main__':unittest.main()
