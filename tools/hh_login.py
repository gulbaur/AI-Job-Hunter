"""One-time HH login for the program's own browser profile (needed only to send responses; search works without it).

Opens hh.kz in a visible window. Log in there, then close the window. The program then checks, in the same
profile, that HH no longer redirects to the login page. The password is typed only into hh.kz, never here.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browser_check import PROFILE_DIR, BrowserStop, navigate, verify_login  # noqa: E402
import run_lock  # noqa: E402

WAIT_MS = 15 * 60 * 1000


def logged_in(context):
    try:
        return verify_login(context)
    except BrowserStop as exc:  # captcha or network: say it, do not pretend «not logged in»
        print(str(exc))
        return False


def main():
    from playwright.sync_api import sync_playwright
    try:
        lock = run_lock.one_run(ROOT / 'profile/hunt.lock')
        lock.__enter__()
    except run_lock.RunBusy:
        print('Сейчас идёт подготовка писем. Запустите вход через несколько минут.')
        return 1
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False,
                                                           viewport={'width': 1280, 'height': 860})
            try:
                if logged_in(context):
                    print('Вход в HH уже сохранён. Ничего делать не нужно.')
                    return 0
                page = context.new_page()
                navigate(page, 'https://hh.kz/account/login?backurl=%2Fapplicant%2Fresumes', 'вход HH')
                print('Открылось окно HH. Войдите в свой аккаунт, затем ЗАКРОЙТЕ это окно (до 15 минут).')
                try:
                    context.wait_for_event('close', timeout=WAIT_MS)
                except Exception:
                    pass
            finally:
                try:
                    context.close()
                except Exception:
                    pass
            context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
            try:
                ok = logged_in(context)
            finally:
                context.close()
        print('✅ Вход в HH сохранён. Отклики из бота теперь работают.' if ok else
              '❌ Вход не сохранился. Запустите ещё раз и закройте окно только после входа.')
        return 0 if ok else 2
    finally:
        lock.__exit__(None, None, None)


if __name__ == '__main__':
    raise SystemExit(main())
