"""Persistent local review queue; never opens a browser or calls a model."""
import json
import hashlib
from pathlib import Path

from common import CheckError

SCHEMA = """
CREATE TABLE IF NOT EXISTS draft_queue (
    source TEXT NOT NULL DEFAULT 'hh',
    vacancy_id TEXT NOT NULL,
    packet_path TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_hash TEXT,
    PRIMARY KEY(source,vacancy_id)
);
"""


def ensure(db):
    columns=db.execute('PRAGMA table_info(draft_queue)').fetchall()
    if columns and 'source' not in {v[1] for v in columns}:
        with db:
            db.execute('ALTER TABLE draft_queue RENAME TO draft_queue_legacy')
            db.execute(SCHEMA)
            db.execute("INSERT INTO draft_queue(source,vacancy_id,packet_path,updated_at) SELECT 'hh',vacancy_id,packet_path,updated_at FROM draft_queue_legacy")
            db.execute('DROP TABLE draft_queue_legacy')
    else:db.execute(SCHEMA)
    if 'reviewed_hash' not in {v[1] for v in db.execute('PRAGMA table_info(draft_queue)')}:
        with db:db.execute('ALTER TABLE draft_queue ADD COLUMN reviewed_hash TEXT')


def review_hash(path, packet):
    return hashlib.sha256(Path(path).read_bytes()+b'\0'+Path(packet['letter_path']).read_bytes()).hexdigest()


def mark_reviewed(db, path, packet):
    """Record display only; never changes application or job status."""
    ensure(db)
    fingerprint=review_hash(path, packet)
    with db:
        db.execute('UPDATE draft_queue SET reviewed_hash=? WHERE source=? AND vacancy_id=? AND packet_path=?',
                   (fingerprint,packet['source'],packet['vacancy_id'],str(Path(path).resolve())))


def register(db, packet_path, *, replace=True):
    path = Path(packet_path).resolve()
    packet = json.loads(path.read_text(encoding='utf-8'))
    if packet.get('source') not in {'hh','web'} or not str(packet.get('vacancy_id', '')).isascii() or not str(packet['vacancy_id']).isdigit():
        raise CheckError('Некорректный пакет вакансии.')
    ensure(db)
    conflict = ("DO UPDATE SET packet_path=excluded.packet_path, updated_at=CURRENT_TIMESTAMP"
                if replace else 'DO NOTHING')
    with db:
        db.execute('INSERT INTO draft_queue(source,vacancy_id,packet_path) VALUES (?,?,?) '
                   'ON CONFLICT(source,vacancy_id) '+conflict, (packet['source'],packet['vacancy_id'], str(path)))


def import_existing(db, root):
    """One-time discovery of old drafts, newest packet per job; never replace queued edits."""
    for path in sorted((Path(root)/'drafts'/'runs').glob('**/application.json'), reverse=True):
        try:
            register(db, path, replace=False)
        except (CheckError, OSError, ValueError, KeyError, TypeError):
            continue


def pending(store, preferences, settings=None, *, unread_only=False):
    from hunt import eligible
    db = store.connection
    ensure(db)
    allowed = {(j['source'],j['vacancy_id']) for j in eligible(store, preferences, limit=100000,settings=settings)}
    result = []
    for row in db.execute('SELECT source,vacancy_id,packet_path,reviewed_hash FROM draft_queue ORDER BY updated_at DESC,source,vacancy_id'):
        if (row['source'],row['vacancy_id']) not in allowed:
            continue
        try:
            packet = json.loads(Path(row['packet_path']).read_text(encoding='utf-8'))
            if packet['vacancy_id'] != row['vacancy_id'] or packet.get('source') != row['source']:
                continue
            if not Path(packet['letter_path']).is_file() or not Path(packet['local_cv_path']).is_file():
                continue
            if unread_only and row['reviewed_hash']==review_hash(row['packet_path'],packet):
                continue
            result.append((Path(row['packet_path']), packet))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result
