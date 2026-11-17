"""User-editable search scope; independent from permissions to submit applications."""
import json
import math
from pathlib import Path
from common import CheckError

DEFAULTS = {'role_groups':['junior','ml_ds','applied_ai'], 'junior_experience':True,
            'search_days':30, 'pages_per_query':2, 'vacancies_per_run':10,
            'budget_usd':0.5, 'excluded_title_words':[], 'custom_roles':[], 'scan_limit':12,
            'letter_mode':'personalized'}
GROUPS = {'junior':'Junior и стажировки', 'ml_ds':'ML и Data Science',
          'applied_ai':'Applied AI, LLM, NLP и Computer Vision'}


def validate(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULTS):
        raise CheckError('В настройках поиска есть неизвестные поля.')
    result={**DEFAULTS,**value}
    groups=result['role_groups']
    if not isinstance(groups,list) or (not groups and not result['custom_roles']) or not all(isinstance(g,str) and g in GROUPS for g in groups):
        raise CheckError('Выберите хотя бы одну из трёх групп вакансий.')
    result['role_groups']=list(dict.fromkeys(groups))
    if result['letter_mode'] not in {'personalized','literal'}:raise CheckError('Режим письма: personalized или literal.')
    if type(result['junior_experience']) is not bool:
        raise CheckError('junior_experience должен быть true или false.')
    for key,allowed in [('search_days',(1,3,7,30)),('pages_per_query',(1,2)),('vacancies_per_run',range(1,11))]:
        if type(result[key]) is not int or result[key] not in allowed:
            raise CheckError(f'Недопустимое значение {key}.')
    if type(result['scan_limit']) is not int or not 1<=result['scan_limit']<=30:
        raise CheckError('Объём сбора scan_limit: от 1 до 30 новых вакансий.')
    roles=result['custom_roles']
    if not isinstance(roles,list) or len(roles)>10 or not all(isinstance(v,str) and v.strip() and len(v)<=80 for v in roles):
        raise CheckError('Укажите до 10 дополнительных названий ролей.')
    result['custom_roles']=list(dict.fromkeys(v.strip() for v in roles))
    budget=result['budget_usd']
    if budget is not None and (type(budget) not in (int,float) or not math.isfinite(budget) or budget<=0):
        raise CheckError('Бюджет запуска должен быть положительным числом или null — без денежного лимита.')
    words=result['excluded_title_words']
    if not isinstance(words,list) or len(words)>30 or not all(isinstance(w,str) and w.strip() and len(w)<=80 for w in words):
        raise CheckError('Укажите не больше 30 непустых исключений для названия.')
    result['excluded_title_words']=list(dict.fromkeys(w.strip() for w in words))
    return result


def load(profile_dir):
    path=Path(profile_dir)/'search_settings.json'
    try:
        return validate(json.loads(path.read_text(encoding='utf-8'))) if path.exists() else validate({})
    except (OSError,ValueError):
        raise CheckError('Не удалось прочитать profile/search_settings.json.') from None


def save(profile_dir, settings):
    path=Path(profile_dir)/'search_settings.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(validate(settings),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    from common import replace_file
    replace_file(temporary,path)


def title_allowed(title, settings):
    # Whole words only (audit A32): «Lead» must not exclude «Leading AI Engineer».
    import re
    text=str(title or '').casefold()
    return not any(re.search(r'(?<!\w)'+re.escape(word.casefold())+r'(?!\w)',text) for word in settings['excluded_title_words'])


def relevant(title, settings):
    from relevance import title_is_relevant, OTHER_ROLE
    return title_allowed(title,settings) and (title_is_relevant(title) or
        (not OTHER_ROLE.search(title) and any(role.casefold() in title.casefold() for role in settings['custom_roles'])))
