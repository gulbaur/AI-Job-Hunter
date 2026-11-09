"""Bounded HH browser collection: visible session, new vacancies and existing SQLite."""

import argparse
import hashlib
from datetime import datetime, timezone,timedelta
import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from common import CheckError, html_to_text
from storage import JobStore
from relevance import title_is_relevant
from preferences import company_is_excluded, location_fit
from search_settings import DEFAULTS as SEARCH_DEFAULTS, title_allowed, relevant

PROFILE_DIR = Path(__file__).resolve().parent / "profile" / "hh_browser_check"
RUN_MANIFEST = Path(__file__).resolve().parent / "data" / "last_browser_run.json"
SEARCH_PARAMS = {
    "area": "40",  # Kazakhstan; host hh.kz alone is not a region filter.
    "text": '("machine learning" OR "ML Engineer" OR "AI Engineer" OR LLM '
            'OR "Computer Vision" OR "машинное обучение")',
    "search_period": "30", "order_by": "publication_time", "items_on_page": "20",
    "search_field": "name",
}


def search_url(*, junior: bool = False, text: str | None = None, page: int = 0, search_days: int = 30) -> str:
    params = dict(SEARCH_PARAMS)
    params['search_period']=str(search_days)
    if text is not None:
        params["text"] = text
    if page:
        params["page"] = page
    if junior:
        # HH experience buckets are a search filter, not proof of suitability.
        params["experience"] = ["noExperience", "between1And3"]
    return "https://hh.kz/search/vacancy?" + urlencode(params, doseq=True)


ENTRY_TERMS = '(junior OR trainee OR intern OR стажер OR стажёр OR начинающий OR младший)'
ROLE_TERMS = ('("machine learning" OR "ML Engineer" OR "AI Engineer" OR LLM OR NLP '
              'OR "Computer Vision" OR "Data Scientist" OR "машинного обучения" '
              'OR "машинное обучение" OR "искусственного интеллекта")')
SEARCH_PROFILES = (
    ("Junior и стажировки", f"{ENTRY_TERMS} AND {ROLE_TERMS}"),
    ("ML и Data Science", '("Data Scientist" OR "ML Engineer" OR "machine learning" '
                            'OR MLOps OR "машинного обучения" OR "машинное обучение")'),
    ("Applied AI, LLM и Computer Vision", '("AI Engineer" OR LLM OR NLP OR "Computer Vision" '
                                          'OR "инженер ИИ" OR "искусственного интеллекта")'),
)


def search_plan(*, junior: bool, expanded: bool, pages: int, settings=None) -> list[tuple[str, str]]:
    if pages not in (1, 2):
        raise ValueError("Можно просмотреть только 1 или 2 страницы на запрос.")
    profiles = SEARCH_PROFILES if expanded else (("AI/ML", SEARCH_PARAMS["text"]),)
    if settings and expanded:
        profiles=tuple(profile for key,profile in zip(('junior','ml_ds','applied_ai'),SEARCH_PROFILES)
                       if key in settings['role_groups'])
        profiles=profiles+tuple(('Роль: '+role,'"'+role.replace('"','')+'"') for role in settings['custom_roles'])
    return [(label, search_url(junior=junior, text=text, page=page,
                              search_days=(settings or SEARCH_DEFAULTS)['search_days']))
            for label, text in profiles for page in range(pages)]


SEARCH_URL = search_url()
TITLE_LINKS = 'a[data-qa="serp-item__title"], a[data-qa="vacancy-serp__vacancy-title"]'
DESCRIPTION = '[data-qa="vacancy-description"]'
TIMEOUT_MS = 30_000


class BrowserStop(CheckError):
    pass


class VacancyReadError(BrowserStop):
    """One malformed vacancy; authentication/captcha still stop the whole run."""
    pass


class VacancyClosed(VacancyReadError):
    pass


def already_applied(page) -> bool:
    markers = page.get_by_text(re.compile(r'^Вы\s+(?:уже\s+)?откликнулись[.!]?$', re.I))
    return any(markers.nth(i).is_visible() for i in range(markers.count()))


def job_content_hash(job):
    values={key:' '.join(str(job.get(key) or '').split()) for key in ('title','company','region','description_text')}
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def read_job_snapshot(page, vacancy_id, url):
    vacancy=read_vacancy(page,vacancy_id,url)
    job={'title':vacancy['name'],'company':vacancy['employer']['name'],
         'region':vacancy['area']['name'],'description_text':html_to_text(vacancy['description'])}
    return vacancy,job


def refresh_saved(store, jobs):
    """Check current source and previous HH responses before spending on saved jobs."""
    import work_queue
    outcome={'failed':[]};remaining=list(jobs)
    try:
        with sync_playwright() as p:
            context=p.chromium.launch_persistent_context(str(PROFILE_DIR),headless=False,
                        viewport={'width':1360,'height':900})
            try:
                page=context.new_page()
                for job in jobs:
                    work_queue.stage(store,job,'refreshing')
                    try:
                        navigate(page,job['url'],'актуальность сохранённой вакансии')
                        check_blocker(page)
                        vacancy,_=read_job_snapshot(page,job['vacancy_id'],job['url'])
                        save_browser_vacancy(store,vacancy)
                        if already_applied(page):
                            with store.connection:
                                store.connection.execute("UPDATE jobs SET status='applied' WHERE source='hh' AND vacancy_id=?",(job['vacancy_id'],))
                        work_queue.stage(store,job,'new')
                    except VacancyClosed:
                        with store.connection:store.connection.execute("UPDATE jobs SET status='archived',status_reason='closed' WHERE source='hh' AND vacancy_id=? AND status NOT IN ('applied','skipped')",(job['vacancy_id'],))
                        work_queue.stage(store,job,'archived')
                    except VacancyReadError as exc:
                        outcome['failed'].append(('hh',job['vacancy_id']))
                        work_queue.stage(store,job,'refresh_failed',str(exc))
                    remaining.remove(job)
            finally:context.close()
    except (BrowserStop,PlaywrightError):
        for job in remaining:
            outcome['failed'].append(('hh',job['vacancy_id']))
            work_queue.stage(store,job,'refresh_failed','HH временно недоступен или требуется ручной вход/капча. Другие источники продолжаются.')
    return outcome


def first_text(page, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            item = locator.nth(index)
            if item.is_visible():
                text = " ".join(item.inner_text().split())
                if text:
                    return text
    return ""


def check_blocker(page) -> None:
    if page.is_closed():
        raise BrowserStop("Окно браузера закрыто до завершения проверки.")
    path = urlsplit(page.url).path.lower()
    captcha = page.locator(
        'iframe[src*="captcha"], [data-qa*="captcha"], input[name*="captcha"], #captcha'
    )
    if "captcha" in path or any(captcha.nth(i).is_visible() for i in range(captcha.count())):
        raise BrowserStop("HH показывает капчу. Сбор остановлен; пройдите её вручную при следующем запуске.")
    if "/account/login" in path or "/applicant/login" in path:
        raise BrowserStop("HH перенаправил на вход: ручная авторизация не завершена или сессия истекла.")
    blocked = page.get_by_text(re.compile(
        r"подтвердите, что вы не робот|доступ временно ограничен|доступ заблокирован",
        re.IGNORECASE,
    ))
    if any(blocked.nth(i).is_visible() for i in range(blocked.count())):
        raise BrowserStop("HH показывает проверку робота или блокировку доступа; автоматическое продолжение остановлено.")


def navigate(page, url: str, stage: str) -> None:
    try:
        result = page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    except PlaywrightTimeout:
        raise BrowserStop(f"Таймаут: не удалось открыть {stage} за 30 секунд.") from None
    except PlaywrightError as exc:
        # Emit a known error code only, never a browser log, URL parameters or session data.
        if page.is_closed() or "Target page, context or browser has been closed" in str(exc):
            raise BrowserStop(
                f"Не удалось открыть {stage}: управляемая вкладка или окно браузера закрыто."
            ) from None
        if "interrupted by another navigation" in str(exc):
            raise BrowserStop(f"Переход к странице «{stage}» прерван другой навигацией в этой вкладке.") from None
        code = re.search(r"net::(ERR_[A-Z_]+)", str(exc))
        suffix = f" ({code.group(1)})" if code else ""
        raise BrowserStop(f"Не удалось открыть {stage}{suffix}.") from None
    if result is not None and result.status in (404, 410):
        raise VacancyClosed(f"HH вернул HTTP {result.status}: страница недоступна.")
    if result is not None and result.status >= 400:
        raise BrowserStop(f"HH вернул HTTP {result.status}: не удалось открыть {stage}.")


def wait_for_content(page, selector: str, label: str) -> None:
    check_blocker(page)
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeout:
        check_blocker(page)
        raise VacancyReadError(
            f"Не найден элемент «{label}» ({selector}); страница не загрузилась или интерфейс HH изменился."
        ) from None
    check_blocker(page)


def unique_links(hrefs: list[str | None], base_url: str, limit: int) -> list[tuple[str, str]]:
    result = []
    seen: set[str] = set()
    for href in hrefs:
        url = urlsplit(urljoin(base_url, href or ""))
        host = (url.hostname or "").lower()
        if url.scheme != "https" or not any(host == h or host.endswith("." + h) for h in ("hh.kz", "hh.ru")):
            continue
        match = re.fullmatch(r"/vacancy/(\d+)/?", url.path)
        if match and match.group(1) not in seen:
            vacancy_id = match.group(1)
            seen.add(vacancy_id)
            result.append((vacancy_id, f"https://hh.kz/vacancy/{vacancy_id}"))
            if len(result) >= limit:
                break
    return result


def job_postings(page) -> list[dict]:
    # Public JobPosting metadata only; no app state, cookies or session storage.
    postings = []
    for raw in page.locator('script[type="application/ld+json"]').all_text_contents():
        try:
            pending = [json.loads(raw)]
        except (ValueError, TypeError):
            continue
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                kind = node.get("@type", [])
                if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                    postings.append(node)
                if isinstance(node.get("@graph"), list):
                    pending.extend(node["@graph"])
    return postings


def publication_date(page) -> str:
    for node in job_postings(page):
        value = node.get("datePosted")
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T[0-9:+.Z-]+)?", value):
            return value
    return ""  # Unknown; do not substitute the collection date for publication.


def vacancy_region(page) -> str:
    visible = first_text(page, (
        '[data-qa="vacancy-view-location"]', '[data-qa="vacancy-view-raw-address"]',
        '[data-qa="vacancy-view-address"]', '[data-qa="vacancy-region"]',
    ))
    if visible:
        return visible
    for node in job_postings(page):
        locations = node.get("jobLocation", [])
        if isinstance(locations, dict):
            locations = [locations]
        if not isinstance(locations, list):
            continue
        for location in locations:
            if not isinstance(location, dict):
                continue
            address = location.get("address", {})
            if isinstance(address, dict):
                city = address.get("addressLocality")
                if isinstance(city, str) and city.strip():
                    return city.strip()
    return ""  # Unknown is preferable to inventing Almaty/remote from the search filter.


def read_vacancy(page, vacancy_id: str, url: str) -> dict:
    wait_for_content(page, DESCRIPTION, "полное описание вакансии")
    title = first_text(page, ('[data-qa="vacancy-title"]',))
    company = first_text(page, ('[data-qa="vacancy-company-name"]',))
    region = vacancy_region(page)
    for name, value in (("название", title), ("компания", company)):
        if not value:
            raise VacancyReadError(f"В вакансии {vacancy_id} не найдено поле «{name}».")
    description = page.locator(DESCRIPTION).first.inner_html()
    if not html_to_text(description).strip():
        raise VacancyReadError(f"У вакансии {vacancy_id} пустое полное описание; запись не сохранена.")
    experience = first_text(page, ('[data-qa="vacancy-experience"]',))
    return {
        "id": vacancy_id, "name": title, "employer": {"name": company},
        "area": {"name": region}, "alternate_url": url, "description": description,
        "published_at": publication_date(page),
        "experience": {"name": experience} if experience else None,
    }


def save_browser_vacancy(store: JobStore, vacancy: dict) -> bool:
    # Browser and API share HH identity. Missing browser metadata must not erase known data.
    old = store.get(vacancy["id"], source="hh")
    if old:
        vacancy = dict(vacancy)
        if not vacancy["area"].get("name") and old["region"]:
            vacancy["area"] = {"name": old["region"], "id": old["region_id"]}
        vacancy["published_at"] = vacancy.get("published_at") or old["published_at"]
        if not vacancy.get("experience"):
            vacancy["experience"] = {"id": old["experience_id"], "name": old["experience_name"]}
        if not vacancy.get("salary") and old["salary_json"]:
            vacancy["salary"] = json.loads(old["salary_json"])
        if vacancy["area"].get("name") == old["region"]:
            vacancy["area"] = {**vacancy["area"], "id": old["region_id"]}
    return store.save(vacancy, source="hh")


def collect_links(detail,store,links,*,limit,refresh,counts,seen,preferences,settings):
    for vacancy_id,vacancy_url in links:
        if counts['saved']>=limit:return
        if vacancy_id in seen:continue
        seen.add(vacancy_id)
        if not refresh and store.get(vacancy_id) is not None:
            counts['known']+=1
            continue
        try:
            navigate(detail,vacancy_url,f'вакансию {vacancy_id}')
            vacancy=read_vacancy(detail,vacancy_id,vacancy_url)
        except VacancyClosed:
            counts.setdefault('closed_ids',[]).append(vacancy_id)
            continue
        except VacancyReadError as exc:
            counts['errors']+=1
            print(f'[ПРОПУЩЕНО] {exc}')
            continue
        if not relevant(vacancy['name'],settings or SEARCH_DEFAULTS):
            counts['skipped']+=1
            continue
        if vacancy.get('published_at'):
            try:
                published=datetime.fromisoformat(vacancy['published_at'].replace('Z','+00:00'))
                if published.tzinfo is None:published=published.replace(tzinfo=timezone.utc)
                if published<datetime.now(timezone.utc)-timedelta(days=(settings or SEARCH_DEFAULTS)['search_days']):
                    counts.setdefault('date_filtered_ids',[]).append(vacancy_id)
                    continue
            except ValueError:pass
        is_new=save_browser_vacancy(store,vacancy)
        if already_applied(detail):
            with store.connection:store.connection.execute("UPDATE jobs SET status='applied' WHERE source='hh' AND vacancy_id=?",(vacancy_id,))
            counts['already_applied']=counts.get('already_applied',0)+1
            continue
        if preferences:
            job=dict(store.get(vacancy_id));excluded=company_is_excluded(job,preferences)
            if excluded or location_fit(job,preferences)[0] in {'blocked','unconfirmed'}:
                counts['filtered']=counts.get('filtered',0)+1
                if excluded:
                    with store.connection:store.connection.execute("UPDATE jobs SET status='filtered',status_reason='company' WHERE source='hh' AND vacancy_id=? AND status NOT IN ('applied','skipped')",(vacancy_id,))
                continue
        counts['saved']+=1;counts['added']+=int(is_new)
        counts.setdefault('vacancy_ids',[]).append(vacancy_id)
        print(f"{vacancy_id} | {vacancy['name']} | {vacancy['employer']['name']}")
        print(vacancy_url)
        print(f"Полное описание сохранено: {len(html_to_text(vacancy['description']))} символов.",flush=True)


def collect_searches(page, detail, store: JobStore, *, limit: int, junior: bool,
                     expanded: bool, pages: int, refresh: bool, counts: dict,
                     preferences: dict | None = None, settings=None) -> None:
    """Keep progress if a later search hits an authentication/blocking condition."""
    seen = set()
    exhausted = set()
    plan = search_plan(junior=junior, expanded=expanded, pages=pages, settings=settings)
    for label, url in plan:
        if counts["saved"] >= limit:
            break
        if label in exhausted:
            continue
        counts["searches"] += 1
        query_record={'label':label,'url':url,'status':'started','cards':0}
        counts.setdefault('queries',[]).append(query_record)
        print(f"Поиск: {label} ({counts['searches']}/{len(plan)})", flush=True)
        navigate(page, url, "поиск AI/ML-вакансий в Казахстане")
        check_blocker(page)
        empty = page.get_by_text(re.compile(r"по вашему запросу ничего не найдено|ничего не найдено", re.I))
        if any(empty.nth(i).is_visible() for i in range(empty.count())):
            print("По этому запросу больше нет результатов.")
            exhausted.add(label)
            query_record['status']='empty'
            continue
        header = first_text(page, ('[data-qa="vacancies-search-header"]',))
        found = re.search(r"Найден[аоы]?\s+([\d\s]+)\s+ваканси", header, re.I)
        total = int("".join(found[1].split())) if found else None
        query_record['reported_total']=total
        page_index = int(parse_qs(urlsplit(url).query).get("page", ["0"])[0])
        if total is not None and page_index * 20 >= total:
            exhausted.add(label)
            query_record['status']='past_end'
            continue
        try:
            wait_for_content(page, TITLE_LINKS, "ссылки на вакансии в выдаче")
        except VacancyReadError as exc:
            counts["errors"] += 1
            print(f"[ПРОПУЩЕНА СТРАНИЦА] {exc}")
            query_record['status']='error'
            continue
        candidates = page.locator(TITLE_LINKS).evaluate_all(
            "links => links.map(link => ({href: link.getAttribute('href'), title: link.innerText}))"
        )
        query_record.update(status='checked',cards=len(candidates))
        hrefs = []
        for candidate in candidates:
            if relevant(candidate['title'],settings or SEARCH_DEFAULTS):
                hrefs.append(candidate["href"])
            else:
                counts["skipped"] += 1
        collect_links(detail,store,unique_links(hrefs,page.url,20),limit=limit,refresh=refresh,
                      counts=counts,seen=seen,preferences=preferences,settings=settings)
        if counts['saved']>=limit:
            query_record['status']='limited'
            return
        if total is not None and (page_index + 1) * 20 >= total:
            exhausted.add(label)


def run(limit: int, *, junior: bool = False, reuse_session: bool = False,
        expanded: bool = False, pages: int = 1, refresh: bool = False,
        preferences: dict | None = None, settings=None, vacancy_urls=None) -> int:
    counts = dict(saved=0, added=0, errors=0, skipped=0, known=0, searches=0, vacancy_ids=[])
    started_at = datetime.now(timezone.utc).isoformat()
    context = None
    try:
        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR, headless=False,
                    viewport={"width": 1360, "height": 900}, accept_downloads=False,
                    timeout=TIMEOUT_MS,
                )
                context.set_default_timeout(15_000)
                page = context.pages[0] if context.pages else context.new_page()
                navigate(page, "https://hh.kz/", "главную страницу HH")
                print("Открыт видимый браузер с отдельным профилем проекта.", flush=True)
                if reuse_session:
                    print("Используется сохранённая сессия. При запросе входа или капче сбор остановится.", flush=True)
                else:
                    input("Войдите в HH самостоятельно. Затем вернитесь в консоль и нажмите Enter; Ctrl+C — остановить: ")
                # Login can replace or close its original tab. Keep the authenticated
                # context but open search in a fresh tab, without inspecting session data.
                page = context.new_page()
                detail = context.new_page()
                with JobStore() as store:
                    direct=unique_links(vacancy_urls or [],'https://hh.kz/',30)
                    if direct:
                        print(f'Проверка прямых HH-находок веб-поиска: {len(direct)}.',flush=True)
                        collect_links(detail,store,direct,limit=limit,refresh=refresh,counts=counts,
                                      seen=set(),preferences=preferences,settings=settings)
                    collect_searches(page, detail, store, limit=limit, junior=junior, expanded=expanded,
                                     pages=pages, refresh=refresh, counts=counts, preferences=preferences, settings=settings)
            finally:
                if context is not None:
                    context.close()
    except BrowserStop as exc:
        counts["errors"] += 1
        counts['error']=str(exc)
        print(f"[ОСТАНОВЛЕНО] {exc}")
    except (EOFError, KeyboardInterrupt):
        counts["errors"] += 1
        print("[ОСТАНОВЛЕНО] Нет подтверждения ручного входа или проверка прервана пользователем.")
    except PlaywrightError as exc:
        counts["errors"] += 1
        if isinstance(exc,PlaywrightTimeout):
            message='Браузер HH не запустился вовремя. Сбор остановлен; следующая плановая попытка — в следующем окне.'
        elif "Executable doesn't exist" in str(exc):
            message='Chromium не установлен. Выполните: .\\.venv\\Scripts\\python.exe -m playwright install chromium'
        elif "ProcessSingleton" in str(exc) or "user data directory is already in use" in str(exc):
            message='Профиль браузера занят; закройте предыдущее окно этой проверки.'
        elif "Target page, context or browser has been closed" in str(exc):
            message='Управляемое окно браузера закрыто или связь с ним потеряна после ручного входа.'
        else:
            message='Playwright потерял доступ к браузеру или не смог запустить его; журналы сессии не выводятся.'
        counts['error']=message
        counts['error_type']=type(exc).__name__
        print('[ОСТАНОВЛЕНО] '+message)
    except (sqlite3.Error, OSError, CheckError, ValueError):
        counts["errors"] += 1
        print("[ОСТАНОВЛЕНО] Не удалось сохранить данные вакансии в существующую SQLite.")
    print(f"{'Частичная проверка' if counts['errors'] else 'Проверка завершена'}: сохранено {counts['saved']}; "
          f"новых {counts['added']}; обновлено {counts['saved'] - counts['added']}; "
          f"уже в базе {counts['known']}; смежных карточек {counts['skipped']}; ошибок {counts['errors']}.")
    if counts.get('filtered'):
        print(f"Отсеяно по компании/географии до лимита: {counts['filtered']}.")
    if not counts["saved"] and not counts["errors"]:
        print("Новых вакансий в пределах заданных запросов и страниц не найдено. База и статусы сохранены.")
    try:
        RUN_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        temporary = RUN_MANIFEST.with_suffix(".tmp")
        temporary.write_text(json.dumps({**counts, "source": "hh", "started_at": started_at,
                                        "finished_at": datetime.now(timezone.utc).isoformat(),
                                        "complete": counts["errors"] == 0}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(RUN_MANIFEST)
    except OSError:
        counts["errors"] += 1
        print("[ОШИБКА] Не удалось записать список вакансий последнего сбора.")
    return 1 if counts["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="HH через видимый браузер, ручной вход и SQLite; без HH API.")
    parser.add_argument("--limit", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--junior", action="store_true", help="Фильтр HH: без опыта или 1–3 года")
    parser.add_argument("--reuse-session", action="store_true", help="Без паузы для входа; использовать существующую сессию")
    parser.add_argument("--expanded", action="store_true", help="Три запроса: Junior/стажировки, ML/DS, Applied AI/CV")
    parser.add_argument("--pages", type=int, choices=(1, 2), default=1, help="Страниц на запрос (максимум 2)")
    parser.add_argument("--refresh", action="store_true", help="Обновлять известные вакансии; по умолчанию только новые")
    args = parser.parse_args(argv)
    return run(args.limit, junior=args.junior, reuse_session=args.reuse_session,
               expanded=args.expanded, pages=args.pages, refresh=args.refresh)


if __name__ == "__main__":
    raise SystemExit(main())
