"""Shared interpretation of preparation reports for the UI and local notices."""
import hashlib
import json


INTERRUPTED='Предыдущая подготовка прервалась (процесс завершился, не дописав отчёт). Неудачные этапы доступны для явного повтора.'
WAIT_EXPIRED=('Плановая подготовка не выполнена: программа была занята больше часа '
              '(ручной запуск, вход в HH или подготовка формы). Это окно расписания пропущено.')


def problem(report):
    """Call only when no run is active: a report still in 'started' then means the process died mid-run."""
    if not isinstance(report,dict):return ''
    if report.get('state') in {'stopped','interrupted'} or report.get('exit_code',0) not in (None,0):
        return report.get('error') or 'Подготовка остановлена. Откройте журнал и повторите нужный этап явно.'
    if report.get('state')=='started' and not report.get('finished_at'):return INTERRUPTED
    # Every problem is named: a list of failed letters must not hide a broken search source.
    parts=['Поиск неполный: '+warning for warning in report.get('coverage_warnings') or []]
    if report.get('work_issues'):
        parts.append(f"Требуют внимания вакансии: {len(report['work_issues'])}. Доступен явный повтор подготовки.")
    if parts:return ' '.join(parts)
    if report.get('state') in {'needs_attention','search_incomplete'}:return 'Подготовка требует внимания. Проверьте отчёт и список ошибок.'
    return ''


def fingerprint(report):
    detail={'problem':problem(report),'warnings':report.get('coverage_warnings',[]),
        'issues':[(v.get('source'),v.get('vacancy_id'),v.get('stage'),v.get('error')) for v in report.get('work_issues',[])],
        # A lost window or a killed run is a new event each time, unlike a report that keeps the same failures.
        'event':report.get('invoked_at') or (report.get('started_at') if report.get('state')=='started' else None)}
    return hashlib.sha256(json.dumps(detail,ensure_ascii=False,sort_keys=True).encode()).hexdigest() if detail['problem'] else ''
