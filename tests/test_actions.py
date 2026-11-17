"""Shared letter/application actions used by the web UI and the Telegram bot."""
import json
from pathlib import Path
import tempfile
import unittest

import actions
from common import CheckError
import draft_queue
import hh_response
import search_settings
import sources
from storage import JobStore
from test_hunt import PREF,vacancy


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);profile=self.root/'profile';profile.mkdir()
        (profile/'preferences.json').write_text(json.dumps(PREF),encoding='utf-8')
        search_settings.save(profile,{});sources.save(profile,{'enabled':['hh'],'boards':[],'career_pages':[]})
        self.cv=profile/'cv.docx';self.cv.write_bytes(b'fixture')
        self.db=self.root/'data/jobs.sqlite';self.db.parent.mkdir()
        self.add_packet('hh','1')

    def add_packet(self,source,identity,*,letter_dir=None):
        folder=letter_dir or self.root/'drafts'/f'{source}-{identity}';folder.mkdir(parents=True,exist_ok=True)
        letter=folder/'letter.txt';letter.write_text('Письмо пользователя',encoding='utf-8')
        url=f'https://hh.kz/vacancy/{identity}' if source=='hh' else f'https://company.example/jobs/{identity}'
        packet={'source':source,'vacancy_id':identity,'title':'AI Engineer','company':'Example','decision':'stretch',
            'url':url,'gaps':[],'letter_path':str(letter),'local_cv_path':str(self.cv),'output_dir':str(folder)}
        path=folder/'application.json';path.write_text(json.dumps(packet),encoding='utf-8')
        with self.store() as store:
            job=vacancy(identity)
            if source=='web':
                job={**job,'alternate_url':url}
            store.save(job) if source=='hh' else store.save(job,source='web')
            draft_queue.register(store.connection,path)
        return path,letter

    def store(self):return JobStore(self.db)

    def letter(self,identity='1',source='hh'):
        return (self.root/'drafts'/f'{source}-{identity}'/'letter.txt').read_text(encoding='utf-8')

    def test_moved_project_folder_keeps_drafts_and_edits_the_new_copy(self):
        # Audit A13: absolute paths in draft_queue/packets hid every draft after the folder was moved or restored.
        import shutil
        moved=Path(tempfile.mkdtemp());self.addCleanup(lambda:shutil.rmtree(moved,ignore_errors=True))
        shutil.copytree(self.root,moved/'job hunter');new=moved/'job hunter'
        shutil.rmtree(self.root/'drafts');(self.root/'profile'/'cv.docx').unlink()
        with JobStore(new/'data/jobs.sqlite') as store:
            items=actions.pending(new,store)
            self.assertEqual([i['vacancy_id'] for _,i in items],['1'])
            self.assertTrue(Path(items[0][1]['letter_path']).is_relative_to(new))
            actions.save_letter(new,store,'hh:1','Новый текст')
            actions.defer(new,store,'hh:1')
            unread=draft_queue.pending(store,PREF,search_settings.load(new/'profile'),unread_only=True,root=new)
        self.assertEqual((new/'drafts/hh-1/letter.txt').read_text(encoding='utf-8'),'Новый текст')
        self.assertEqual(unread,[])

    def test_save_letter_writes_text_and_marks_reviewed(self):
        with self.store() as store:
            actions.save_letter(self.root,store,'hh:1','Новый текст')
            row=store.connection.execute("SELECT reviewed_hash FROM draft_queue WHERE vacancy_id='1'").fetchone()
        self.assertEqual(self.letter(),'Новый текст');self.assertTrue(row[0])

    def test_save_letter_rejects_empty_and_oversized(self):
        with self.store() as store:
            for text in ['',' \n','x'*12001]:
                with self.subTest(size=len(text)),self.assertRaises(CheckError):actions.save_letter(self.root,store,'hh:1',text)
        self.assertEqual(self.letter(),'Письмо пользователя')

    def test_skip_then_restore_roundtrip(self):
        with self.store() as store:
            info=actions.skip(self.root,store,'hh:1')
            self.assertEqual(info['key'],'hh:1')
            row=store.get('1')
            self.assertEqual((row['status'],row['status_reason']),('skipped','user'))
            self.assertEqual(actions.pending(self.root,store),[])
            actions.restore(self.root,store,'hh:1')
            self.assertEqual(store.get('1')['status'],'new')

    def test_restore_twice_is_harmless_error(self):
        with self.store() as store:
            actions.skip(self.root,store,'hh:1');actions.restore(self.root,store,'hh:1')
            with self.assertRaises(CheckError) as caught:actions.restore(self.root,store,'hh:1')
        self.assertIn('уже восстановлена',str(caught.exception))

    def test_manual_sent_external_records_history(self):
        self.add_packet('web','2')
        with self.store() as store:
            actions.manual_sent(self.root,store,'web:2')
            state=store.connection.execute("SELECT state FROM application_attempts WHERE target_url='https://company.example/jobs/2'").fetchone()[0]
            self.assertEqual(state,'reported_by_user');self.assertEqual(store.get('2','web')['status'],'applied')

    def attempt(self,state):
        with self.store() as store:
            store.connection.executescript(hh_response.SCHEMA)
            with store.connection:store.connection.execute('INSERT INTO application_attempts VALUES (?,?,?,?)',
                ('https://hh.kz/vacancy/1','hash',state,'2026-11-10T00:00:00+00:00'))

    def attempt_state(self):
        with self.store() as store:
            return store.connection.execute("SELECT state FROM application_attempts WHERE target_url='https://hh.kz/vacancy/1'").fetchone()[0]

    def test_resolve_attempt_outcomes(self):
        self.attempt('submitted_unverified')
        with self.store() as store:actions.resolve_attempt(self.root,store,'https://hh.kz/vacancy/1','not_sent')
        self.assertEqual(self.attempt_state(),'hh_form_closed')
        with self.store() as store:
            store.connection.execute("UPDATE application_attempts SET state='submit_started'");store.connection.commit()
            actions.resolve_attempt(self.root,store,'https://hh.kz/vacancy/1','delivered')
            self.assertEqual(store.get('1')['status'],'applied')
        self.assertEqual(self.attempt_state(),'reported_by_user')
        with self.store() as store:
            store.connection.execute("UPDATE application_attempts SET state='confirmed_by_site'");store.connection.commit()
            with self.assertRaises(CheckError):actions.resolve_attempt(self.root,store,'https://hh.kz/vacancy/1','not_sent')

    def test_manual_sent_hh_blocks_program_send(self):
        import hunt
        with self.store() as store:
            actions.manual_sent(self.root,store,'hh:1')
            self.assertEqual(store.connection.execute("SELECT state FROM application_attempts").fetchone()[0],'reported_by_user')
            _,item=None,json.loads((self.root/'drafts/hh-1/application.json').read_text(encoding='utf-8'))
            with self.assertRaises(CheckError):hh_response.reserve(store.connection,item,'hash')
            self.assertEqual(hunt.eligible(store,PREF,[('hh','1')]),[])

    def test_manual_sent_hh_after_closed_form_records_and_blocks(self):
        # Final review #1: an earlier hh_form_closed row kept its state, so reserve() succeeded again.
        self.attempt('hh_form_closed')
        with self.store() as store:
            actions.manual_sent(self.root,store,'hh:1')
            item=json.loads((self.root/'drafts/hh-1/application.json').read_text(encoding='utf-8'))
            with self.assertRaises(CheckError):hh_response.reserve(store.connection,item,'hash')
        self.assertEqual(self.attempt_state(),'reported_by_user')

    def test_save_letter_marks_user_edited(self):
        path=self.root/'drafts/hh-1/application.json';item=json.loads(path.read_text(encoding='utf-8'))
        with self.store() as store:
            self.assertFalse(actions.is_user_edited(store,item))
            actions.save_letter(self.root,store,'hh:1','Своя редакция')
            self.assertTrue(actions.is_user_edited(store,item))
            draft_queue.register(store.connection,path)
            self.assertTrue(actions.is_user_edited(store,item))

    def test_new_letter_version_clears_user_edit_mark(self):
        with self.store() as store:actions.save_letter(self.root,store,'hh:1','Своя редакция')
        path,_=self.add_packet('hh','1',letter_dir=self.root/'drafts/hh-1/personal-new')
        item=json.loads(path.read_text(encoding='utf-8'))
        with self.store() as store:self.assertFalse(actions.is_user_edited(store,item))

    def test_cv_for_sending_prefers_current_pdf_next_to_docx(self):
        # User request 11.11.2026: send the CV as PDF; profile has «CV_*.docx.pdf» exported next to each docx.
        import os
        item={'local_cv_path':str(self.cv)}
        self.assertEqual(actions.cv_for_sending(item),self.cv)  # no PDF yet: the docx itself
        pdf=Path(str(self.cv)+'.pdf');pdf.write_bytes(b'%PDF-1.7 fixture')
        stamp=self.cv.stat().st_mtime
        os.utime(pdf,(stamp-30,stamp-30))  # real files: PDF exported 30 s before the docx was saved
        self.assertEqual(actions.cv_for_sending(item),pdf)
        os.utime(pdf,(stamp-3600,stamp-3600))  # docx edited an hour after the PDF: the PDF is stale
        self.assertEqual(actions.cv_for_sending(item),self.cv)
        os.utime(pdf,(stamp,stamp));pdf.write_bytes(b'not a pdf')
        self.assertEqual(actions.cv_for_sending(item),self.cv)

    def test_packet_outside_drafts_is_refused(self):
        outside=Path(tempfile.mkdtemp());self.addCleanup(lambda:__import__('shutil').rmtree(outside,ignore_errors=True))
        path,_=self.add_packet('hh','3')
        packet=json.loads(path.read_text(encoding='utf-8'))
        (outside/'letter.txt').write_text('x',encoding='utf-8')
        packet['letter_path']=str(outside/'letter.txt');path.write_text(json.dumps(packet),encoding='utf-8')
        with self.store() as store,self.assertRaises(CheckError):actions.packet(self.root,store,'hh:3')


if __name__=='__main__':unittest.main()
