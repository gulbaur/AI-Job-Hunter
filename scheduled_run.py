"""One unattended preparation per morning/evening slot. Never opens response forms."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys

import hunt
from common import CheckError

ROOT=Path(__file__).resolve().parent
STATE=ROOT/'data/last_scheduled_run.json'
ALMATY=timezone(timedelta(hours=5))


def schedule_slot(moment):
    local=moment.astimezone(ALMATY)
    if local.hour<10:return None
    return local.date().isoformat()+('/18:00' if local.hour>=18 else '/10:00')


def plan():
    import hashlib
    import sources,search_settings
    slot=schedule_slot(datetime.now(ALMATY))
    old=json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    settings=search_settings.load(ROOT/'profile');config=sources.load(ROOT/'profile')
    queries=sources.web_plan(settings) if 'web' in config['enabled'] else []
    return {'due':slot is not None and old.get('slot')!=slot,'slot':slot,'queries':queries,
            'plan_hash':hashlib.sha256(json.dumps(queries,ensure_ascii=False).encode()).hexdigest(),
            'output':str(ROOT/'data/web_discovery.json'),'max_candidates':30}


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    slot=schedule_slot(datetime.now(ALMATY))
    if slot is None:
        print('Плановые окна начинаются в 10:00 и 18:00 по Алматы. Сейчас запуска нет.');return 0
    try:
        # Claim the slot only after the shared lock; hunt.main reenters it in
        # this context, so reports cannot be replaced by a competing process.
        with hunt.one_run(ROOT/'profile/scheduled.lock'),hunt.one_run(ROOT/'profile/hunt.lock'):
            old=json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
            if old.get('slot')==slot:
                print('Обработка в этом плановом окне уже начиналась; повторного запуска нет.')
                return 0
            state={'slot':slot,'state':'started','mode':'prepare_only','runner':'windows',
                   'started_at':datetime.now(timezone.utc).isoformat()}
            hunt.save_report(state,STATE)
            hunt.save_report({'state':'running','slot':slot},ROOT/'data/schedule_waiting.json')
            try:code=hunt.main(['--run','--queue'])
            except SystemExit as exc:
                code=exc.code if isinstance(exc.code,int) else 1
                state.update(state='stopped',exit_code=code,finished_at=datetime.now(timezone.utc).isoformat(),
                    error='Запуск остановлен до начала работы: неверные настройки поиска (profile/search_settings.json). '
                          'Откройте настройки на сайте программы и сохраните их заново.')
                hunt.save_report(state,STATE);return code
            report=json.loads(hunt.LAST.read_text(encoding='utf-8'))
            state.update(state=report['state'],exit_code=code,run_id=report.get('run_id'),
                         report_path=report.get('report_path',str(hunt.LAST)),
                         prepared_count=len(report.get('packet_paths',[])),model_usage=report.get('model_usage'),
                         coverage_warnings=report.get('coverage_warnings',[]),
                         work_issues=report.get('work_issues',[]),error=report.get('error'),
                         finished_at=datetime.now(timezone.utc).isoformat())
            hunt.save_report(state,STATE)
            return code
    except hunt.RunBusy:
        hunt.save_report({'state':'waiting','slot':slot,'updated_at':datetime.now(timezone.utc).isoformat(),
            'message':'Ожидаю освобождения программы; обработка в этом окне ещё не начиналась.'},ROOT/'data/schedule_waiting.json')
        print('Программа занята; плановое окно не израсходовано.')
        return 75
    except CheckError as exc:
        print(f'Плановый запуск остановлен: {exc}');return 1
    except (OSError,ValueError,KeyError):
        print('Не удалось прочитать состояние планового запуска. Новая отправка не запускалась.');return 1


if __name__=='__main__':
    if sys.argv[1:]==['--plan']:
        print(json.dumps(plan(),ensure_ascii=False,indent=2))
    elif sys.argv[1:]:raise SystemExit('Поддерживается только --plan или запуск без аргументов.')
    else:raise SystemExit(main())
