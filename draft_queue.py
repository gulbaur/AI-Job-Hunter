"""Persistent local review queue; never opens a browser or calls a model."""
import json
import hashlib
import sqlite3
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
    columns={v[1] for v in db.execute('PRAGMA table_info(draft_queue)')}
    if 'reviewed_hash' not in columns:
        with db:db.execute('ALTER TABLE draft_queue ADD COLUMN reviewed_hash TEXT')
    if 'edited_by_user_at' not in columns:
        with db:db.execute('ALTER TABLE draft_queue ADD COLUMN edited_by_user_at TEXT')


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
    # A new letter version (new packet path) was written by the model, not edited by the user.
    conflict = ("DO UPDATE SET edited_by_user_at=CASE WHEN draft_queue.packet_path=excluded.packet_path "
                "THEN draft_queue.edited_by_user_at END, packet_path=excluded.packet_path, updated_at=CURRENT_TIMESTAMP"
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


def pending(store, preferences, settings=None, *, unread_only=False, problems=None, root=None):
    """Ready drafts. A queued draft whose files are gone or unreadable is skipped; with `problems`
    it is reported there ({source, vacancy_id, packet_path, error}) instead of vanishing silently."""
    from hunt import eligible
    from common import rebase
    root = Path(root) if root else Path(__file__).resolve().parent
    db = store.connection
    ensure(db)
    allowed = {(j['source'],j['vacancy_id']) for j in eligible(store, preferences, limit=100000,settings=settings)}
    result = []
    for row in db.execute('SELECT source,vacancy_id,packet_path,reviewed_hash FROM draft_queue ORDER BY updated_at DESC,source,vacancy_id'):
        if (row['source'],row['vacancy_id']) not in allowed:
            continue
        try:
            packet_path = rebase(row['packet_path'], root)
            if str(packet_path) != row['packet_path'] and packet_path.is_file():
                # Folder moved or restored: repair the stored path once, so marks and edits keep matching.
                try:
                    with db:
                        db.execute('UPDATE draft_queue SET packet_path=? WHERE source=? AND vacancy_id=? AND packet_path=?',
                                   (str(packet_path.resolve()), row['source'], row['vacancy_id'], row['packet_path']))
                except sqlite3.OperationalError:pass  # repaired on a later read
            packet = json.loads(packet_path.read_text(encoding='utf-8'))
            if packet['vacancy_id'] != row['vacancy_id'] or packet.get('source') != row['source']:
                continue
            for key in ('letter_path', 'local_cv_path', 'output_dir'):
                if packet.get(key):packet[key] = str(rebase(packet[key], root))
            missing = [name for name, key in (('файл письма', 'letter_path'), ('файл CV', 'local_cv_path'))
                       if not Path(packet[key]).is_file()]
            if missing:
                raise FileNotFoundError(' и '.join(missing) + ' не найден')
            if unread_only and row['reviewed_hash']==review_hash(packet_path,packet):
                continue
            result.append((packet_path, packet))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if problems is not None:
                reason = str(exc) if isinstance(exc, FileNotFoundError) and not exc.filename else 'файл черновика не прочитан'
                problems.append({'source': row['source'], 'vacancy_id': row['vacancy_id'],
                                 'packet_path': row['packet_path'], 'error': reason})
            continue
    return result
