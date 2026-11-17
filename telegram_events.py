"""Producers of Telegram notifications. They never raise: a failed notice must not break a run."""
from pathlib import Path
import re
import sqlite3

import telegram_store


def configured(root):
    return telegram_store.configured(root)


def _enqueue(db_path,items):
    try:
        db=sqlite3.connect(db_path)
        try:
            for kind,payload,key in items:telegram_store.enqueue(db,kind,payload,key)
        finally:db.close()
    except Exception as exc:
        print(f'Уведомление Telegram не поставлено в очередь: {type(exc).__name__}.')


def _sources(report):
    entries=(report.get('coverage') or {}).get('sources') or []
    warnings=report.get('coverage_warnings') or []
    def warning(prefix):return next((w for w in warnings if w.startswith(prefix)),'')
    result=[]
    for entry in entries:
        if entry.get('source')=='HH':
            ok=entry.get('status') in {'checked','limited'}
            result.append({'name':'HH','ok':ok,'detail':'' if ok else (warning('HH') or 'проверен не полностью')})
    boards=[e for e in entries if e.get('slug')]
    if boards:
        failed=[e.get('source','') for e in boards if e.get('status')!='checked']
        result.append({'name':'Сайты компаний','ok':not failed,'detail':'ошибка: '+', '.join(failed) if failed else ''})
    for entry in entries:
        if entry.get('source')=='Веб-поиск':
            ok=entry.get('status')=='fresh'
            result.append({'name':'Веб-поиск','ok':ok,'detail':'' if ok else (warning('Веб-поиск') or str(entry.get('status')))})
    return result


LINKS_MAX=8
LINKS_DAYS=14


def link_label(url):
    from urllib.parse import urlsplit
    parts=urlsplit(url);host=(parts.hostname or '').removeprefix('www.')
    segments=[v for v in parts.path.split('/') if v]
    words=re.sub(r'[-_]+',' ',segments[-1] if segments else '');words=re.sub(r'\s*\d{5,}\s*$','',words).strip()
    if len(words)<4 and parts.query:words=parts.query
    return (host+(': '+words if words else ''))[:70]


def unread_links(db_path):
    """Links found by search that the program cannot read itself; each is sent once (user request 17.11.2026).
    Links the AI reader will still retry, dead links (404/410) and LinkedIn search pages are not sent."""
    from datetime import datetime,timezone,timedelta
    db=sqlite3.connect(db_path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovered_links'").fetchone():return []
        if 'reported_at' not in [r[1] for r in db.execute('PRAGMA table_info(discovered_links)')]:
            with db:db.execute('ALTER TABLE discovered_links ADD COLUMN reported_at TEXT')
        since=(datetime.now(timezone.utc)-timedelta(days=LINKS_DAYS)).isoformat()
        rows=db.execute("SELECT url,state,note FROM discovered_links WHERE reported_at IS NULL AND last_seen_at>=? AND "
            "(state='manual_link' OR (state='needs_adapter' AND note LIKE 'Страница без текста%') OR "
            "(state='error' AND note LIKE '%HTTP 403%')) ORDER BY last_seen_at DESC",(since,)).fetchall()
        chosen=[url for url,state,_ in rows if not ('linkedin.com' in url and '/jobs/view/' not in url)][:LINKS_MAX]
        stamp=datetime.now(timezone.utc).isoformat()
        with db:db.executemany('UPDATE discovered_links SET reported_at=? WHERE url=?',[(stamp,u) for u in chosen])
        return [{'url':u,'label':link_label(u)} for u in chosen]
    finally:db.close()


def run_finished(db_path,report,*,root):
    """Summary plus one card per new letter of a finished --run --queue preparation (only when the bot is set up)."""
    try:
        if not configured(root):return
        run_id=str(report.get('run_id') or report.get('started_at') or '')
        error=report.get('error') if report.get('state') in {'stopped','interrupted'} else ''
        # One message per run (user request 13.11.2026): the summary with a button per letter; the bot builds the
        # list when it sends, so letters already handled on the site are not offered again.
        items=[('digest',{'started_at':report.get('started_at'),'prepared':len(report.get('packet_paths') or []),
                'attention':len(report.get('work_issues') or []),'sources':_sources(report),'problem':error or '',
                'cost':(report.get('model_usage') or {}).get('known_cost_usd'),'links':unread_links(db_path)},
                'digest:'+run_id)]
    except Exception as exc:
        print(f'Уведомление Telegram не подготовлено: {type(exc).__name__}.');return
    _enqueue(db_path,items)


def problem(db_path,message,signature):
    _enqueue(db_path,[('problem',{'message':str(message)},'problem:'+str(signature))])
