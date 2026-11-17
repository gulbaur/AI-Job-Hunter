"""Bounded first-party company reading, with a vacancy-only fallback."""
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests
import assess
from common import CheckError,html_to_text
import sources
from web_search import cached_query

AGGREGATORS=('linkedin.com','hh.kz','hh.ru','ashbyhq.com','greenhouse.io','lever.co','aijobs.net','indeed.com',
             'glassdoor.com','jobberbuzz.com','finder.work','careerist.ru','alion.io','dsml.kz','wikipedia.org')
LEGAL={'тоо','нао','ао','ип','llc','ltd','limited','inc','company','corporation','the','казахстан','kazakhstan'}
GENERIC={'technologies','technology','tech','systems','solutions','digital','group','bank','services',
         'engineering','international','national','global','центр','банк','технологии','компания'}
POLICY_VERSION=2


def tokens(name):
    return [v for v in re.findall(r'[\w]+',name.casefold()) if v not in LEGAL and len(v)>2]


def first_party(url,name,text='',trusted_domains=()):
    host=urlsplit(url).hostname or ''
    if any(host==v or host.endswith('.'+v) for v in AGGREGATORS):return False
    if any(host==domain or host.endswith('.'+domain) for domain in trusted_domains):return True
    words=tokens(name);brand=[word for word in words if word not in GENERIC]
    try:host=host.encode('ascii').decode('idna')
    except (UnicodeError,UnicodeEncodeError):pass
    labels={re.sub(r'[-_]','',label) for label in host.casefold().split('.')}
    # Match the whole distinctive brand, never just "Technologies", "Bank", etc.
    domain_match=bool(brand and (''.join(brand) in labels or ''.join(words) in labels))
    content_match=not text or all(re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',text,re.I) for word in brand)
    return domain_match and content_match


def research(job,*,profile_dir,session,key,model,client=None):
    root=Path(profile_dir).parent;name=job.get('company') or ''
    trusted=[]
    for domain in re.findall(r'\b[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\b',name):trusted.append(domain.casefold())
    try:
        mapping=json.loads((Path(profile_dir)/'company_domains.json').read_text(encoding='utf-8'))
        for company,domains in mapping.items():
            if company.strip().casefold()!=name.strip().casefold():continue
            for domain in domains:
                trusted.append(urlsplit(sources.canonical_url(domain)).hostname)
    except (OSError,ValueError,AttributeError,TypeError,CheckError):pass
    identity=hashlib.sha256(json.dumps([POLICY_VERSION,name,job['url'],sorted(trusted)],ensure_ascii=False).encode()).hexdigest()
    path=root/'data/company_research'/(identity+'.json')
    try:
        old=json.loads(path.read_text(encoding='utf-8'))
        age=datetime.now(timezone.utc)-datetime.fromisoformat(old['retrieved_at'])
        ttl=timedelta(days=3) if old.get('sources') else timedelta(hours=2)
        if timedelta(0)<=age<ttl:return old
    except (OSError,ValueError,KeyError,TypeError):pass
    result={'company':name,'status':'vacancy_only','sources':[],'fragments':{},'retrieved_at':datetime.now(timezone.utc).isoformat(),
            'note':'Надёжных сведений о компании пока нет; письмо опирается на задачи вакансии.'}
    candidates=[]
    candidates.extend('https://'+domain+'/' for domain in trusted)
    if job.get('source')=='web' and first_party(job['url'],name):candidates.append(job['url'])
    # Exact domains present in the employer's name, e.g. Kaspi.kz.
    for domain in re.findall(r'\b[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\b',name):candidates.append('https://'+domain+'/')
    try:
        found=cached_query('"'+name.replace('"','')+'" официальный сайт о компании',session=session,key=key,
            model=model,cache_dir=root/'data/company_search_cache',ttl_hours=72)
        candidates.extend(hit['url'] for hit in found['hits'])
    except CheckError as exc:result['note']=str(exc)+' Письмо опирается на вакансию.'
    owned=requests.Session() if client is None else None
    public=client or sources.PublicClient(owned)
    try:
        for url in list(dict.fromkeys(candidates))[:6]:
            if len(result['sources'])>=2:break
            try:
                canonical=sources.canonical_url(url)
                host=urlsplit(canonical).hostname
                if not first_party(canonical,name,trusted_domains=trusted):continue
                final,html=public.page(canonical)
                parser=sources.StructuredPage();parser.feed(html)
                body=html_to_text(''.join(parser.main)) or html_to_text(html)
                body=assess.normalize(body)[:6500]
                if len(body)<150 or not first_party(final,name,body,trusted_domains=trusted):continue
                # Two links may land on the same page or text (audit A29): keep one source.
                if any(sources.canonical_url(final)==sources.canonical_url(old['url']) or body==old['text'] for old in result['sources']):continue
                result['sources'].append({'url':final,'title':name,'retrieved_at':result['retrieved_at'],'text':body})
            except (CheckError,requests.RequestException,ValueError):continue
    finally:
        if owned is not None:owned.close()
    for record in result['sources']:
        for text in assess.fragments(record['text'],'C').values():
            if any(text==old['text'] for old in result['fragments'].values()):continue
            identity=f"C{len(result['fragments'])+1:03d}"
            result['fragments'][identity]={'text':text,'url':record['url']}
            if len(result['fragments'])>=26:break
        if len(result['fragments'])>=26:break
    if result['sources']:
        result.update(status='company_sources',note='Письмо использует проверенные страницы компании и описание вакансии.')
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)
    return result
