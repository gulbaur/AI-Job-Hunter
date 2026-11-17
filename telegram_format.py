"""Telegram message texts and keyboards. Every external value is HTML-escaped."""
from datetime import datetime,timedelta,timezone
from html import escape
from pathlib import Path
import re

LIMIT=4096
PART=3500
LOCAL=timezone(timedelta(hours=5))
CODES={'ed','sv','cx','df','op','sk','rs','ms','my','mn','cv','ls','ai','hs','hx','hc'}
DECISIONS={'apply':'стоит откликнуться','stretch':'откликаться с оговорками'}


def local_time(iso):
    try:return datetime.fromisoformat(str(iso).replace('Z','+00:00')).astimezone(LOCAL).strftime('%H:%M')
    except (TypeError,ValueError):return '—'


def callback(code,key):
    return f'{code}:{key}'


def back(key):
    """«Back to the list» button; carries the key only because every callback of this bot names a letter."""
    return {'text':'⬅️ К списку писем','callback_data':callback('ls',key)}


def parse_callback(data):
    """(code, 'source:id') or None for anything that is not a button this bot made."""
    if not isinstance(data,str) or len(data.encode())>64:return None
    match=re.fullmatch(r'([a-z]{2}):(hh|web):(\d{1,30})',data)
    if not match or match[1] not in CODES:return None
    return match[1],f'{match[2]}:{match[3]}'


def split_text(text,limit=PART):
    parts=[];current=''
    for block in re.split(r'(\n\n)',text):
        if len(current)+len(block)<=limit:current+=block;continue
        if current.strip():parts.append(current.strip('\n'))
        current=''
        while len(block)>limit:
            cut=block.rfind('\n',0,limit)
            cut=cut if cut>0 else limit
            entity=block.rfind('&',max(0,cut-8),cut)
            if entity>0 and ';' not in block[entity:cut]:cut=entity  # never split "&amp;" — Telegram rejects broken HTML
            parts.append(block[:cut]);block=block[cut:].lstrip('\n')
        current=block
    if current.strip():parts.append(current.strip('\n'))
    return parts


def summary(payload):
    lines=[f"📋 <b>Подготовка {local_time(payload.get('started_at'))} завершена</b>",
           f"Новых писем: {int(payload.get('prepared') or 0)} · требуют внимания: {int(payload.get('attention') or 0)}"]
    statuses=[]
    for source in payload.get('sources') or []:
        name=escape(str(source.get('name') or ''))
        statuses.append(f'{name} ✓' if source.get('ok') else f"{name.lower()}: {escape(str(source.get('detail') or 'ошибка'))}")
    if statuses:lines.append('Поиск: '+' · '.join(statuses))
    cost=payload.get('cost')
    if isinstance(cost,(int,float)) and cost>0:lines.append(f'Расход на ИИ: ${cost:.3f}')
    links=[v for v in payload.get('links') or [] if isinstance(v,dict) and str(v.get('url','')).startswith('https://')]
    if links:  # user request 17.11.2026: what the program could not read is shown, not lost
        lines.append('🔗 <b>Не прочитаны автоматически — откройте сами:</b>')
        lines+=[f'• <a href="{escape(str(v["url"]),quote=True)}">{escape(str(v.get("label") or v["url"]))}</a>' for v in links]
    if payload.get('problem'):lines.append('⚠️ '+escape(str(payload['problem'])))
    return '\n'.join(lines)


LIST_MAX=20


def letter_list(entries,deferred,*,head='',undo=None):
    """One message: optional head (run summary / action result), then a button per letter in the queue.
    entries: [(key,title,company)]; deferred: keys the user postponed (listed last, marked ⏸)."""
    fresh=[e for e in entries if e[0] not in deferred];later=[e for e in entries if e[0] in deferred]
    lines=[head] if head else []
    if entries:
        count=f'📬 <b>Писем в очереди: {len(entries)}</b>'+(f' (⏸ отложено или открыто на сайте: {len(later)})' if later else '')
        lines+=[count,'Нажмите на вакансию — здесь откроются вакансия и письмо.']
    else:lines.append('Писем в очереди нет.')
    keyboard=[[{'text':'↩️ Вернуть: '+undo[1][:40],'callback_data':callback('rs',undo[0])}]] if undo else []
    for (key,title,company),mark in [(e,'🟢') for e in fresh]+[(e,'⏸') for e in later]:
        if len(keyboard)>=LIST_MAX+(1 if undo else 0):break
        title=' '.join(str(title or 'Вакансия').split());company=' '.join(str(company or '').split())
        label=f"{mark} {title}"+(f' · {company}' if company else '')
        if len(label)>60:label=label[:59].rstrip(' ·')+'…'
        keyboard.append([{'text':label,'callback_data':callback('op',key)}])
    if len(entries)>LIST_MAX:lines.append(f'Показаны первые {LIST_MAX}; остальные — на сайте программы.')
    return '\n'.join(lines),keyboard or None


def problem(message):
    return '⚠️ '+escape(str(message))


def _why(text):
    text=re.sub(r'\bJ\d{3}\b,?\s*','',str(text or ''))
    text=re.sub(r'\(\s*\)','',text);text=re.sub(r'\s+([.,;:])',r'\1',text);text=' '.join(text.split())
    return text[:300]+('…' if len(text)>300 else '')


def published_line(value):
    try:
        stamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
        return '📅 опубликована '+stamp.astimezone(LOCAL).strftime('%d.%m.%Y')
    except (TypeError,ValueError):return '📅 дата публикации неизвестна'


def card(packet,letter,edited,*,url,region='',published=None):
    key=f"{packet['source']}:{packet['vacancy_id']}"
    head=[f"🟢 <b>{escape(packet.get('title') or 'Вакансия')}</b> · {escape(packet.get('company') or '')}"
          f"   ({'HH' if packet['source']=='hh' else 'сайт'})"]
    meta=' · '.join(v for v in [escape(region or ''),DECISIONS.get(packet.get('decision'),'')] if v)
    if meta:head.append(meta)
    if published is not None:head.append(published_line(published))
    if packet.get('assessment_summary'):head.append('Почему: '+escape(_why(packet['assessment_summary'])))
    gaps=[escape(str(g.get('reason') or '')) for g in (packet.get('gaps') or [])[:2] if isinstance(g,dict) and g.get('reason')]
    if gaps:head.append('Пробелы: '+'; '.join(gaps))
    import actions
    cv=actions.cv_for_sending(packet).name
    head.append(f"CV: {escape(cv)} · {'✏️ изменено вами' if edited else '✔ проверено по источникам'}")
    body=escape(letter)
    keyboard=[[{'text':'✏️ Изменить','callback_data':callback('ed',key)},{'text':'🤖 Переписать с ИИ','callback_data':callback('ai',key)}],
              [{'text':'⏸ Отложить','callback_data':callback('df',key)},{'text':'❌ Не подходит','callback_data':callback('sk',key)}],
              [{'text':'✅ Отправлено вручную','callback_data':callback('ms',key)},{'text':'📎 CV','callback_data':callback('cv',key)}],
              [{'text':'🔗 Вакансия','url':url},back(key)]]
    if packet['source']=='hh':keyboard.insert(0,[{'text':'🚀 Откликнуться на HH','callback_data':callback('hs',key)}])
    text='\n'.join(head)+'\n\n'+body
    if len(text)<=LIMIT:return text,keyboard,[]
    return '\n'.join(head)+'\n\nТекст письма — в следующих сообщениях.',keyboard,split_text(body)
