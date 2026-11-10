"""Prepare the observed HH response page; submit only after exact interactive approval.

Observed 2026-10-22: response-link href opens a separate page without applying.
The on-page HH resume is reviewed separately from the recommended local DOCX.
"""
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlsplit, parse_qs

from common import CheckError
from browser_check import PROFILE_DIR, navigate, check_blocker, already_applied, read_job_snapshot, job_content_hash
from apply import SCHEMA, now

SUBMIT = '[data-qa="vacancy-response-submit-popup"]'
LETTER = '[data-qa="vacancy-response-popup-form-letter-input"]'
TOGGLE = '[data-qa="vacancy-response-letter-toggle"]'
SUCCESS_TIMEOUT_MS = 15000


def success_marker(page):
    # HH uses non-breaking spaces and also shows a delivery confirmation after redirecting.
    return page.get_by_text(re.compile(
        r'^\s*(?:Вы\s+(?:успешно\s+)?откликнулись|Отклик\s+(?:успешно\s+)?отправлен|Резюме\s+доставлено)[.!]?\s*$',
        re.I))


class AlreadyApplied(CheckError):
    pass


def response_url(base, href, vacancy_id):
    if not isinstance(href,str) or not href:
        raise CheckError('HH: отсутствует ссылка на форму.')
    url = urljoin(base, href)
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not (parts.hostname == 'hh.kz' or (parts.hostname or '').endswith('.hh.kz'))
            or parts.username or parts.password or parts.port not in (None, 443)
            or parts.path != '/applicant/vacancy_response'
            or parse_qs(parts.query).get('vacancyId') != [str(vacancy_id)]):
        raise CheckError('HH: адрес формы не совпадает с выбранной вакансией.')
    return url


def check_vacancy_url(url, vacancy_id):
    parts=urlsplit(url)
    if (parts.scheme!='https' or not (parts.hostname=='hh.kz' or (parts.hostname or '').endswith('.hh.kz'))
            or parts.username or parts.password or parts.port not in (None,443)
            or parts.path != f'/vacancy/{vacancy_id}'):
        raise CheckError('Разрешена только исходная HH-вакансия с тем же ID.')


def identity(packet):
    data = dict(packet)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
                          + Path(packet['letter_path']).read_bytes()
                          + Path(packet['local_cv_path']).read_bytes()).hexdigest()


def reserve(db, packet, fingerprint):
    db.executescript(SCHEMA)
    with db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT state FROM application_attempts WHERE target_url=?', (packet['url'],)).fetchone()
        if old and old[0] not in {'hh_prepared', 'hh_form_closed'}:
            raise CheckError('Для этой вакансии уже была попытка отправки; повтор заблокирован.')
        db.execute('''INSERT INTO application_attempts VALUES (?, ?, 'hh_prepared', ?)
                      ON CONFLICT(target_url) DO UPDATE SET packet_hash=excluded.packet_hash,
                      state='hh_prepared',updated_at=excluded.updated_at''',
                   (packet['url'], fingerprint, now()))


def set_state(db, url, state):
    with db:
        db.execute('UPDATE application_attempts SET state=?,updated_at=? WHERE target_url=?', (state, now(), url))


def begin_submit(db, packet, fingerprint):
    if identity(packet) != fingerprint:
        raise CheckError('Письмо или CV изменились после просмотра. Отправка отменена.')
    with db:
        changed = db.execute("""UPDATE application_attempts SET state='submit_started',updated_at=?
                                WHERE target_url=? AND packet_hash=? AND state='hh_prepared'""",
                             (now(), packet['url'], fingerprint)).rowcount
        if changed != 1:
            raise CheckError('Отправка уже начиналась или черновик изменён; повтор запрещён.')


def prepare_page(page, packet):
    check_vacancy_url(packet['url'],packet['vacancy_id'])
    navigate(page, packet['url'], 'выбранную вакансию')
    check_blocker(page)
    if already_applied(page):
        raise AlreadyApplied('HH сообщает о прежнем отклике; повтор запрещён.')
    if packet.get('job_content_hash'):
        _,current=read_job_snapshot(page,packet['vacancy_id'],packet['url'])
        if job_content_hash(current)!=packet['job_content_hash']:
            raise CheckError('Описание или условия вакансии изменились после оценки; нужна новая оценка до заполнения формы.')
    links = page.locator('a[data-qa="vacancy-response-link-top"]')
    try:
        links.first.wait_for(state='visible', timeout=15000)
    except Exception:
        if already_applied(page):
            raise AlreadyApplied('HH сообщает о прежнем отклике; повтор запрещён.') from None
        raise
    hrefs = {response_url(page.url, links.nth(i).get_attribute('href'), packet['vacancy_id'])
             for i in range(links.count()) if links.nth(i).is_visible()}
    if len(hrefs) != 1:
        raise CheckError('Нет однозначной ссылки на отдельную форму. Кнопка отклика не нажималась.')
    # Navigate the observed link; do not invoke vacancy-card click handlers that may apply immediately.
    navigate(page, hrefs.pop(), 'отдельную форму отклика')
    check_blocker(page)
    response_url(page.url, page.url, packet['vacancy_id'])
    submit = page.locator(SUBMIT)
    submit.wait_for(state='visible', timeout=15000)
    warning = page.locator('[data-qa="hidden-resume-warning"]')
    if any(warning.nth(i).is_visible() for i in range(warning.count())):
        raise CheckError('HH требует изменить видимость резюме. Настройки не изменены, отправки нет.')
    resumes = page.locator('[data-qa="resume-title"]')
    titles = [resumes.nth(i).inner_text().strip() for i in range(resumes.count()) if resumes.nth(i).is_visible()]
    if len(titles) != 1 or not titles[0]:
        raise CheckError('HH: выбранное резюме не определено однозначно; нужен ручной просмотр.')
    letter = page.locator(LETTER)
    if not letter.count() or not letter.is_visible():
        page.locator(TOGGLE).click()
    letter.wait_for(state='visible', timeout=10000)
    form = submit.locator('xpath=ancestor::form[1]')
    extras = form.locator('input:not([type="hidden"]), textarea, select')
    for i in range(extras.count()):
        field = extras.nth(i)
        if field.is_visible() and field.get_attribute('data-qa') != 'vacancy-response-popup-form-letter-input':
            raise CheckError('В форме есть дополнительные вопросы. Ответы не выдумываются; отправка остановлена.')
    text = Path(packet['letter_path']).read_text(encoding='utf-8')
    if not text.strip():
        raise CheckError('Письмо пустое.')
    letter.fill(text)
    if letter.input_value() != text:
        raise CheckError('Письмо заполнено неверно; отправка запрещена.')
    return titles[0]


def run(packet, db, *, allow_send=False, prompt=input, profile_dir=PROFILE_DIR, confirmation_channel=None, result_channel=None):
    from playwright.sync_api import sync_playwright
    output = Path(packet['output_dir']); output.mkdir(parents=True, exist_ok=True)
    fingerprint = identity(packet)
    # With a UI channel the console is never asked: a hidden console prompt would hang the web flow.
    interactive=bool(sys.stdin is not None and sys.stdin.isatty()) and confirmation_channel is None
    if allow_send and not interactive and confirmation_channel is None:
        raise CheckError('Реальная отправка требует интерактивной консоли.')
    reserve(db, packet, fingerprint)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile_dir), headless=False,
                   viewport={'width':1360,'height':900})
        try:
            page = context.new_page()
            title = prepare_page(page, packet)
            # HH uses an existing account resume; a local file is not uploaded or silently substituted.
            review = {'hh_resume_title':title, 'local_cv':packet['local_cv_path'],
                      'vacancy_url':packet['url'], 'state':'hh_prepared', 'prepared_at':now()}
            (output/'form-review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            page.screenshot(path=str(output/'form-filled.png'),full_page=True)
            print(f"Форма заполнена: {packet['title']} | {packet['company']}\nРезюме в HH: {title}")
            print(f"Локальная рекомендация: {Path(packet['local_cv_path']).name}. Файл в HH не загружался.")
            print(f"Проверьте содержание резюме HH и письмо. Скриншот: {output/'form-filled.png'}")
            if not allow_send:
                if interactive:
                    try:prompt('Проверьте заполненную форму. Enter — закрыть без отправки: ')
                    except EOFError:pass
                set_state(db, packet['url'], 'hh_form_closed')
                return 'hh_prepared'
            print('После подтверждения существующее резюме HH и письмо получит указанный работодатель.')
            token = f"ОТПРАВИТЬ {packet['vacancy_id']}"
            try:
                answer=(confirmation_channel(review,token) if confirmation_channel is not None else
                        prompt(f'Один отклик, только после проверки формы. Введите {token}: ')).strip()
            except EOFError:answer=''
            if answer != token:
                set_state(db, packet['url'], 'hh_form_closed')
                return 'cancelled_before_send'
            check_blocker(page)
            response_url(page.url,page.url,packet['vacancy_id'])
            if page.locator(LETTER).input_value() != Path(packet['letter_path']).read_text(encoding='utf-8'):
                raise CheckError('Письмо в форме изменилось; повторите просмотр до отправки.')
            if title not in page.locator('[data-qa="resume-title"]').all_inner_texts():
                raise CheckError('Резюме HH изменилось после просмотра; отправка отменена.')
            begin_submit(db,packet,fingerprint)
            page.locator(SUBMIT).click(timeout=10000)
            # Success wording is deliberately narrow. Unknown UI never triggers a second click.
            success = success_marker(page)
            try:
                success.first.wait_for(state='visible',timeout=SUCCESS_TIMEOUT_MS)
                state='confirmed_by_site'
            except Exception:
                state='submitted_unverified'
            if state!='confirmed_by_site':
                print('Подтверждение пока не найдено. Если сайт показал капчу, пройдите её вручную в этом окне. Не нажимайте отправку повторно.')
            if state!='confirmed_by_site' and interactive:
                try:prompt('Проверьте результат в браузере. Enter — проверить ещё раз и закрыть: ')
                except EOFError:pass
            elif state!='confirmed_by_site' and result_channel is not None:
                # Keep the window open: the user may need to pass a captcha before HH shows the result.
                result_channel({**review,'state':'submitted_unverified'})
            if state!='confirmed_by_site':
                try:page.wait_for_load_state('load',timeout=5000)
                except Exception:pass
            if state!='confirmed_by_site' and any(success.nth(i).is_visible() for i in range(success.count())):
                state='confirmed_by_site'
            set_state(db,packet['url'],state)
            page.screenshot(path=str(output/'after-submit.png'),full_page=True)
            if state=='confirmed_by_site':
                with db:
                    db.execute("UPDATE jobs SET status='applied' WHERE source='hh' AND vacancy_id=?",(packet['vacancy_id'],))
            else:
                print('Подтверждение сайта не найдено. Проверьте страницу, повторная отправка заблокирована.')
            return state
        except AlreadyApplied as exc:
            set_state(db,packet['url'],'already_applied_on_hh')
            with db:
                db.execute("UPDATE jobs SET status='applied' WHERE source='hh' AND vacancy_id=?",(packet['vacancy_id'],))
            print(str(exc))
            return 'already_applied_on_hh'
        except Exception:
            if 'page' in locals() and not page.is_closed():
                page.screenshot(path=str(output/'form-stopped.png'),full_page=True)
            raise
        finally:
            context.close()
