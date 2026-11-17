"""Windows entry point: local logs, no response submission."""
import contextlib
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import subprocess
import traceback
import time
from common import write_json
import run_status

ROOT=Path(__file__).resolve().parent


def main():
    logs=ROOT/'data/scheduled_logs';logs.mkdir(parents=True,exist_ok=True)
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.log'
    path=logs/name
    state_path=ROOT/'data/last_scheduled_run.json'
    previous={}
    try:previous=json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):pass
    with path.open('w',encoding='utf-8') as output,contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
        output.reconfigure(line_buffering=True)
        try:
            import common
            try:common.daily_backup(ROOT/'data/jobs.sqlite',ROOT/'data/backups')
            except Exception as exc:print(f'Резервная копия базы не создана: {type(exc).__name__}.',flush=True)
            import scheduled_run
            print('Autonomous Windows scheduler entry point.',flush=True)
            # Waiting is allowed only before processing has actually begun.
            for attempt in range(121):
                code=scheduled_run.main()
                if code!=75 or attempt==120:break
                time.sleep(30)
        except SystemExit as exc:
            # e.g. parser.error() on invalid settings (audit A19): still record the call and notify.
            traceback.print_exc();code=exc.code if isinstance(exc.code,int) else 1
        except Exception:
            traceback.print_exc();code=1
    state={}
    try:state=json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):pass
    prepared=state.get('prepared_count',0) if state.get('started_at')!=previous.get('started_at') else 0
    write_json(ROOT/'data/last_scheduler_invocation.json',{
        'invoked_at':datetime.now(timezone.utc).isoformat(),'exit_code':code,'log_path':str(path),
        'runner':'windows','prepared_count':prepared})
    notice_path=ROOT/'data/last_scheduler_notice.json';notice={}
    try:notice=json.loads(notice_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):pass
    problem_state=state
    if code==75:
        # 121 attempts over an hour: the window was never processed. Not an error of the run, but the user must know.
        problem_state={'state':'stopped','error':run_status.WAIT_EXPIRED,'invoked_at':datetime.now(timezone.utc).isoformat()}
    elif code!=0 and not run_status.problem(state):
        problem_state={'state':'stopped','error':'Не удалось запустить плановую подготовку. Подробности — в журнале приложения.'}
    signature=run_status.fingerprint(problem_state)
    new_problem=bool(signature and signature!=notice.get('last_failure'))
    import telegram_events
    if telegram_events.configured(ROOT):
        # Letters and search status arrive as the run's Telegram summary and cards (hunt.py);
        # here only run-level failures that the summary cannot carry: lost window, crash, stop.
        run_level=code!=0 or problem_state.get('state') in {'stopped','interrupted','started'}
        if new_problem and run_level:telegram_events.problem(ROOT/'data/jobs.sqlite',run_status.problem(problem_state),signature)
    elif (prepared or new_problem) and sys.platform=='win32':
        args=['powershell.exe','-NoProfile','-File',str(ROOT/'tools/notify_ready.ps1'),'-Count',str(min(prepared,10))]
        if new_problem:args.append('-Problem')
        subprocess.Popen(args,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    write_json(notice_path,{'last_failure':signature})
    return code


if __name__=='__main__':raise SystemExit(main())
