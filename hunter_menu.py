"""Small local console UI. All external actions are chosen by the user here."""
from datetime import datetime
from pathlib import Path
import sqlite3
import subprocess
import sys
import json
import webbrowser

import draft_queue
import hunt
import search_settings
from common import CheckError
from preferences import load_preferences
from storage import JobStore

ROOT = Path(__file__).resolve().parent
STATES = {
    'reported_by_user': 'Отправлено вручную — отмечено пользователем',
    'confirmed_by_site': 'Доставлено — подтверждено сайтом',
    'already_applied_on_hh': 'Ранее откликались в HH',
    'submitted_unverified': 'Результат неизвестен — повтор запрещён',
    'submit_started': 'Отправка начиналась — повтор запрещён',
    'hh_prepared': 'Форма подготовлена, отправка не начиналась',
    'hh_form_closed': 'Форма закрыта без отправки',
}


def ask(prompt, read=input):
    try:
        return read(prompt).strip()
    except EOFError:
        raise KeyboardInterrupt from None


def show_packet(packet):
    print(f"\n{packet['title']}\n{packet['company']}\n{packet['url']}")
    print('Оценка: '+{'apply': 'рассмотреть', 'stretch': 'есть заметные пробелы'}.get(packet['decision'],packet['decision']))
    print(packet.get('assessment_summary', ''))
    for gap in packet.get('gaps', []):
        print('  Пробел: '+gap['reason'])
    print(f"Рекомендованный файл: {Path(packet['local_cv_path']).name}")
    print('В HH отправляется резюме из аккаунта; его название будет показано в браузере.' if packet['source']=='hh'
          else 'Внешний сайт: подготовлены ссылка, CV и письмо. Отправка на этой площадке выполняется вручную.')
    print('\nПИСЬМО\n'+Path(packet['letter_path']).read_text(encoding='utf-8'))


def pending_entries(unread_only=False):
    with JobStore() as store:
        draft_queue.import_existing(store.connection, ROOT)
        return draft_queue.pending(store, load_preferences(ROOT/'profile'),search_settings.load(ROOT/'profile'),unread_only=unread_only)


def review(read=input, quiet=False, include_viewed=False):
    entries = pending_entries(unread_only=not include_viewed)
    if not entries:
        if not quiet:print('Новых непросмотренных писем нет. Enter в главном меню запустит поиск; «п» откроет отложенные письма.')
        return
    for number, (path, packet) in enumerate(entries, 1):
        # Recheck eligibility after each interaction: a scheduled run can change the queue.
        current = {p: data for p, data in pending_entries()}
        if path not in current:continue
        packet = current[path]
        while True:
            print(f'\nОТКЛИК {number} ИЗ {len(entries)}')
            show_packet(packet)
            with JobStore() as store:
                draft_queue.mark_reviewed(store.connection,path,packet)
            print(('о — открыть форму HH; перед отправкой потребуется отдельное подтверждение\n'
                  'ф — только проверить форму, без отправки\n'
                  if packet['source']=='hh' else 'о — открыть сайт компании (отправка вручную)\nс — отметить уже отправленный вручную отклик\n')+
                  'р — изменить письмо | и — исключить вакансию\nEnter — отложить и перейти дальше | 0 — главное меню')
            action = ask('Действие: ', read).casefold()
            if action=='0':
                return
            if not action:
                break
            if action in ({'о','ф'} if packet['source']=='hh' else {'о','с'}):
                if packet['source']!='hh':
                    from sources import canonical_url
                    if action=='о':
                        webbrowser.open(canonical_url(packet['url']))
                        print('Открыта ссылка. Программа не вводила данные и не отправляла отклик.')
                    elif ask(f"Если уже отправили вручную, введите ОТПРАВЛЕНО {packet['vacancy_id']}: ",read)==f"ОТПРАВЛЕНО {packet['vacancy_id']}":
                        from hh_response import SCHEMA
                        from apply import now
                        with hunt.one_run(ROOT/'profile/hunt.lock'),JobStore() as store:
                            store.connection.executescript(SCHEMA)
                            with store.connection:
                                store.connection.execute("INSERT INTO application_attempts VALUES (?,?,?,?) ON CONFLICT(target_url) DO NOTHING",
                                    (packet['url'],'manual-user-confirmation','reported_by_user',now()))
                                store.connection.execute("UPDATE jobs SET status='applied' WHERE source=? AND vacancy_id=?",(packet['source'],packet['vacancy_id']))
                        print('Отмечено по вашему подтверждению; это не автоматическая проверка доставки сайтом.')
                    break
                args=['--run','--resume','--packet',str(path)]
                if action=='о':args.append('--send')
                code=hunt.main(args)
                print('Проверка завершена.' if code==0 else 'Действие остановлено; смотрите причину выше. Черновики сохранены.')
                break
            if action=='р':
                subprocess.Popen(['notepad.exe',str(Path(packet['letter_path']).resolve())])
                ask('Отредактируйте письмо в Блокноте, сохраните файл и нажмите здесь Enter: ',read)
            elif action=='и':
                with hunt.one_run(ROOT/'profile'/'hunt.lock'), JobStore() as store:
                    with store.connection:
                        store.connection.execute("UPDATE jobs SET status='skipped',status_reason='user' WHERE source=? AND vacancy_id=?",(packet['source'],packet['vacancy_id']))
                print('Вакансия исключена. Письмо сохранено.'); break
            else:
                print('Выберите действие из подсказки; Enter откладывает письмо без отправки.')
    print('\nВсе новые письма просмотрены. Отложенные доступны через «п» в главном меню.')


def continue_work(read=input, force_search=False, from_db=False):
    if not force_search and pending_entries(unread_only=True):
        review(read)
        return
    budget=search_settings.load(ROOT/'profile')['budget_usd']
    reserve='без денежного лимита' if budget is None else f'до {budget:.4f} USD'
    print(f'Поиск → оценка → письма → просмотр. OpenRouter: {reserve}.')
    print('Этапы проходят подряд. Формы отклика автоматически не открываются.')
    args=['--run','--queue']
    if from_db:args.append('--from-db')
    code=hunt.main(args)
    print('Подготовка завершена.' if code==0 else 'Подготовка остановлена; причина указана выше.')
    review(read)


def history():
    from hh_response import SCHEMA
    with JobStore() as store:
        store.connection.executescript(SCHEMA)
        rows=store.connection.execute('''SELECT a.target_url,a.state,a.updated_at,j.title,j.company
            FROM application_attempts a LEFT JOIN jobs j ON j.url=a.target_url
            ORDER BY a.updated_at DESC LIMIT 30''').fetchall()
    if not rows:
        print('История отправок пока пустая.'); return
    for row in rows:
        print(f"\n{row['company'] or ''} | {row['title'] or row['target_url']}")
        print(STATES.get(row['state'], 'Требуется проверка: '+row['state']))
        try:
            stamp=datetime.fromisoformat(row['updated_at'].replace('Z','+00:00')).astimezone().strftime('%d.%m.%Y %H:%M %z')
        except ValueError:
            stamp=row['updated_at']
        print(stamp+' | '+row['target_url'])


def login(read=input):
    from playwright.sync_api import sync_playwright
    from browser_check import PROFILE_DIR, navigate, check_blocker
    with hunt.one_run(ROOT/'profile'/'hunt.lock'), sync_playwright() as p:
        context=p.chromium.launch_persistent_context(str(PROFILE_DIR),headless=False)
        try:
            page=context.new_page()
            navigate(page,'https://hh.kz/','HH для ручного входа')
            ask('Войдите в HH самостоятельно в открытом окне. После завершения нажмите Enter здесь: ',read)
            check_blocker(page)
            print('Окно закрывается; сессия сохранена. Если при поиске снова потребуется вход, повторите этот пункт.')
        finally:
            context.close()


def settings_menu(read=input):
    settings=search_settings.load(ROOT/'profile')
    print('\nНАСТРОЙКИ ПОИСКА. Enter сохраняет текущее значение.')
    settings['role_groups']=list(search_settings.GROUPS)
    print('Один запуск ищет всё вместе: Junior/стажировки, ML/Data Science, Applied AI/LLM/NLP/CV.')
    print('Исключения в названии: '+('; '.join(settings['excluded_title_words']) or 'нет'))
    value=ask('Исключить слова через ; (например MLOps; Data Scientist). Минус — очистить: ',read)
    if value:settings['excluded_title_words']=[] if value=='-' else [v.strip() for v in value.split(';') if v.strip()]
    for key,label in [('search_days','Вакансии за 1, 3, 7 или 30 дней'),
                      ('vacancies_per_run','До скольких новых вакансий готовить за запуск: от 1 до 10'),
                      ('pages_per_query','Страниц на каждый поисковый запрос: 1 или 2')]:
        value=ask(f'{label} [сейчас {settings[key]}]: ',read)
        if value:settings[key]=int(value)
    value=ask(f"Ограничить фильтр HH опытом без опыта/1–3 года? да/нет [сейчас {'да' if settings['junior_experience'] else 'нет'}]: ",read)
    if value:
        if value.casefold() not in {'да','нет'}:raise CheckError('Введите да или нет.')
        settings['junior_experience']=value.casefold()=='да'
    value=ask(f"Резерв OpenRouter в USD; минус — без денежного лимита [сейчас {settings['budget_usd']}]: ",read)
    if value:settings['budget_usd']=None if value=='-' else float(value.replace(',','.'))
    print('Дополнительные роли: '+('; '.join(settings['custom_roles']) or 'нет'))
    value=ask('Добавочные названия ролей через ; (минус — очистить): ',read)
    if value:settings['custom_roles']=[] if value=='-' else [v.strip() for v in value.split(';') if v.strip()]
    value=ask(f"Лимит новых вакансий HH перед отбором (1–30) [сейчас {settings['scan_limit']}]: ",read)
    if value:settings['scan_limit']=int(value)
    with hunt.one_run(ROOT/'profile'/'hunt.lock'):
        search_settings.save(ROOT/'profile',settings)
    print('Настройки сохранены. Они действуют при следующих запусках, в том числе по расписанию.')
    print('География и исключённые компании хранятся отдельно в profile/preferences.json. Разрешение отправки не изменено.')


def source_menu(read=input):
    import sources
    from urllib.parse import urlsplit
    config=sources.load(ROOT/'profile')
    print('\nИСТОЧНИКИ: 1 — HH, 2 — сайты компаний, 3 — независимый веб-поиск OpenRouter')
    print('Включены: '+', '.join(config['enabled']))
    value=ask('Включённые источники через запятую; Enter — оставить: ',read)
    if value:
        options={'1':'hh','2':'companies','3':'web'};chosen=[v.strip() for v in value.split(',')]
        if not all(v in options for v in chosen):raise CheckError('Выберите 1, 2, 3.')
        config['enabled']=list(dict.fromkeys(options[v] for v in chosen))
    for n,board in enumerate(config['boards'],1):print(f"{n}. {board['name']} ({board['kind']}/{board['slug']})")
    value=ask('Номер доски для удаления; Enter — ничего не удалять: ',read)
    if value:
        if not value.isdigit() or not 1<=int(value)<=len(config['boards']):raise CheckError('Нет такой доски.')
        config['boards']=[b for n,b in enumerate(config['boards'],1) if n!=int(value)]
    value=ask('Добавить ссылку карьерной страницы / доски компании; Enter — пропустить: ',read)
    if value:
        url=sources.canonical_url(value);parts=urlsplit(url);path=[p for p in parts.path.split('/') if p]
        kind=next((kind for host,kind in [('jobs.ashbyhq.com','ashby'),('boards.greenhouse.io','greenhouse'),
                    ('job-boards.greenhouse.io','greenhouse'),('jobs.lever.co','lever')] if parts.hostname==host),None)
        if kind and path:
            name=ask('Название компании: ',read) or path[0]
            config['boards'].append({'kind':kind,'slug':path[0],'name':name})
        elif url not in config['career_pages']:config['career_pages'].append(url)
    with hunt.one_run(ROOT/'profile/hunt.lock'):sources.save(ROOT/'profile',config)
    print('Источники сохранены. Веб-поиск выполняется программой через OpenRouter.')


def coverage_report():
    import discovery
    if not discovery.REPORT.exists():print('Сначала запустите поиск: отчёт ещё не создан.');return
    report=json.loads(discovery.REPORT.read_text(encoding='utf-8'))
    print('\nОХВАТ ПОИСКА / '+report['started_at'])
    print(report['scope'])
    labels={'checked':'проверено','limited':'достигнут лимит','partial':'частично','failed':'не работает','error':'ошибка',
            'fresh':'свежий поиск','missing':'веб-поиск ещё не выполнен','stale':'веб-поиск устарел',
            'settings_changed':'правила изменились, нужен новый веб-поиск','invalid_input':'ошибка данных веб-поиска',
            'incomplete_plan':'список выполненных запросов не совпадает с планом',
            'manual_link':'LinkedIn: ссылка для ручного просмотра','needs_adapter':'пока нет читателя полного описания',
            'duplicate_link':'повтор ссылки','hh_link':'ссылка HH — отдельный источник',
            'unavailable':'вакансия не найдена в опубликованной доске'}
    fields={'seen':'карточек просмотрено','saved':'новых сохранено','known':'уже в базе',
            'role_filtered':'отсеяно по роли','date_filtered':'за пределами давности',
            'location_filtered':'география не подходит/не подтверждена','company_filtered':'компания исключена',
            'error':'причина','note':'примечание'}
    for entry in report['sources']:
        print(f"\n{entry['source']}: {labels.get(entry['status'],entry['status'])}")
        for key in ('seen','saved','known','role_filtered','date_filtered','location_filtered','company_filtered','error','note'):
            if key in entry:print(f'  {fields[key]}: {entry[key]}')
        if 'details' in entry:
            d=entry['details'];print(f"  Запросов/страниц HH: {d.get('searches',0)}; сохранено {d.get('saved',0)}; известных {d.get('known',0)}; ошибок {d.get('errors',0)}")
            from urllib.parse import parse_qs,urlsplit
            for query in d.get('queries',[]):
                page=int(parse_qs(urlsplit(query['url']).query).get('page',['0'])[0])+1
                print(f"  {labels.get(query['status'],query['status'])} | {query['label']}, стр. {page}: {query['cards']} карточек")
        for query in entry.get('queries',[]):print(f"  {query['status']} | {query['q']}")


def more_menu(read=input):
    while True:
        print('\nНАСТРОЙКИ И ИСТОРИЯ\n'
              '1 — что искать\n2 — где искать\n'
              '3 — общий отчёт\n0 — назад')
        action=ask('Ваш выбор: ',read)
        if action=='0':return
        if action=='1':settings_menu(read)
        elif action=='2':source_menu(read)
        elif action=='3':
            history()
            coverage_report()
        else:print('Введите номер пункта меню.')


def main(read=input):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    print('ЛИЧНЫЙ JOB HUNTER\nАлматы: офис или удалёнка. Другие места: только удалёнка.')
    print('По расписанию поиск и письма готовятся автоматически. Здесь — просмотр и подтверждение.')
    startup=True
    try:
        while True:
            try:
                if startup:
                    startup=False
                    review(read,quiet=True)
                print('\nEnter / 1 — продолжить: новые письма или поиск → письма → просмотр\n'
                      'п — вернуться к отложенным письмам\n2 — настройки и история\n0 — выйти')
                action=ask('Ваш выбор: ',read)
                if action=='0':return 0
                if action in {'','1'}:continue_work(read)
                elif action.casefold()=='п':review(read,include_viewed=True)
                elif action.casefold()=='вход':login(read)
                elif action=='2':more_menu(read)
                else:print('Введите номер пункта меню.')
            except CheckError as exc:
                print(f'Действие остановлено: {exc}')
                if any(word in str(exc).casefold() for word in ('вход','войти','капч','сесси')):
                    print('Для ручного входа в HH введите «вход» в главном меню.')
            except (OSError,ValueError,KeyError,sqlite3.Error):
                print('Действие остановлено: не удалось прочитать данные, открыть редактор или завершить проверку. '
                      'Черновики сохранены: выберите «Продолжить».')
            except Exception:
                print('Связь с браузером прервана или действие завершилось ошибкой. '
                      'Не повторяйте отправку при неизвестном результате; проверьте «Настройки и история → Общий отчёт».')
    except KeyboardInterrupt:
        print('\nПрограмма закрыта. Черновики и результаты сохранены.')
        return 0


if __name__=='__main__':
    raise SystemExit(main())
