"""Durable per-vacancy preparation stages, separate from application decisions."""
import json
from datetime import datetime,timezone

FAILED={'assessment_failed','letter_failed','rejected','refresh_failed'}
RUNNING={'assessing','writing','refreshing'}


def ensure(store):
    db=store.connection
    db.execute("CREATE TABLE IF NOT EXISTS discovery_backlog (source TEXT NOT NULL,vacancy_id TEXT NOT NULL,stage TEXT NOT NULL DEFAULT 'new',PRIMARY KEY(source,vacancy_id))")
    columns={r[1] for r in db.execute('PRAGMA table_info(discovery_backlog)')}
    with db:
        for name in ('error','updated_at'):
            if name not in columns:db.execute(f"ALTER TABLE discovery_backlog ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


def stage(store,job,value,error=''):
    ensure(store)
    key=(job['source'],job['vacancy_id']) if isinstance(job,dict) else tuple(job)
    with store.connection:
        store.connection.execute('''INSERT INTO discovery_backlog(source,vacancy_id,stage,error,updated_at)
            VALUES (?,?,?,?,?) ON CONFLICT(source,vacancy_id) DO UPDATE SET
            stage=excluded.stage,error=excluded.error,updated_at=excluded.updated_at''',
            (*key,value,error,datetime.now(timezone.utc).isoformat()))


def retry_required(store):
    ensure(store)
    return {(r[0],r[1]) for r in store.connection.execute(
        "SELECT source,vacancy_id FROM discovery_backlog WHERE stage NOT IN ('new','processed','ready','archived')")}


def issues(store,preferences,settings,*,running=False):
    from hunt import eligible
    ensure(store)
    allowed={(j['source'],j['vacancy_id']) for j in eligible(store,preferences,limit=100000,settings=settings)}
    result=[]
    for row in store.connection.execute('''SELECT b.*,j.title,j.company,j.url FROM discovery_backlog b
        JOIN jobs j USING(source,vacancy_id) ORDER BY b.updated_at DESC'''):
        if (row['source'],row['vacancy_id']) not in allowed:continue
        if row['stage'] not in FAILED and (running or row['stage'] not in RUNNING):continue
        result.append({**dict(row),'key':row['source']+':'+row['vacancy_id'],
            'error':row['error'] or 'Подготовка прервалась до сохранения результата. Можно повторить явно.'})
    return result


def recover_legacy(store):
    """Expose previously lost work without performing new model requests."""
    ensure(store);db=store.connection
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'assessments' not in tables:return
    queued=set()
    if 'draft_queue' in tables:queued={(r[0],r[1]) for r in db.execute('SELECT source,vacancy_id FROM draft_queue')}
    for row in db.execute("SELECT source,vacancy_id,status FROM jobs WHERE status NOT IN ('applied','skipped','archived')").fetchall():
        key=(row['source'],row['vacancy_id'])
        if key in queued:continue
        old=db.execute('SELECT stage FROM discovery_backlog WHERE source=? AND vacancy_id=?',key).fetchone()
        if old and old[0]!='processed':continue
        assessment=db.execute('SELECT result_json FROM assessments WHERE source=? AND vacancy_id=? ORDER BY created_at DESC LIMIT 1',key).fetchone()
        if assessment:
            try:decision=json.loads(assessment[0]).get('decision')
            except (ValueError,AttributeError):decision=None
            if decision and decision!='skip':stage(store,key,'letter_failed','Сохранена оценка, но готового письма нет. Повтор подготовки — только по вашему действию.')
        elif 'rejected_assessments' in tables and db.execute('SELECT 1 FROM rejected_assessments WHERE source=? AND vacancy_id=?',key).fetchone():
            stage(store,key,'assessment_failed','Предыдущая оценка отклонена. Можно запросить новую оценку явно.')
