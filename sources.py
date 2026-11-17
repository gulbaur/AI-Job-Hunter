"""Public career boards and verified JobPosting pages. Read-only network access."""
from datetime import datetime, timezone, timedelta
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import requests
from common import CheckError,html_to_text

DEFAULTS={'enabled':['hh','companies','web'],
    'boards':[{'kind':'ashby','slug':'higgsfieldai','name':'Higgsfield AI'},
              {'kind':'ashby','slug':'cohere','name':'Cohere'},
              {'kind':'greenhouse','slug':'canonical','name':'Canonical'}],
    'career_pages':[], 'max_web_links':12}
KINDS={'ashby','greenhouse','lever'}


class JobUnavailable(CheckError):
    """A source explicitly reports 404/410 or an unlisted ATS vacancy."""
    pass


def validate(config):
    if not isinstance(config,dict) or set(config)-set(DEFAULTS):raise CheckError('Неизвестные настройки источников.')
    result={**DEFAULTS,**config}
    if not isinstance(result['enabled'],list) or not all(v in {'hh','companies','web'} for v in result['enabled']):
        raise CheckError('Источники: hh, companies, web.')
    if not isinstance(result['boards'],list) or len(result['boards'])>30:raise CheckError('Не больше 30 досок компаний.')
    for board in result['boards']:
        if (not isinstance(board,dict) or set(board)!={'kind','slug','name'} or board['kind'] not in KINDS
            or not isinstance(board['slug'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',board['slug'])
            or not isinstance(board['name'],str) or not board['name'].strip()):
            raise CheckError('Доска: kind=ashby/greenhouse/lever, slug и name.')
    if not isinstance(result['career_pages'],list) or len(result['career_pages'])>20:raise CheckError('Не больше 20 карьерных страниц.')
    for url in result['career_pages']:canonical_url(url)
    if type(result['max_web_links']) is not int or not 1<=result['max_web_links']<=30:raise CheckError('Лимит веб-ссылок: 1–30.')
    return result


def load(profile):
    path=Path(profile)/'sources.json'
    try:return validate(json.loads(path.read_text(encoding='utf-8'))) if path.exists() else validate({})
    except (OSError,ValueError):raise CheckError('Не удалось прочитать profile/sources.json.') from None


def save(profile,config):
    path=Path(profile)/'sources.json';path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(validate(config),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    from common import replace_file
    replace_file(temp,path)  # Windows: a reader may briefly hold the file


def canonical_url(value):
    if not isinstance(value,str) or len(value)>2000:raise CheckError('Некорректная ссылка.')
    try:
        p=urlsplit(value)
        if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):raise ValueError
    except ValueError:raise CheckError('Нужна публичная HTTPS-ссылка без учётных данных.') from None
    query=[(k,v) for k,v in parse_qsl(p.query) if not (k.casefold().startswith('utm_') or k in {'source','ref','gh_src','from'})]
    return urlunsplit(('https',p.hostname.lower(),p.path or '/',urlencode(sorted(query)),''))


def public_url(value):
    url=canonical_url(value);host=urlsplit(url).hostname
    try:
        addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):raise ValueError
    except (OSError,ValueError):raise CheckError('Адрес не является доступным публичным сайтом.') from None
    return url


class PublicClient:
    def __init__(self,session):self.session=session;self.robots={}

    def get(self,url):
        for _ in range(5):
            url=public_url(url)
            with self.session.get(url,headers={'User-Agent':'PersonalJobHunter/1.0'},timeout=(10,20),
                                  allow_redirects=False,stream=True) as response:
                if response.status_code in {301,302,303,307,308}:
                    from urllib.parse import urljoin
                    url=urljoin(url,response.headers.get('Location',''));continue
                if response.status_code in (404,410):raise JobUnavailable(f'Сайт вернул HTTP {response.status_code}.')
                if response.status_code!=200:raise CheckError(f'Сайт вернул HTTP {response.status_code}.')
                chunks=[];size=0
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>4_000_000:raise CheckError('Страница превышает лимит 4 MB.')
                    chunks.append(chunk)
                raw=b''.join(chunks)
                return url,raw.decode('utf-8',errors='replace')
        raise CheckError('Слишком много перенаправлений.')

    def json(self,url):
        _,text=self.get(url)
        try:return json.loads(text)
        except ValueError:raise CheckError('Источник не вернул ожидаемый JSON.') from None

    def page(self,url):
        parsed=urlsplit(canonical_url(url));origin=f'https://{parsed.hostname}'
        if origin not in self.robots:
            robot=RobotFileParser()
            try:
                _,body=self.get(origin+'/robots.txt');robot.parse(body.splitlines())
            except CheckError as exc:
                # Missing robots is distinct from denied/temporarily unavailable access.
                if 'HTTP 404' not in str(exc):raise
                robot.parse([])
            self.robots[origin]=robot
        if not self.robots[origin].can_fetch('PersonalJobHunter',url):raise CheckError('robots.txt не разрешает чтение этой страницы.')
        return self.get(url)


def vacancy(url,title,company,region,description,published='',applicant_locations=None):
    url=canonical_url(url)
    if not all(isinstance(v,str) and v.strip() for v in [title,company,description]):raise CheckError('Нет полного описания, названия или компании.')
    return {'id':str(int(hashlib.sha256(url.encode()).hexdigest()[:24],16)), 'name':title,
            'employer':{'name':company},'area':{'name':region},'alternate_url':url,
            'description':description,'published_at':published or '', 'applicant_locations':applicant_locations}


def _ashby(item,name):
    if item.get('isListed') is False:return None
    remote=item.get('workplaceType') or ('Remote' if item.get('isRemote') else '')
    region=item.get('location','')
    body=item.get('descriptionHtml') or item.get('descriptionPlain') or ''
    if not body.strip():raise CheckError('Доска содержит вакансию без полного описания.')
    return vacancy(item['jobUrl'],item['title'],name,region,
        f"<p>Location: {region}. Workplace: {remote}.</p>"+body,item.get('publishedAt',''))


def _greenhouse(item,name):
    from html import unescape
    region=item.get('location',{}).get('name','')
    if not item.get('content','').strip():raise CheckError('Доска содержит вакансию без полного описания.')
    return vacancy(item['absolute_url'],item['title'],name,region,
        '<p>Location: '+region+'</p>'+unescape(item.get('content','')),item.get('first_published') or item.get('updated_at',''))


def _lever(item,name):
    region=item.get('categories',{}).get('location','')
    description=item.get('description','')+''.join('<h3>'+v.get('text','')+'</h3>'+v.get('content','') for v in item.get('lists',[]))+item.get('additional','')
    description='<p>Location: '+region+'. Workplace: '+str(item.get('workplaceType',''))+'</p>'+description
    stamp=datetime.fromtimestamp(item['createdAt']/1000,timezone.utc).isoformat() if item.get('createdAt') else ''
    return vacancy(item['hostedUrl'],item['text'],name,region,description,stamp)


def board_jobs(client,board,*,errors=None):
    """Vacancies of one board. A malformed card is skipped and its title appended to `errors`
    (without `errors` it raises), so one «General Application» never hides the rest of the board."""
    slug=board['slug'];kind=board['kind'];name=board['name']
    if kind=='ashby':items,build=client.json(f'https://api.ashbyhq.com/posting-api/job-board/{slug}')['jobs'],_ashby
    elif kind=='greenhouse':items,build=client.json(f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true')['jobs'],_greenhouse
    else:items,build=client.json(f'https://api.lever.co/v0/postings/{slug}?mode=json'),_lever
    for item in items:
        try:job=build(item,name)
        except (CheckError,KeyError,TypeError,ValueError,AttributeError,OverflowError):
            if errors is None:raise
            errors.append(str((item.get('title') or item.get('text') or '?') if isinstance(item,dict) else '?')[:120]);continue
        if job is not None:yield job


class StructuredPage(HTMLParser):
    def __init__(self):super().__init__();self.blocks=[];self.collect=False;self.parts=[];self.links=[];self.in_main=False;self.main=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='main':self.in_main=True
        if self.in_main:self.main.append(self.get_starttag_text())
        if tag=='script' and attrs.get('type','').lower()=='application/ld+json':self.collect=True;self.parts=[]
        if tag=='a' and attrs.get('href'):self.links.append(attrs['href'])
    def handle_data(self,data):
        if self.collect:self.parts.append(data)
        if self.in_main:
            from html import escape
            self.main.append(escape(data))
    def handle_endtag(self,tag):
        if self.in_main:self.main.append('</'+tag+'>')
        if tag=='main':self.in_main=False
        if tag=='script' and self.collect:
            self.blocks.append(''.join(self.parts));self.collect=False


def jobpostings(text):
    page=StructuredPage();page.feed(text)
    def walk(value):
        if isinstance(value,list):
            for item in value:yield from walk(item)
        elif isinstance(value,dict):
            types=value.get('@type',[]);types=[types] if isinstance(types,str) else types
            if 'JobPosting' in types:yield value
            if '@graph' in value:yield from walk(value['@graph'])
    for block in page.blocks:
        try:yield from walk(json.loads(block))
        except ValueError:continue


def page_jobs(client,url):
    parts=urlsplit(canonical_url(url));segments=[v for v in parts.path.split('/') if v]
    kind={'jobs.ashbyhq.com':'ashby','job-boards.greenhouse.io':'greenhouse',
          'boards.greenhouse.io':'greenhouse','jobs.lever.co':'lever'}.get(parts.hostname)
    if kind and segments:
        found=False
        for item in board_jobs(client,{'kind':kind,'slug':segments[0],'name':segments[0]}):
            if len(segments)==1 or canonical_url(item['alternate_url'])==canonical_url(url):
                found=True;yield item
        if not found:raise JobUnavailable('Вакансия не найдена в опубликованной доске компании.')
        return
    final,html=client.page(url)
    items=list(jobpostings(html));visible=StructuredPage();visible.feed(html)
    main_text=html_to_text(''.join(visible.main))
    for item in items:
        body=item.get('description','')
        if not isinstance(body,str) or not body.strip():raise CheckError('JobPosting не содержит описания.')
        until=item.get('validThrough')
        if until:
            try:
                end=datetime.fromisoformat(until.replace('Z','+00:00'))
                if end.tzinfo is None:end=end.replace(tzinfo=timezone.utc)
                if end<datetime.now(timezone.utc):continue
            except (ValueError,AttributeError):pass
        org=item.get('hiringOrganization') or {};locations=item.get('jobLocation',[])
        if not isinstance(locations,list):locations=[locations]
        region=[]
        for location in locations:
            if not isinstance(location,dict):continue
            address=location.get('address') or {}
            if isinstance(address,str):region.append(address);continue
            region.extend(str(address[k]) for k in ('addressLocality','addressRegion','addressCountry') if address.get(k))
        restrictions=item.get('applicantLocationRequirements',[])
        if isinstance(restrictions,dict):restrictions=[restrictions]
        names=[v.get('name','') for v in restrictions if isinstance(v,dict)]
        detail='<p>Location: '+', '.join(region)+'</p>'
        if str(item.get('jobLocationType','')).upper()=='TELECOMMUTE':detail+='<p>Remote</p>'
        if names:detail+='<p>Applicants must be located in: '+', '.join(names)+'</p>'
        # Single-job pages often abbreviate JSON-LD. Preserve the actual visible main section too.
        if len(items)==1 and str(item.get('title') or '').casefold() in main_text.casefold() and len(main_text)>len(html_to_text(body)):
            from html import escape
            body+='<h3>Full visible job page</h3><p>'+escape(main_text).replace('\n','<br>')+'</p>'
        yield vacancy(item.get('url') or final,str(item.get('title') or ''),org.get('name','') if isinstance(org,dict) else str(org),
                      ', '.join(region),detail+body,item.get('datePosted',''),applicant_locations=names)


def external_location(job,preferences):
    from preferences import location_fit
    text=job['description_text'];region=job.get('region') or ''
    # "KZ" only as a standalone upper-case code: kaspi.kz or hr@kaspi.kz is a domain, not hiring from Kazakhstan.
    country=r'\bKazakhstan\b|(?<![\w.@/-])(?-i:KZ)(?![\w.])|Казахстан|Қазақстан'
    global_scope=r'\bworldwide\b|work from anywhere|remote anywhere|global(?:ly)? remote|в любой точке мира'
    requirements=job.get('applicant_locations')
    if requirements is None and job.get('location_requirements_json'):
        try:requirements=json.loads(job['location_requirements_json'])
        except (ValueError,TypeError):return 'unconfirmed','Не удалось проверить ограничения страны кандидата.'
    if requirements and not any(re.search(country+'|'+global_scope,str(value),re.I) for value in requirements):
        return 'blocked','В первоисточнике явно ограничена страна кандидата; Казахстан не указан.'
    explicit=re.findall(r'(?:applicants?|candidates?|you)\s+must\s+(?:be\s+)?(?:located|based|reside|live)\s+(?:in|within)\s*:?\s*([^\n.;]{1,130})',text,re.I)
    if any(not re.search(country+'|'+global_scope,value,re.I) for value in explicit):
        return 'blocked','В описании есть явное ограничение страны кандидата; Казахстан не указан.'
    restricted=re.search(r'(?:USA|United States|UK|EU|Europe|Canada|Germany|India)[ -]?(?:only|based)|'
        r'\b(?-i:US)[ -]?(?:only|based)|only (?:in|within) (?:the )?(?:USA|United States|UK|EU|Europe|Canada|Germany|India)|'
        r'must (?:be|reside|live)[^.\n]{0,70}(?:United States|Canada|UK|EU)|'
        r'remote[^\n.]{0,20}\b(?:USA|United States|Canada|UK|Europe|EU)\b',text,re.I)
    # The whole clause counts: "Remote (Europe or Kazakhstan)" names Kazakhstan after the matched region.
    clause=re.match(r'[^\n.;)]*\)?',text[restricted.start():]).group(0) if restricted else ''
    if restricted and not re.search(country,clause,re.I):
        return 'blocked','Внешняя удалёнка содержит ограничение страны/региона; Казахстан не подтверждён.'
    state,reason=location_fit(job,preferences)
    if state!='remote_review':return state,reason
    # Remote alone never proves eligibility from Kazakhstan.
    unrestricted=re.search(country+'|'+global_scope,text+' '+region,re.I)
    if unrestricted:return 'remote_review','Удалёнка из Казахстана/по миру указана; финальная проверка условий перед откликом.'
    return 'unconfirmed','Remote без подтверждения найма из Казахстана. Нужна ручная проверка.'


def web_plan(settings):
    representative={'junior':'AI Engineer','ml_ds':'ML Engineer','applied_ai':'LLM Engineer'}
    roles=list(dict.fromkeys([*settings['custom_roles'],*(representative[g] for g in settings['role_groups'])]))
    entry='(junior OR intern OR graduate)' if settings['junior_experience'] else ''
    queries=[f'"{role.replace(chr(34),chr(39))}" {entry} (Almaty OR Kazakhstan OR "remote worldwide") jobs careers' for role in roles[:6]]
    primary='"'+roles[0].replace('"','')+'"'
    for domain in ('linkedin.com/jobs/view','jobs.ashbyhq.com','job-boards.greenhouse.io','jobs.lever.co'):
        if len(queries)>=6:break
        queries.append(f'site:{domain} {primary} (Almaty OR Kazakhstan OR worldwide)')
    if len(queries)<6:queries.append(f'{primary} {entry} "work from anywhere" careers')
    return queries[:6]
