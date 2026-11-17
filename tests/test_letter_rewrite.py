"""AI rewrite of a ready letter by the user's own instruction (bot button «🤖 Переписать с ИИ»). No real requests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import requests

from common import CheckError
from model_stub import setUpModule, tearDownModule  # noqa: F401
import draft_queue
import letter_rewrite
import search_settings
import sources
from storage import JobStore
from test_hunt import PREF,vacancy,response

RU_CV='Разрабатывала FastAPI-эндпоинты и JSON-схемы для сервиса заявок.\nДорабатывала RAG-поиск по документации с индексами SQLite и кэшем эмбеддингов.'
LETTER='Здравствуйте!\n\nМне интересны ваши задачи по RAG.\n\nРазрабатывала FastAPI-эндпоинты и дорабатывала RAG-поиск по документации.\n\nБуду рада обсудить.\n'
NEW='Здравствуйте!\n\nМне интересны ваши задачи по RAG, и я хочу работать в сильной команде.\n\nРазрабатывала FastAPI-эндпоинты.\n\nБуду рада обсудить.\n'


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);profile=self.root/'profile';profile.mkdir()
        (profile/'preferences.json').write_text(json.dumps(PREF),encoding='utf-8')
        search_settings.save(profile,{});sources.save(profile,{'enabled':['hh'],'boards':[],'career_pages':[]})
        cv=profile/'CV_APPLIED_AI_RU.docx';cv.write_bytes(b'fixture')
        folder=self.root/'drafts/hh-1/personal-abc';folder.mkdir(parents=True)
        self.letter=folder/'letter.txt';self.letter.write_text(LETTER,encoding='utf-8')
        (folder/'selection.json').write_text(json.dumps({'selection':{'cv_variant':'applied_ai','fact_ids':['AI001']}}),encoding='utf-8')
        (folder/'research.json').write_text(json.dumps({'fragments':{'C001':{'text':'Компания строит поиск по документам.','url':'https://example.com'}}}),encoding='utf-8')
        packet={'source':'hh','vacancy_id':'1','title':'AI Engineer','company':'Example','decision':'apply','url':'https://hh.kz/vacancy/1',
                'gaps':[],'letter_path':str(self.letter),'local_cv_path':str(cv),'output_dir':str(folder)}
        path=folder/'application.json';path.write_text(json.dumps(packet),encoding='utf-8')
        self.db=self.root/'data/jobs.sqlite';self.db.parent.mkdir()
        with JobStore(self.db) as store:
            store.save(vacancy(description='Нужен опыт с Python, FastAPI и RAG-поиском по документам.'));draft_queue.register(store.connection,path)

    def run_rewrite(self,session,instruction='Добавь, что хочу работать в сильной команде.',base=None):
        with JobStore(self.db) as store,patch('assess.read_cv',return_value=RU_CV):
            return letter_rewrite.rewrite(self.root,store,'hh:1',instruction,base_text=base,session=session,api_key='fixture',model='test')

    def test_rewrite_sends_instruction_letter_and_cv_facts_then_checks_facts(self):
        session=Mock();session.post.side_effect=[response({'letter':NEW}),response({'findings':[]})]
        result=self.run_rewrite(session)
        self.assertEqual(result['letter'],NEW)
        self.assertEqual(session.post.call_count,2)
        writer=json.loads(session.post.call_args_list[0].kwargs['json']['messages'][1]['content'])
        self.assertEqual(writer['instruction'],'Добавь, что хочу работать в сильной команде.')
        self.assertEqual(writer['letter'],LETTER)
        self.assertIn('FastAPI',' '.join(writer['cv_facts'].values()))
        audit=json.loads(session.post.call_args_list[1].kwargs['json']['messages'][1]['content'])
        self.assertEqual(audit['letter'],NEW);self.assertIn('candidate_instruction',audit)
        self.assertEqual(self.letter.read_text(encoding='utf-8'),LETTER)  # nothing saved without the user's «Сохранить»

    def test_next_instruction_builds_on_the_previous_proposal(self):
        session=Mock();session.post.side_effect=[response({'letter':NEW}),response({'findings':[]})]
        self.run_rewrite(session,'Убери последнюю фразу.',base=NEW)
        writer=json.loads(session.post.call_args_list[0].kwargs['json']['messages'][1]['content'])
        self.assertEqual(writer['letter'],NEW)

    def test_fact_check_rejection_names_the_quote(self):
        bad={'findings':[{'quote':'хочу работать в сильной команде','verdict':'unsupported','explanation':'Нет в CV.'}]}
        session=Mock();session.post.side_effect=[response({'letter':NEW}),response(bad)]
        with self.assertRaises(CheckError) as caught:self.run_rewrite(session)
        self.assertIn('хочу работать в сильной команде',str(caught.exception))

    def test_invented_number_is_refused_before_the_paid_fact_check(self):
        session=Mock();session.post.side_effect=[response({'letter':NEW.replace('Разрабатывала','За 3 года разрабатывала')})]
        with self.assertRaises(CheckError):self.run_rewrite(session)
        self.assertEqual(session.post.call_count,1)

    def test_missing_key_and_outage_are_clear_errors(self):
        with JobStore(self.db) as store,self.assertRaises(CheckError) as caught:
            letter_rewrite.rewrite(self.root,store,'hh:1','x',session=Mock(),api_key='',model='test')
        self.assertIn('OPENROUTER_API_KEY',str(caught.exception))
        session=Mock();session.post.side_effect=requests.ConnectionError('down')
        with self.assertRaises(CheckError):self.run_rewrite(session)



class LetterLanguageTests(unittest.TestCase):
    def test_language_follows_the_text_not_the_greeting(self):
        # Senior review 13.11 (web 13): an English letter starting «Dear…» was treated as Russian.
        import letter_rewrite
        self.assertEqual(letter_rewrite.letter_language('Dear hiring team,\nI built RAG search for product manuals.'),'en')
        self.assertEqual(letter_rewrite.letter_language('Здравствуйте!\nЯ разрабатывала RAG-поиск (FastAPI, SQLite).'),'ru')

if __name__=='__main__':unittest.main()
