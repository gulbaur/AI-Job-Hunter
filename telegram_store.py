"""Telegram bot tables in data/jobs.sqlite: notification outbox, card messages, small state, deferred actions."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path

SCHEMA='''
CREATE TABLE IF NOT EXISTS telegram_outbox(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, sent_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS telegram_cards(job_key TEXT PRIMARY KEY, chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_state(name TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_actions(id INTEGER PRIMARY KEY, action TEXT NOT NULL, data_json TEXT NOT NULL,
    created_at TEXT NOT NULL, done_at TEXT, result TEXT);
'''


def stamp():
    return datetime.now(timezone.utc).isoformat()


def ensure(db):
    db.executescript(SCHEMA)


def settings(root):
    """(token, chat_id) from the project's .env, falling back to the process environment."""
    from dotenv import dotenv_values
    values={}
    try:values=dotenv_values(Path(root)/'.env',encoding='utf-8-sig')
    except OSError:pass
    def value(name):return str(values.get(name) or os.environ.get(name,'') or '').strip()
    return value('TELEGRAM_BOT_TOKEN'),value('TELEGRAM_CHAT_ID')


def configured(root):
    return all(settings(root))


def enqueue(db,kind,payload,dedupe_key):
    ensure(db)
    with db:
        cursor=db.execute('INSERT INTO telegram_outbox(kind,payload_json,dedupe_key,created_at) VALUES (?,?,?,?) '
                          'ON CONFLICT(dedupe_key) DO NOTHING',(kind,json.dumps(payload,ensure_ascii=False),dedupe_key,stamp()))
    return cursor.rowcount==1


def due_outbox(db,limit=20):
    ensure(db)
    rows=db.execute('SELECT id,kind,payload_json FROM telegram_outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?',(limit,)).fetchall()
    return [{'id':r[0],'kind':r[1],'payload':json.loads(r[2])} for r in rows]


def mark_sent(db,item_id):
    ensure(db)
    with db:db.execute('UPDATE telegram_outbox SET sent_at=? WHERE id=?',(stamp(),item_id))


def mark_failed(db,item_id,error,*,count=True):
    """count=False for network trouble: only permanent errors move a notification toward being given up."""
    ensure(db)
    with db:db.execute('UPDATE telegram_outbox SET attempts=attempts+?,last_error=? WHERE id=?',(1 if count else 0,str(error)[:300],item_id))


def get_state(db,name,default=None):
    ensure(db)
    row=db.execute('SELECT value_json FROM telegram_state WHERE name=?',(name,)).fetchone()
    return json.loads(row[0]) if row else default


def set_state(db,name,value):
    ensure(db)
    with db:
        db.execute('INSERT INTO telegram_state VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json,'
                   'updated_at=excluded.updated_at',(name,json.dumps(value,ensure_ascii=False),stamp()))


def clear_state(db,name):
    ensure(db)
    with db:db.execute('DELETE FROM telegram_state WHERE name=?',(name,))


def queue_action(db,action,data):
    ensure(db)
    with db:
        cursor=db.execute('INSERT INTO telegram_actions(action,data_json,created_at) VALUES (?,?,?)',
                          (action,json.dumps(data,ensure_ascii=False),stamp()))
    return cursor.lastrowid


def due_actions(db):
    ensure(db)
    rows=db.execute('SELECT id,action,data_json FROM telegram_actions WHERE done_at IS NULL ORDER BY id').fetchall()
    return [{'id':r[0],'action':r[1],'data':json.loads(r[2])} for r in rows]


def finish_action(db,action_id,result):
    ensure(db)
    with db:db.execute('UPDATE telegram_actions SET done_at=?,result=? WHERE id=?',(stamp(),str(result)[:500],action_id))


def remember_card(db,job_key,chat_id,message_id):
    ensure(db)
    with db:
        db.execute('INSERT INTO telegram_cards VALUES (?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET chat_id=excluded.chat_id,'
                   'message_id=excluded.message_id,updated_at=excluded.updated_at',(job_key,str(chat_id),int(message_id),stamp()))


def forget_message(db,chat_id,message_id):
    ensure(db)
    with db:db.execute('DELETE FROM telegram_cards WHERE chat_id=? AND message_id=?',(str(chat_id),int(message_id)))


def card(db,job_key):
    ensure(db)
    row=db.execute('SELECT chat_id,message_id FROM telegram_cards WHERE job_key=?',(job_key,)).fetchone()
    return (row[0],row[1]) if row else None
