"""One run: browser search -> grounded assessment -> letter -> HH form -> optional user approval."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys

import requests
from dotenv import load_dotenv
import assess
import prepare
from common import CheckError
from preferences import load_preferences, location_fit, company_is_excluded
from relevance import title_is_relevant
from storage import JobStore, DB_PATH
import search_settings
from run_lock import one_run,RunBusy
import work_queue

ROOT=Path(__file__).resolve().parent
LAST=ROOT/'data'/'last_hunt_run.json'
LAST_PACKET=ROOT/'data'/'last_hunt_packet.json'


class BudgetSession:
    """One conservative reserve across both assessment and letter calls, including failures."""
    def __init__(self,session,budget):
        self.session=session;self.budget=budget;self.reserved=0.;self.calls=0;self.cost=0.;self.unknown=0
        self.succeeded=0;self.failed=0

    def post(self,url,**kwargs):
        payload=kwargs['json']
        if url!='https://openrouter.ai/api/v1/chat/completions':
            raise CheckError('Неожиданный адрес модели.')
        cap=payload.get('provider',{}).get('max_price')
        if cap not in (assess.PRICE_CAP,assess.AUDIT_PRICE_CAP):
            raise CheckError('Не заданы согласованные ограничения цены модели.')
        size=len(json.dumps(payload,ensure_ascii=False).encode('utf-8'))+512
        reserve=(size*cap['prompt']+payload['max_tokens']*cap['completion'])/1_000_000
        if any(tool.get('type')=='openrouter:web_search' for tool in payload.get('tools',[])):
            reserve+=.02  # Conservative allowance for one bounded Exa search, up to five results.
        if self.reserved+reserve>self.budget:
            raise CheckError('Достигнут общий бюджет запуска; новый запрос не отправлен.')
        self.reserved+=reserve;self.calls+=1
        try:response=self.session.post(url,**kwargs)
        except requests.RequestException:
            self.failed+=1  # no answer: the provider reports no usage; the reserve already covers it (A17)
            raise
        if getattr(response,'status_code',200)!=200:
            self.failed+=1;return response
        self.succeeded+=1
        try:
            value=response.json().get('usage',{}).get('cost')
            if type(value) in (int,float) and math.isfinite(value) and value>=0:self.cost+=value
            else:self.unknown+=1
        except (ValueError,TypeError,AttributeError):self.unknown+=1
        return response

    def summary(self):
        return {'calls':self.calls,'succeeded_calls':self.succeeded,'failed_calls':self.failed,'reserved_usd':self.reserved,
                'known_cost_usd':self.cost,'unknown_cost_calls':self.unknown}


def usage_text(usage):
    """One honest line: what was actually charged versus the up-front ceiling (A17)."""
    line=(f"Модель: {usage.get('calls',0)} запросов (успешных {usage.get('succeeded_calls',0)}, неудачных {usage.get('failed_calls',0)}); "
          f"списано по данным API {usage.get('known_cost_usd',0):.4f} USD")
    if usage.get('unknown_cost_calls'):line+=f"; без данных о стоимости: {usage['unknown_cost_calls']}"
    return line+f"; резерв-потолок {usage.get('reserved_usd',0):.4f} USD (не списание)."



def opportunity_key(job):
    """Same employer, same role, same place: a repost or an aggregator copy, not a new opportunity."""
    import re
    def norm(value):return ' '.join(re.sub(r'[^\w]+',' ',str(value or '').casefold().replace('ё','е')).split())
    company=re.sub(r'^(?:тоо|ао|ип|too|llp|jsc|ооо)\s+|\s+(?:inc|ltd|llc|llp|gmbh)$','',norm(job.get('company')))
    return norm(job.get('title')),company,norm(job.get('region'))


def duplicate_copies(store,rows):
    """Later copies of an opportunity that is already in work (draft, application or the user's «Не подходит»).
    A copy that already has its own draft stays visible; an archived original lets a repost count as new.
    Two HH postings with the same title but different texts (e.g. two teams) are both kept; for aggregator
    links (web) the texts of one talent pool differ a lot, so title, company and place are enough."""
    from difflib import SequenceMatcher
    db=store.connection
    def table(name):return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()
    drafted=set(map(tuple,db.execute('SELECT source,vacancy_id FROM draft_queue').fetchall())) if table('draft_queue') else set()
    attempted={r[0] for r in db.execute('SELECT target_url FROM application_attempts')} if table('application_attempts') else set()
    groups={}
    for row in sorted(rows,key=lambda r:(r['first_seen_at'],r['source'],r['vacancy_id'])):
        if row['status']!='archived':groups.setdefault(opportunity_key(dict(row)),[]).append(row)
    copies=set()
    for group in groups.values():
        for i,row in enumerate(group):
            if (row['source'],row['vacancy_id']) in drafted or row['status'] in {'applied','skipped'}:continue
            for earlier in group[:i]:
                in_work=((earlier['source'],earlier['vacancy_id']) in drafted or earlier['status'] in {'applied','skipped'}
                         or earlier['url'] in attempted)
                similar=row['source']=='web' or SequenceMatcher(None,earlier['description_text'] or '',
                                                                   row['description_text'] or '').ratio()>=.6
                if in_work and similar:copies.add((row['source'],row['vacancy_id']));break
    return copies


def eligible(store,preferences,ids=None,limit=3,settings=None):
    rows=store.connection.execute("SELECT * FROM jobs WHERE source IN ('hh','web') ORDER BY last_seen_at DESC,vacancy_id DESC").fetchall()
    copies=duplicate_copies(store,rows)
    result=[]
    for row in rows:
        job=dict(row)
        if (job['source'],job['vacancy_id']) in copies:continue
        if ids is not None and not (('source' in job and (job['source'],job['vacancy_id']) in ids) or
                                    (job['source']=='hh' and job['vacancy_id'] in ids)):continue
        if job['status'] in {'applied','skipped','archived'} or company_is_excluded(job,preferences):continue
        if not (search_settings.relevant(job['title'],settings) if settings else title_is_relevant(job['title'])):continue
        if preferences and location_fit(job,preferences)[0] in {'blocked','unconfirmed'}:continue
        if preferences and job['source']=='web':
            from sources import external_location
            if external_location(job,preferences)[0] in {'blocked','unconfirmed'}:continue
        # Any begun submission blocks future paid preparation too.
        exists=store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_attempts'").fetchone()
        if exists:
            attempt=store.connection.execute('SELECT state FROM application_attempts WHERE target_url=?',(job['url'],)).fetchone()
            if attempt and attempt[0] not in {'hh_prepared','hh_form_closed'}:continue
        result.append(job)
        if len(result)>=limit:break
    return result


def current_assessments(store,jobs,profiles,model):
    results=[]
    day=datetime.now(timezone.utc).date().isoformat()
    for job in jobs:
        key=assess.cache_key(assess.build_payload(job,profiles,model,assessment_date=day))
        row=store.connection.execute('SELECT result_json FROM assessments WHERE source=? AND vacancy_id=? AND cache_key=?',
                                     (job['source'],job['vacancy_id'],key)).fetchone()
        if row:
            result=assess.apply_safeguards(assess.validate_result(json.loads(row[0]),job,profiles),job)
            if result['decision']!='skip':results.append((job,result))
    results.sort(key=lambda item:(item[1]['decision']!='apply',-sum(m['coverage']=='full' for m in item[1]['matches'])))
    return results


def prepare_one(store,jobs,*,model,profile_dir,output,session,run,budget,preferences,key='',settings=None):
    packets=prepare_batch(store,jobs,model=model,profile_dir=profile_dir,output=output,session=session,
                          run=run,budget=budget,preferences=preferences,key=key,limit=1,settings=settings)
    return packets[0] if packets else None


def is_outage(exc):
    """Nothing was paid and nothing is wrong with the job: the service or the run budget stopped it."""
    import model_api
    return isinstance(exc,model_api.ModelUnavailable) or 'бюджет' in str(exc).casefold()


def check_profile(profile_dir):
    """Before any paid call: the CVs letters need exist and contain usable facts (added 17.11.2026)."""
    try:
        english=assess.load_profiles(profile_dir)
        russian={variant:assess.read_cv(profile_dir/name) for variant,name in prepare.RU_FILES.items()}
        prepare.fact_catalog(english,'en');prepare.fact_catalog(russian,'ru')
        # Without it every geography passes silently; a run must not start like that.
        if load_preferences(profile_dir) is None:raise CheckError('Нет файла profile/preferences.json с географией поиска.')
        if not assess.candidate_profile():raise CheckError('Заполните profile/candidate.md: цель поиска и от чьего лица писать письма.')
    except CheckError as exc:
        raise CheckError(f'{exc} Проверьте папку profile/; образец и требования к CV — examples/profile.') from None


def prepare_batch(store,jobs,*,model,profile_dir,output,session,run,budget,preferences,key='',limit=3,settings=None):
    profiles=assess.load_profiles(profile_dir)
    for job in jobs:
        if run:work_queue.stage(store,job,'assessing')
        try:
            result_code=assess.assess_jobs(store,profiles,model,limit=1,run=run,budget=budget,key=key,session=session,
                job_keys=[(job['source'],job['vacancy_id'])],preferences=preferences,settings=settings,
                retry_rejected=bool(job.get('_retry')))
        except CheckError as exc:
            if run and is_outage(exc):
                # Back to the queue, next scheduled run retries; later jobs are not sent to a failing service.
                work_queue.stage(store,job,'new','Повтор в следующем запуске: '+str(exc))
                print(f"Оценка {job['vacancy_id']} отложена до следующего запуска: {exc}")
                return []
            if run:work_queue.stage(store,job,'assessment_failed',str(exc))
            print(f"Оценка {job['vacancy_id']} остановлена: {exc}")
            continue
        if run:work_queue.stage(store,job,'assessment_failed' if result_code else 'new',
            'Оценка отклонена; результат не используется. Повтор возможен по вашему действию.' if result_code else '')
        if run and not result_code:
            identity=assess.cache_key(assess.build_payload(job,profiles,model,assessment_date=datetime.now(timezone.utc).date().isoformat()))
            cached=store.connection.execute('SELECT result_json FROM assessments WHERE source=? AND vacancy_id=? AND cache_key=?',
                                           (job['source'],job['vacancy_id'],identity)).fetchone()
            if cached and json.loads(cached[0]).get('decision')=='skip':work_queue.stage(store,job,'processed')
    candidates=current_assessments(store,jobs,profiles,model)
    russian={variant:assess.read_cv(profile_dir/name) for variant,name in prepare.RU_FILES.items()}
    packets=[]
    for job,result in candidates[:limit]:
        job={**job,'_letter_language':letter_language(job)}
        facts=profiles if job['_letter_language']=='en' else russian
        destination=output if limit==1 else output/(job['source']+'-'+job['vacancy_id'])
        try:
            if run:work_queue.stage(store,job,'writing')
            packet=make_packet(job,result,facts,model,profile_dir=profile_dir,output=destination,
                               session=session,run=run,budget=budget,preferences=preferences,key=key,settings=settings)
        except CheckError as exc:
            if run:work_queue.stage(store,job,'letter_failed',str(exc))
            print(f"Письмо {job['vacancy_id']} не подготовлено: {exc}")
            if is_outage(exc):break  # do not send the remaining letters to a failing service
            continue
        if packet is not None:
            packets.append(packet)
            from draft_queue import register
            register(store.connection,destination/'application.json')
            work_queue.stage(store,job,'ready')
    return packets


def letter_language(job):
    """Letter and CV follow the vacancy's language, for HH too. Saved jobs split cleanly: English
    descriptions have ~0% Cyrillic letters, Russian ones (even full of tech terms) 76% and more."""
    import re
    text=str(job.get('description_text') or '')
    cyrillic=len(re.findall('[А-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі]',text));latin=len(re.findall('[A-Za-z]',text))
    return 'en' if latin and cyrillic/(cyrillic+latin)<0.3 else 'ru'


def make_packet(job,result,russian,model,*,profile_dir,output,session,run,budget,preferences,key,settings=None):
    writer=prepare.prepare
    if settings and settings.get('letter_mode')=='personalized':
        from personalized import prepare_letter
        writer=prepare_letter
    folder=writer(job,russian,result,model,profile_dir=profile_dir,output_dir=ROOT/'drafts',
                           run=run,budget=budget,key=key,session=session,preferences=preferences)
    if folder is None or not run:return None
    selection=json.loads((folder/'selection.json').read_text(encoding='utf-8'))['selection']
    files=assess.CV_FILES if job.get('_letter_language')=='en' else prepare.RU_FILES
    packet={'source':job['source'],'vacancy_id':job['vacancy_id'],'title':job['title'],'company':job['company'],
            'url':job['url'],'local_cv_path':str((profile_dir/files[selection['cv_variant']]).resolve()),
            'letter_path':str((folder/'letter.txt').resolve()),'output_dir':str(output.resolve()),
            'decision':result['decision'],'gaps':result['gaps'],'assessment_summary':result['summary']}
    packet['letter_mode']='personalized' if (folder/'personalized.json').exists() else 'literal'
    if (folder/'research.json').exists():
        research=json.loads((folder/'research.json').read_text(encoding='utf-8'))
        packet['company_research']={k:research[k] for k in ('status','note','retrieved_at')}
        packet['company_research']['sources']=[{k:v[k] for k in ('url','title','retrieved_at')} for v in research['sources']]
    from browser_check import job_content_hash
    packet['job_content_hash']=job_content_hash(job)
    save_report(packet,output/'application.json')
    return packet


def save_report(report,path):
    from common import write_json
    write_json(path,report)


def refresh_candidates(store,jobs):
    """Isolate a dead or unreadable source so other vacancies can proceed."""
    import browser_check
    import sources
    failed=set()
    hh_jobs=[j for j in jobs if j['source']=='hh']
    if hh_jobs:
        outcome=browser_check.refresh_saved(store,hh_jobs)
        if isinstance(outcome,dict):failed.update(tuple(k) for k in outcome.get('failed',[]))
    for job in [j for j in jobs if j['source']=='web']:
        key=(job['source'],job['vacancy_id']);work_queue.stage(store,job,'refreshing')
        try:
            with requests.Session() as session:
                matches=[v for v in sources.page_jobs(sources.PublicClient(session),job['url']) if v['id']==job['vacancy_id']]
            if not matches:raise CheckError('Полное описание больше не читается. Подготовка этой вакансии отложена.')
            store.save(matches[0],source='web');work_queue.stage(store,job,'new')
        except sources.JobUnavailable:
            with store.connection:store.connection.execute("UPDATE jobs SET status='archived',status_reason='closed' WHERE source=? AND vacancy_id=? AND status NOT IN ('applied','skipped')",key)
            work_queue.stage(store,job,'archived')
        except Exception:
            failed.add(key)
            work_queue.stage(store,job,'refresh_failed','Не удалось обновить полное описание внешней вакансии. Другие вакансии продолжают обрабатываться.')
    return failed


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Поиск, вызовы модели и подготовка HH-формы')
    parser.add_argument('--from-db',action='store_true',help='Использовать только уже собранные вакансии')
    parser.add_argument('--vacancy-id',help='Одна уже сохранённая вакансия HH')
    parser.add_argument('--rewrite-key',help='Новая версия письма для сохранённой вакансии source:id; старый файл сохраняется')
    parser.add_argument('--retry-key',help='Явно повторить неудачную подготовку source:id, используя сохранённую оценку')
    parser.add_argument('--limit',type=int,choices=range(1,11))
    parser.add_argument('--budget',type=float,help='Общий резерв USD для модели и письма')
    parser.add_argument('--no-form',action='store_true',help='Остановиться на локальном письме')
    parser.add_argument('--send',action='store_true',help='Разрешить вопрос об одной отправке; без ответа отправки нет')
    parser.add_argument('--resume',action='store_true',help='Открыть пакет последнего запуска без модели и нового поиска')
    parser.add_argument('--packet',type=Path,help='Сохранённый пакет для --resume')
    parser.add_argument('--queue',action='store_true',help='Подготовить до 10 писем в очередь без открытия форм')
    args=parser.parse_args(argv)
    try:settings=search_settings.load(ROOT/'profile')
    except CheckError as exc:parser.error(str(exc))
    if args.limit is None:args.limit=settings['vacancies_per_run']
    if args.budget is not None and (not math.isfinite(args.budget) or args.budget<=0):
        parser.error('Нужен конечный положительный бюджет.')
    if args.budget is None:args.budget=math.inf if settings['budget_usd'] is None else settings['budget_usd']
    if args.resume and (args.from_db or args.vacancy_id):parser.error('--resume используется отдельно от выбора вакансий.')
    if args.packet and not args.resume:parser.error('--packet требует --resume.')
    if args.queue and (args.resume or args.send):parser.error('--queue используется без --resume и --send.')
    if args.rewrite_key and (not args.run or not args.queue or args.resume or args.from_db or args.vacancy_id):
        parser.error('--rewrite-key требует --run --queue и используется отдельно от других способов выбора.')
    if args.retry_key and (not args.run or not args.queue or args.resume or args.from_db or args.vacancy_id or args.rewrite_key):
        parser.error('--retry-key требует --run --queue и отдельного выбора вакансии.')
    rewrite=None
    retry=None
    if args.rewrite_key or args.retry_key:
        parts=(args.rewrite_key or args.retry_key).split(':')
        if len(parts)!=2 or parts[0] not in {'hh','web'} or not parts[1].isascii() or not parts[1].isdigit():parser.error('Нужен ключ source:id.')
        if args.retry_key:retry=tuple(parts)
        else:rewrite=tuple(parts)
    if args.vacancy_id and not (args.vacancy_id.isascii() and args.vacancy_id.isdigit()):parser.error('Нужен числовой HH ID.')
    if args.send and (not args.run or args.no_form or not sys.stdin.isatty()):parser.error('--send требует --run и интерактивную консоль.')
    output=ROOT/'drafts'/'runs'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    report={'run_id':output.name,'report_path':str(output/'run.json'),
            'started_at':datetime.now(timezone.utc).isoformat(),'state':'started','application_sent':False}
    metered=None
    locks=ExitStack();acquired=False
    try:
        locks.enter_context(one_run(ROOT/'profile'/'hunt.lock'));acquired=True
        if args.run:save_report(report,ROOT/'data/active_run.json')
        with JobStore() as store, requests.Session() as raw:
            preferences=load_preferences(ROOT/'profile')
            if args.run:work_queue.recover_legacy(store)
            if retry and retry not in work_queue.retry_required(store):
                raise CheckError('Для этой вакансии нет неудачной подготовки. Обновите список.')
            if retry and not eligible(store,preferences,[retry],1,settings):
                raise CheckError('Вакансия отправлена, исключена или не проходит текущие правила; повтор не запускался.')
            if args.resume:
                old={'packet_path':str(args.packet)} if args.packet else json.loads(LAST_PACKET.read_text(encoding='utf-8'))
                if not Path(old['packet_path']).resolve().is_relative_to((ROOT/'drafts').resolve()):
                    raise CheckError('Пакет должен находиться в папке drafts проекта.')
                packet=json.loads(Path(old['packet_path']).read_text(encoding='utf-8'))
                if not eligible(store,preferences,[(packet['source'],packet['vacancy_id'])],1,settings):raise CheckError('Вакансия отправлена, исключена или больше не подходит по правилам.')
                if not args.run:
                    print(f"Последний пакет: {old['packet_path']}\nОтправки нет.");return 0
                output.mkdir(parents=True,exist_ok=True)
                packet={**packet,'output_dir':str(output.resolve())}
                save_report(packet,output/'application.json')
                report.update(packet_path=str(output/'application.json'))
            else:
                ids=[rewrite or retry] if rewrite or retry else ([args.vacancy_id] if args.vacancy_id else None)
                if args.run and not args.from_db and not args.vacancy_id and not rewrite and not retry:
                    import discovery
                    check_profile(ROOT/'profile')
                    print('1/4. Поиск: HH, сайты компаний и результаты веб-поиска.')
                    load_dotenv(ROOT/'.env',override=False,encoding='utf-8-sig',interpolate=False)
                    model=os.getenv('OPENROUTER_MODEL',assess.DEFAULT_MODEL).strip() or assess.DEFAULT_MODEL
                    metered=BudgetSession(raw,args.budget)
                    coverage=discovery.collect(store,settings,preferences,refresh_web=True,search_session=metered,
                        search_key=os.getenv('OPENROUTER_API_KEY','').strip(),search_model=model)
                    report['coverage']=coverage
                    report['coverage_warnings']=discovery.coverage_warnings(coverage)
                    discovery.print_summary(coverage)
                    if args.queue:
                        recovered=discovery.recover_unassessed(store,settings,preferences)
                        if recovered:print(f'Добавлены в очередь сохранённые вакансии без прежней оценки: {recovered}.')
                    fresh_keys=[tuple(k) for k in coverage['job_keys']]
                    ids=list(dict.fromkeys(fresh_keys+discovery.pending_keys(store)))
                    from draft_queue import pending
                    ready={(p['source'],p['vacancy_id']) for _,p in pending(store,preferences,settings)}
                    ids=[k for k in ids if k not in ready]
                    blocked=work_queue.retry_required(store)
                    ids=[k for k in ids if k not in blocked]
                    report['backlog_keys']=[list(k) for k in ids if k not in fresh_keys]
                    if report['backlog_keys']:print(f"Продолжается обработка ранее найденных, ещё не оценённых вакансий: {len(report['backlog_keys'])}.")
                jobs=eligible(store,preferences,ids,100000 if args.queue and args.from_db else args.limit,settings)
                if args.queue and args.from_db:
                    from draft_queue import pending
                    ready={(p['source'],p['vacancy_id']) for _,p in pending(store,preferences,settings)}
                    blocked=work_queue.retry_required(store)
                    jobs=[j for j in jobs if (j['source'],j['vacancy_id']) not in ready|blocked][:args.limit]
                if args.run and jobs and (args.from_db or args.vacancy_id or rewrite or retry or report.get('backlog_keys')):
                    print('1/4. Проверка актуальности и прежних откликов по сохранённым вакансиям.')
                    to_refresh=jobs if args.from_db or args.vacancy_id or rewrite or retry else [j for j in jobs if [j['source'],j['vacancy_id']] in report['backlog_keys']]
                    failed=refresh_candidates(store,to_refresh)
                    if failed:report.setdefault('coverage_warnings',[]).append('Некоторые сохранённые вакансии не удалось обновить; они доступны для явного повтора.')
                    jobs=eligible(store,preferences,[(j['source'],j['vacancy_id']) for j in jobs if (j['source'],j['vacancy_id']) not in failed],args.limit,settings)
                if not jobs:
                    print('Новых подходящих вакансий в выбранном наборе нет; модель не вызывается.')
                    report['work_issues']=work_queue.issues(store,preferences,settings)
                    report['state']='needs_attention' if report['work_issues'] else ('search_incomplete' if report.get('coverage_warnings') else 'no_eligible_jobs');return 0
                if rewrite or retry:jobs=[{**job,'_letter_revision':output.name,'_retry':bool(retry)} for job in jobs]
                report['vacancy_ids']=[j['vacancy_id'] for j in jobs]
                print('2/4. Сравнение с CV. Исключённые компании и неподходящая география уже отсеяны.')
                load_dotenv(ROOT/'.env',override=False,encoding='utf-8-sig',interpolate=False)
                model=os.getenv('OPENROUTER_MODEL',assess.DEFAULT_MODEL).strip() or assess.DEFAULT_MODEL
                if metered is None:metered=BudgetSession(raw,args.budget)
                output.mkdir(parents=True,exist_ok=True)
                options=dict(model=model,profile_dir=ROOT/'profile',output=output,session=metered,
                             run=args.run,budget=args.budget,preferences=preferences,
                             key=os.getenv('OPENROUTER_API_KEY','').strip(),settings=settings)
                if args.queue:
                    packets=prepare_batch(store,jobs,limit=args.limit,**options)
                    report['packet_paths']=[str(Path(p['output_dir'])/'application.json') for p in packets]
                    packet=packets[0] if packets else None
                else:
                    packet=prepare_one(store,jobs,**options)
                if args.run:report['work_issues']=work_queue.issues(store,preferences,settings)
                if packet is None:
                    report['state']='preview' if not args.run else ('needs_attention' if report.get('work_issues') else 'no_recommended_draft')
                    print('Пакет не создан: нет актуальной рекомендации или требуется --run.');return 0
                report['packet_path']=str(Path(packet['output_dir'])/'application.json')
                print(f"3/4. Письмо готово: {packet['letter_path']}")
            report['state']='draft_ready'
            save_report(report,LAST)
            save_report({'packet_path':report['packet_path']},LAST_PACKET)
            if args.no_form or args.queue:return 0
            if packet['source']!='hh':
                report['state']='external_ready'
                print('Внешнее письмо готово. Откройте пакет в меню: ссылка работодателя и отправка вручную; универсальный отправщик не подключён.')
                return 0
            print('4/4. Подготовка одной HH-формы. Кнопка отправки требует отдельного подтверждения.')
            import hh_response
            report['state']=hh_response.run(packet,store.connection,allow_send=args.send)
            report['application_sent']=report['state']=='confirmed_by_site'
            messages={
                'confirmed_by_site':'Отклик доставлен — HH подтвердил получение.',
                'already_applied_on_hh':'HH сообщает о прежнем отклике; новый не отправлен.',
                'cancelled_before_send':'Отправка отменена. Черновик сохранён.',
                'hh_prepared':'Форма проверена и закрыта без отправки.',
                'submitted_unverified':'Результат неизвестен. Повтор заблокирован; проверьте историю и сайт.',
            }
            print(messages.get(report['state'],'Результат сохранён: '+report['state']))
            return 0
    except RunBusy:
        print('Программа занята. Обработка ещё не начиналась; текущий отчёт не изменён.')
        return 75
    except (CheckError,OSError,ValueError,KeyError) as exc:
        report['state']='stopped';report['error']=str(exc)
        print(f'[ОСТАНОВЛЕНО] {exc}');return 1
    except KeyboardInterrupt:
        report['state']='interrupted';print('Остановлено. Сохранённые результаты остаются; неизвестная отправка не повторяется.');return 1
    except Exception as exc:
        import traceback
        report['error_type']=type(exc).__name__
        report['error_locations']=[f'{Path(f.filename).name}:{f.lineno}' for f in traceback.extract_tb(exc.__traceback__)
                                   if Path(f.filename).parent==ROOT]
        report['state']='stopped';report['error']='Ошибка браузера или локальных данных; полный журнал скрыт для защиты личных данных.'
        print('[ОСТАНОВЛЕНО] Не удалось завершить этап; сохранённые результаты остаются.');return 1
    finally:
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        report['model_usage']=metered.summary() if metered else {'calls':0,'succeeded_calls':0,'failed_calls':0,'reserved_usd':0,
                                                                  'known_cost_usd':0,'unknown_cost_calls':0}
        try:
            if args.run and acquired:
                save_report(report,output/'run.json');save_report(report,LAST)
                save_report(report,ROOT/'data/active_run.json')
                if args.queue:
                    import telegram_events
                    telegram_events.run_finished(ROOT/'data/jobs.sqlite',report,root=ROOT)
        finally:locks.close()
        print(usage_text(report['model_usage']))


if __name__=='__main__':raise SystemExit(main())
