"""One-time Telegram setup: check the bot token, pair the owner's private chat by a 6-digit code, save TELEGRAM_CHAT_ID.

Run from the project root:  .venv\\Scripts\\python.exe -X utf8 tools\\telegram_setup.py
Run it while the bot task is stopped: Telegram allows only one getUpdates reader per bot.
"""
from pathlib import Path
import re
import secrets
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from common import CheckError  # noqa: E402


def pair(api,code,*,deadline_s=600,sleep=time.sleep,clock=time.monotonic):
    """chat_id of the private chat that sent '/start <code>' before the deadline."""
    end=clock()+deadline_s;offset=None
    while clock()<end:
        for update in api.get_updates(offset,timeout=20):
            offset=int(update['update_id'])+1
            message=update.get('message') or {};chat=message.get('chat') or {}
            if chat.get('type')=='private' and str(message.get('text') or '').strip()==f'/start {code}':
                api.get_updates(offset,timeout=0)  # acknowledge, so the bot never sees the pairing message
                return str(chat['id'])
        sleep(1)
    raise CheckError('Код не получен за 10 минут. Запустите настройку ещё раз.')


def write_env(path,name,value):
    """Set NAME=value in .env, keeping every other line (and the file's line endings) byte for byte."""
    path=Path(path)
    raw=path.read_bytes() if path.exists() else b''
    newline=b'\r\n' if b'\r\n' in raw else b'\n'
    lines=raw.split(newline) if raw else []
    trailing=bool(lines) and lines[-1]==b''
    if trailing:lines=lines[:-1]
    entry=f'{name}={value}'.encode()
    pattern=re.compile(rb'\s*'+re.escape(name.encode())+rb'\s*=')
    for index,line in enumerate(lines):
        if pattern.match(line):lines[index]=entry;break
    else:lines.append(entry)
    path.write_bytes(newline.join(lines)+newline)


def main():
    import telegram_api
    import telegram_store
    token,chat=telegram_store.settings(ROOT)
    if not token:
        print('В .env нет TELEGRAM_BOT_TOKEN.\n1) В Telegram откройте @BotFather → /newbot → придумайте имя.\n'
              '2) Вставьте выданный токен в .env строкой TELEGRAM_BOT_TOKEN=...\n3) Запустите эту настройку снова.')
        return 1
    api=telegram_api.Bot(token)
    try:me=api.get_me()
    except telegram_api.TelegramUnauthorized:
        print('Telegram не принял токен. Проверьте строку TELEGRAM_BOT_TOKEN в .env.');return 1
    if chat:print(f'Сейчас привязан чат {chat}; после новой привязки он будет заменён.')
    code=f'{secrets.randbelow(10**6):06d}'
    print(f"Бот: @{me.get('username')}\nОткройте его в Telegram и отправьте сообщение:\n\n    /start {code}\n\nЖду 10 минут…")
    try:chat=pair(api,code)
    except CheckError as exc:print(exc);return 1
    write_env(ROOT/'.env','TELEGRAM_CHAT_ID',chat)
    api.send_message(chat,'✅ Бот подключён к Personal AI Job Hunter. /help — команды.')
    print('Готово: чат привязан, TELEGRAM_CHAT_ID записан в .env.')
    return 0


if __name__=='__main__':raise SystemExit(main())
