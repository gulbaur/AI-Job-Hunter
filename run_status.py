"""Shared interpretation of preparation reports for the UI and local notices."""
import hashlib
import json


def problem(report):
    if not isinstance(report,dict):return ''
    if report.get('state') in {'stopped','interrupted'} or report.get('exit_code',0) not in (None,0):
        return report.get('error') or 'Подготовка остановлена. Откройте журнал и повторите нужный этап явно.'
    if report.get('work_issues'):
        return f"Требуют внимания вакансии: {len(report['work_issues'])}. Доступен явный повтор подготовки."
    if report.get('coverage_warnings'):return 'Поиск неполный: '+report['coverage_warnings'][0]
    if report.get('state') in {'needs_attention','search_incomplete'}:return 'Подготовка требует внимания. Проверьте отчёт и список ошибок.'
    return ''


def fingerprint(report):
    detail={'problem':problem(report),'warnings':report.get('coverage_warnings',[]),
        'issues':[(v.get('source'),v.get('vacancy_id'),v.get('stage'),v.get('error')) for v in report.get('work_issues',[])]}
    return hashlib.sha256(json.dumps(detail,ensure_ascii=False,sort_keys=True).encode()).hexdigest() if detail['problem'] else ''
