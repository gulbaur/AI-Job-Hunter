"""Bounded, auditable discovery across HH, company boards and web-search leads."""
import argparse
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin,urlsplit

import requests
from common import CheckError,html_to_text
from preferences import company_is_excluded,load_preferences
import search_settings
import sources
from storage import JobStore

ROOT=Path(__file__).resolve().parent
REPORT=ROOT/'data/last_discovery.json'
WEB=ROOT/'data/web_discovery.json'
LINK_SCHEMA='''CREATE TABLE IF NOT EXISTS discovered_links (
    url TEXT PRIMARY KEY, found_from TEXT NOT NULL, state TEXT NOT NULL,
    note TEXT NOT NULL, last_seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discovery_backlog (
    source TEXT NOT NULL, vacancy_id TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'new',
    PRIMARY KEY(source,vacancy_id));'''


def pending_keys(store):
    store.connection.executescript(LINK_SCHEMA)
    return [(r[0],r[1]) for r in store.connection.execute("SELECT b.source,b.vacancy_id FROM discovery_backlog b JOIN jobs j USING(source,vacancy_id) WHERE b.stage='new' ORDER BY j.first_seen_at,b.source,b.vacancy_id")]


def recover_unassessed(store,settings,preferences):
    """Bring eligible legacy saves into the queue without retrying prior model work."""
    from hunt import eligible
    db=store.connection
    db.executescript(LINK_SCHEMA)
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    handled=set()
    for table in ('assessments','rejected_assessments','draft_queue','discovery_backlog'):
        if table in tables:
            handled.update((r[0],r[1]) for r in db.execute(f'SELECT source,vacancy_id FROM {table}'))
    keys=[(j['source'],j['vacancy_id']) for j in eligible(store,preferences,limit=100000,settings=settings)
          if (j['source'],j['vacancy_id']) not in handled]
    with db:
        db.executemany('INSERT INTO discovery_backlog(source,vacancy_id) VALUES (?,?) ON CONFLICT DO NOTHING',keys)
    return len(keys)


def print_summary(report):
    entries=report.get('sources',[])
    boards=[e for e in entries if e.get('kind') in {'ashby','greenhouse','lever'}]
    if boards:
        print(f"Сайты компаний: досок {len(boards)}, карточек просмотрено {sum(e.get('seen',0) for e in boards)}; "
              f"новых сохранено {sum(e.get('saved',0) for e in boards)}; ошибок {sum(e['status']=='error' for e in boards)}.")
    web=next((e for e in entries if e.get('source')=='Веб-поиск'),None)
    if web:
        state={'fresh':'использованы свежие сохранённые результаты',
               'missing':'нет сохранённых результатов','stale':'сохранённые результаты устарели',
               'partial':'часть запросов завершилась ошибкой','failed':'ни один запрос не выполнен'}.get(web['status'],'результаты требуют проверки')
        print('Веб-поиск: '+state+'. Поиск выполняется программой через OpenRouter.')
    manual=sum(e.get('status')=='manual_link' for e in entries)
    unreadable=sum(e.get('status') in {'needs_adapter','error','unavailable'} for e in entries)
    if manual or unreadable:
        print(f'Дополнительные ссылки: для ручного просмотра {manual}; не удалось полностью проверить {unreadable}.')


def processed(store,jobs):
    store.connection.executescript(LINK_SCHEMA)
    with store.connection:
        for job in jobs:
            store.connection.execute("UPDATE discovery_backlog SET stage='processed' WHERE source=? AND vacancy_id=?",(job['source'],job['vacancy_id']))


def write_json(path,value):
    from common import write_json as atomic_write
    atomic_write(path,value)


def accept(store,item,settings,preferences,entry,keys):
    entry['seen']=entry.get('seen',0)+1
    if not search_settings.relevant(item['name'],settings):
        entry['role_filtered']=entry.get('role_filtered',0)+1;return
    if item.get('published_at'):
        try:
            published=datetime.fromisoformat(item['published_at'].replace('Z','+00:00'))
            if published.tzinfo is None:published=published.replace(tzinfo=timezone.utc)
            if published<datetime.now(timezone.utc)-timedelta(days=settings['search_days']):
                entry['date_filtered']=entry.get('date_filtered',0)+1;return
        except ValueError:entry['unknown_dates']=entry.get('unknown_dates',0)+1
    else:entry['unknown_dates']=entry.get('unknown_dates',0)+1
    description=html_to_text(item['description'])
    if len(description)<100:raise CheckError('Описание слишком короткое для оценки.')
    job={'company':item['employer']['name'],'region':item['area']['name'],'description_text':description,
         'applicant_locations':item.get('applicant_locations')}
    if company_is_excluded(job,preferences):
        entry['company_filtered']=entry.get('company_filtered',0)+1;return
    if preferences:
        state,_=sources.external_location(job,preferences)
        if state in {'blocked','unconfirmed'}:
            entry['location_filtered']=entry.get('location_filtered',0)+1;return
    # Canonical URL covers tracking variants; exact cross-post content covers identical mirrors.
    duplicate=store.connection.execute('SELECT source,vacancy_id FROM jobs WHERE url=? OR '
        '(title=? AND lower(company)=lower(?) AND description_text=?) LIMIT 1',
        (item['alternate_url'],item['name'],job['company'],description)).fetchone()
    if duplicate:
        if duplicate['source']=='web' and duplicate['vacancy_id']==item['id']:
            store.save(item,source='web')
        entry['known']=entry.get('known',0)+1;return
    store.save(item,source='web')
    keys.append(('web',item['id']));entry['saved']=entry.get('saved',0)+1


def remember(store,url,origin,state,note):
    with store.connection:
        store.connection.execute('INSERT INTO discovered_links VALUES (?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET '
            'found_from=excluded.found_from,state=excluded.state,note=excluded.note,last_seen_at=excluded.last_seen_at',
            (url,origin,state,note,datetime.now(timezone.utc).isoformat()))


def web_input(path,settings):
    if not path.exists():return [],[], 'missing'
    data=json.loads(path.read_text(encoding='utf-8'))
    stamp=datetime.fromisoformat(data['generated_at'].replace('Z','+00:00'))
    if stamp.tzinfo is None:raise CheckError('Для времени веб-поиска нужен часовой пояс.')
    age=datetime.now(timezone.utc)-stamp
    if age>timedelta(hours=12) or age<timedelta(minutes=-5):return [],[],'stale'
    expected=hashlib.sha256(json.dumps(sources.web_plan(settings),ensure_ascii=False).encode()).hexdigest()
    if data.get('plan_hash')!=expected:return [],[],'settings_changed'
    queries=data.get('queries');links=data.get('candidates')
    if (not isinstance(queries,list) or len(queries)>6 or not isinstance(links,list) or len(links)>30
        or not all(isinstance(q,dict) and q.get('status') in {'ok','error','skipped'} and isinstance(q.get('q'),str) for q in queries)
        or not all(isinstance(v,dict) and isinstance(v.get('url'),str) for v in links)):
        raise CheckError('Некорректный результат веб-поиска.')
    if [q['q'] for q in queries]!=sources.web_plan(settings):return [],queries,'incomplete_plan'
    if queries and not any(q['status']=='ok' for q in queries):return links,queries,'failed'
    return links,queries,'partial' if any(q['status']!='ok' for q in queries) else 'fresh'


def coverage_warnings(report):
    """Actionable source failures; individual missing web links do not mean all search failed."""
    warnings=[]
    for entry in report.get('sources',[]):
        if entry.get('source')=='HH' and entry.get('status') in {'partial','error'}:
            warnings.append('HH проверен не полностью. '+entry.get('details',{}).get('error',
                'Не удалось прочитать вакансии через браузер; проверьте вход в HH и журнал.'))
        elif entry.get('source')=='Веб-поиск' and entry.get('status') not in {'fresh','checked'}:
            queries=[q for q in entry.get('queries') or [] if isinstance(q,dict)]
            done=sum(q.get('status')=='ok' for q in queries)
            first=next((q.get('error') for q in queries if q.get('status')=='error' and q.get('error')),'')
            if entry.get('status')=='failed':
                warnings.append(f'Веб-поиск не работает: выполнено {done} из {len(queries)} запросов. '+(first or 'Подробности — в отчёте об источниках.'))
            elif entry.get('status')=='partial':
                warnings.append((f'Веб-поиск выполнен частично: с ошибкой {len(queries)-done} из {len(queries)} запросов. '+(first or '')).strip())
            else:warnings.append('Веб-поиск выполнен не полностью; подробности — в отчёте об источниках.')
        elif entry.get('slug') and entry.get('status')=='error':
            warnings.append('Не удалось проверить доску компании: '+entry.get('source',''))
    return warnings


def collect(store,settings,preferences,*,config=None,client=None,include_hh=True,web_path=None,report_path=None,
            refresh_web=False,search_session=None,search_key=None,search_model=None):
    config=config or sources.load(ROOT/'profile');web_path=Path(web_path or WEB);report_path=Path(report_path or REPORT)
    report={'started_at':datetime.now(timezone.utc).isoformat(),'sources':[],'job_keys':[],
            'scope':'Проверены только перечисленные источники и запросы; полнота всего рынка не гарантируется.'}
    keys=[];store.connection.executescript(LINK_SCHEMA)
    if refresh_web and 'web' in config['enabled']:
        from web_search import refresh
        refresh(settings,web_path,session=search_session,key=search_key,model=search_model)
    links=[];queries=[];web_state='missing';direct=[];manifest={}
    if 'web' in config['enabled']:
        try:links,queries,web_state=web_input(web_path,settings)
        except (CheckError,OSError,ValueError,KeyError,TypeError):web_state='invalid_input'
        for lead in links[:config['max_web_links']]:
            try:
                url=sources.canonical_url(lead['url']);parts=urlsplit(url)
                if parts.hostname in {'hh.kz','hh.ru'} and re.fullmatch(r'/vacancy/\d+/?',parts.path):
                    remember(store,url,'web_search','hh_pending' if 'hh' in config['enabled'] else 'manual_link',
                             'Найдена ссылка HH; полное описание ещё не прочитано.')
            except CheckError:continue
    if 'hh' in config['enabled']:
        direct=[r[0] for r in store.connection.execute("SELECT url FROM discovered_links WHERE state IN ('hh_pending','hh_link') ORDER BY last_seen_at DESC LIMIT 30")]
    if include_hh and 'hh' in config['enabled']:
        import browser_check
        entry={'source':'HH','status':'started','limit':settings['scan_limit']}
        report['sources'].append(entry)
        try:
            code=browser_check.run(settings['scan_limit'],junior=settings['junior_experience'],reuse_session=True,
                expanded=True,pages=settings['pages_per_query'],preferences=preferences,settings=settings,vacancy_urls=direct)
            manifest=json.loads(browser_check.RUN_MANIFEST.read_text(encoding='utf-8'))
            keys.extend(('hh',identity) for identity in manifest['vacancy_ids'])
            entry.update(status='partial' if code else ('limited' if manifest['saved']>=settings['scan_limit'] else 'checked'),
                         details=manifest)
            for url in direct:
                identity=urlsplit(url).path.rstrip('/').split('/')[-1]
                if store.get(identity,'hh'):
                    remember(store,url,'web_search','hh_checked','Полное описание HH сохранено в базе.')
                elif identity in manifest.get('closed_ids',[]):
                    remember(store,url,'web_search','unavailable','HH сообщает, что вакансия закрыта или отсутствует.')
                elif identity in manifest.get('date_filtered_ids',[]):
                    remember(store,url,'web_search','date_filtered','Вакансия вне выбранного периода поиска.')
        except (CheckError,OSError,ValueError,KeyError):entry.update(status='error',error='Не удалось завершить HH; другие источники продолжаются.')
    owned=None
    if client is None:
        owned=requests.Session();client=sources.PublicClient(owned)
    try:
        if 'companies' in config['enabled']:
            for board in config['boards']:
                entry={'source':board['name'],'kind':board['kind'],'slug':board['slug'],'status':'started','seen':0}
                report['sources'].append(entry)
                try:
                    for item in sources.board_jobs(client,board):accept(store,item,settings,preferences,entry,keys)
                    entry['status']='checked'
                except (CheckError,requests.RequestException,KeyError,TypeError,ValueError):
                    entry.update(status='error',error='Доска не прочитана полностью. Сохранённые результаты не потеряны.')
        urls=[]
        if 'companies' in config['enabled']:
            urls.extend((url,'company_page') for url in config['career_pages'])
        if 'web' in config['enabled']:
            report['sources'].append({'source':'Веб-поиск','status':web_state,'queries':queries,
                'note':'Программа ищет через OpenRouter, затем проверяет полные первоисточники; сниппеты не заменяют описание вакансии.'})
            urls.extend((v['url'],'web_search') for v in links[:config['max_web_links']])
            if len(links)>config['max_web_links']:report['sources'][-1]['limited']=True
        visited=set()
        for url,origin in urls:
            entry={'source':url,'kind':origin,'status':'started','seen':0};report['sources'].append(entry)
            try:
                url=sources.canonical_url(url)
                if url in visited:entry['status']='duplicate_link';continue
                visited.add(url)
                host=urlsplit(url).hostname
                if host=='linkedin.com' or host.endswith('.linkedin.com'):
                    entry['status']='manual_link';remember(store,url,origin,'manual_link','LinkedIn не обходится ботом; нужен первоисточник компании.');continue
                if host in {'hh.kz','hh.ru'} or host.endswith(('.hh.kz','.hh.ru')):
                    saved=store.connection.execute('SELECT state,note FROM discovered_links WHERE url=?',(url,)).fetchone()
                    entry['status']=saved['state'] if saved else 'manual_link'
                    entry['note']=saved['note'] if saved else 'Ссылка HH для ручного просмотра.'
                    continue
                items=list(sources.page_jobs(client,url))
                if not items:
                    # A career index may expose links but no structured job body itself.
                    if origin=='company_page':
                        final,html=client.page(url);parsed=sources.StructuredPage();parsed.feed(html)
                        candidates=[]
                        for href in parsed.links:
                            target=urljoin(final,href)
                            if re.search(r'/careers/[^/?#]+/?$|/jobs/[^/?#]+|/vacanc',urlsplit(target).path,re.I):
                                try:target=sources.canonical_url(target)
                                except CheckError:continue
                                if urlsplit(target).hostname==urlsplit(final).hostname and target not in visited:
                                    candidates.append(target)
                        for target in list(dict.fromkeys(candidates))[:5]:
                            visited.add(target)
                            try:items.extend(sources.page_jobs(client,target))
                            except (CheckError,requests.RequestException):continue
                        entry['linked_pages_checked']=min(5,len(set(candidates)))
                        entry['linked_pages_limited']=len(set(candidates))>5
                    if not items:
                        entry['status']='needs_adapter';remember(store,url,origin,'needs_adapter','Нет полного JobPosting; сниппет не используется как вакансия.');continue
                for item in items:accept(store,item,settings,preferences,entry,keys)
                entry['status']='checked';remember(store,url,origin,'checked','Полное описание проверено.')
            except CheckError as exc:
                entry.update(status='unavailable' if 'не найдена в опубликованной' in str(exc) else 'error',error=str(exc))
                try:remember(store,url,origin,entry['status'],str(exc))
                except (CheckError,ValueError):pass
            except (requests.RequestException,ValueError,KeyError,TypeError):
                entry.update(status='error',error='Страница недоступна, запрещена или не разобрана.');
                try:remember(store,url,origin,'error','Не удалось прочитать полный первоисточник.')
                except (CheckError,ValueError):pass
    finally:
        if owned is not None:owned.close()
        report['job_keys']=[list(key) for key in dict.fromkeys(keys)]
        with store.connection:
            for source,identity in report['job_keys']:
                store.connection.execute('INSERT INTO discovery_backlog(source,vacancy_id) VALUES (?,?) ON CONFLICT DO NOTHING',(source,identity))
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        write_json(report_path,report)
        write_json(report_path.parent/'discovery_reports'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json'),report)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--plan',action='store_true');parser.add_argument('--collect',action='store_true')
    args=parser.parse_args(argv);settings=search_settings.load(ROOT/'profile')
    if args.plan:
        queries=sources.web_plan(settings)
        print(json.dumps({'queries':queries,'plan_hash':hashlib.sha256(json.dumps(queries,ensure_ascii=False).encode()).hexdigest(),
              'output':str(WEB),'max_candidates':30},ensure_ascii=False,indent=2));return 0
    if args.collect:
        with JobStore() as store:
            report=collect(store,settings,load_preferences(ROOT/'profile'),refresh_web=True)
            print(f"Новых вакансий из всех источников: {len(report['job_keys'])}. Отчёт: {REPORT}")
    return 0


if __name__=='__main__':raise SystemExit(main())
