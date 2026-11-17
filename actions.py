"""Letter and application actions shared by the web UI and the Telegram bot.

Callers hold run_lock.one_run(root/'profile/hunt.lock'). Confirmation phrases and codes are checked
by each interface; these functions enforce the data rules (paths, states, double-response protection).
"""
from datetime import datetime,timezone
from pathlib import Path

from common import CheckError
import draft_queue
import hh_response
from preferences import load_preferences
import search_settings
import work_queue

MAX_LETTER=12000
GONE='Этот черновик уже обработан, исключён или недоступен. Обновите список.'
OPEN_ATTEMPT={'hh_prepared','hh_form_closed'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def pending(root,store):
    root=Path(root)
    draft_queue.import_existing(store.connection,root)
    return draft_queue.pending(store,load_preferences(root/'profile'),search_settings.load(root/'profile'),root=root)


def packet(root,store,key):
    drafts=(Path(root)/'drafts').resolve()
    for path,item in pending(root,store):
        if item['source']+':'+item['vacancy_id']==key:
            if not path.resolve().is_relative_to(drafts):break
            if not Path(item['letter_path']).resolve().is_relative_to(drafts):break
            return path,item
    raise CheckError(GONE)


PDF_GRACE_S=600  # the user's PDFs were exported ~30 s before the docx was last saved


def cv_for_sending(item):
    """The file to give the user/employer: the PDF exported next to the CV («CV_X.docx.pdf» or «CV_X.pdf») when it
    is a real PDF and not older than the docx (beyond a short grace), otherwise the docx. The model always reads
    the docx; an outdated PDF is never sent."""
    cv=Path(str(item.get('local_cv_path') or ''))
    for pdf in (Path(str(cv)+'.pdf'),cv.with_suffix('.pdf')):
        try:
            if pdf.is_file() and pdf.read_bytes()[:5]==b'%PDF-' and pdf.stat().st_mtime>=cv.stat().st_mtime-PDF_GRACE_S:
                return pdf
        except OSError:continue
    return cv


def _split(key):
    parts=str(key or '').split(':')
    if len(parts)!=2 or parts[0] not in {'hh','web'} or not parts[1].isascii() or not parts[1].isdigit():
        raise CheckError('Некорректная вакансия.')
    return parts[0],parts[1]


def save_letter(root,store,key,text):
    if not isinstance(text,str) or not text.strip() or len(text)>MAX_LETTER:
        raise CheckError('Письмо должно содержать от 1 до 12 000 символов.')
    path,item=packet(root,store,key)
    from common import replace_file
    letter=Path(item['letter_path']);temp=letter.with_suffix('.tmp')
    temp.write_text(text,encoding='utf-8')
    try:replace_file(temp,letter)
    except OSError:
        temp.unlink(missing_ok=True)
        raise CheckError('Письмо НЕ сохранено: файл занят другой программой. Подождите несколько секунд и сохраните ещё раз.') from None
    draft_queue.mark_reviewed(store.connection,path,item)
    with store.connection:
        store.connection.execute('UPDATE draft_queue SET edited_by_user_at=? WHERE source=? AND vacancy_id=?',
                                 (stamp(),item['source'],item['vacancy_id']))


def is_user_edited(store,item):
    draft_queue.ensure(store.connection)
    row=store.connection.execute('SELECT edited_by_user_at FROM draft_queue WHERE source=? AND vacancy_id=?',
                                 (item['source'],item['vacancy_id'])).fetchone()
    return bool(row and row[0])


def defer(root,store,key):
    path,item=packet(root,store,key)
    draft_queue.mark_reviewed(store.connection,path,item)


def skip(root,store,key):
    _,item=packet(root,store,key)
    with store.connection:
        store.connection.execute("UPDATE jobs SET status='skipped',status_reason='user' WHERE source=? AND vacancy_id=?",
                                 (item['source'],item['vacancy_id']))
    return {'key':key,'title':item.get('title') or 'Вакансия','company':item.get('company') or ''}


def restore(root,store,key):
    source,identity=_split(key)
    row=store.get(identity,source)
    if row is None or row['status']!='skipped':raise CheckError('Вакансия уже восстановлена или недоступна.')
    store.connection.executescript(hh_response.SCHEMA)
    attempt=store.connection.execute('SELECT state FROM application_attempts WHERE target_url=?',(row['url'],)).fetchone()
    if attempt and attempt[0] not in OPEN_ATTEMPT:raise CheckError('Отправка уже начиналась; восстановление не разрешает повторный отклик.')
    with store.connection:
        store.connection.execute("UPDATE jobs SET status='new',status_reason='restored_by_user' WHERE source=? AND vacancy_id=?",(source,identity))
    work_queue.stage(store,(source,identity),'new')


def manual_sent(root,store,key):
    # HH too: the user may respond in the HH app; the record blocks any later program send for this vacancy.
    _,item=packet(root,store,key)
    store.connection.executescript(hh_response.SCHEMA)
    with store.connection:
        store.connection.execute('''INSERT INTO application_attempts VALUES (?,?,?,?) ON CONFLICT(target_url) DO UPDATE
            SET state='reported_by_user',packet_hash=excluded.packet_hash,updated_at=excluded.updated_at
            WHERE application_attempts.state IN ('hh_prepared','hh_form_closed')''',
                                 (item['url'],'manual-user-confirmation','reported_by_user',stamp()))
        store.connection.execute("UPDATE jobs SET status='applied' WHERE source=? AND vacancy_id=?",(item['source'],item['vacancy_id']))


def unresolved_attempt(store,url):
    """(source, vacancy_id) of an HH attempt whose result is unknown; otherwise CheckError."""
    store.connection.executescript(hh_response.SCHEMA)
    row=store.connection.execute('''SELECT a.state,j.source,j.vacancy_id FROM application_attempts a
        LEFT JOIN jobs j ON j.url=a.target_url WHERE a.target_url=?''',(str(url or ''),)).fetchone()
    if not row or row[0] not in {'submitted_unverified','submit_started'} or row[1]!='hh':
        raise CheckError('Для этой записи отметка результата не требуется.')
    return row[1],row[2]


def resolve_attempt(root,store,url,outcome):
    state={'delivered':'reported_by_user','not_sent':'hh_form_closed'}.get(outcome)
    if not state:raise CheckError('Неизвестный результат проверки.')
    _,identity=unresolved_attempt(store,url)
    with store.connection:
        store.connection.execute('UPDATE application_attempts SET state=?,updated_at=? WHERE target_url=?',(state,stamp(),url))
        if state=='reported_by_user':
            store.connection.execute("UPDATE jobs SET status='applied' WHERE source='hh' AND vacancy_id=?",(identity,))
