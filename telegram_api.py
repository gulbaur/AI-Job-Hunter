"""Minimal Telegram Bot API client over requests. The token never appears in errors or logs."""
import json
from pathlib import Path

import requests

from common import CheckError

BASE='https://api.telegram.org'


class TelegramError(CheckError):
    pass


class TelegramUnauthorized(TelegramError):
    pass


class TelegramForbidden(TelegramError):
    pass


class TelegramUnavailable(TelegramError):
    pass


class TelegramRetryAfter(TelegramError):
    def __init__(self,message,seconds):
        super().__init__(message)
        self.seconds=seconds


def scrub(text,token):
    text=str(text)
    return text.replace('bot'+token,'bot<скрыто>').replace(token,'<скрыто>') if token else text


class Bot:
    def __init__(self,token,session=None):
        self.token=token
        self.session=session or requests.Session()

    def call(self,method,*,files=None,http_timeout=30,**params):
        url=f'{BASE}/bot{self.token}/{method}'
        try:
            if files:
                data={k:(json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v) for k,v in params.items()}
                response=self.session.post(url,data=data,files=files,timeout=http_timeout)
            else:
                response=self.session.post(url,json=params,timeout=http_timeout)
        except requests.RequestException as exc:
            raise TelegramUnavailable(scrub(f'Telegram недоступен: {type(exc).__name__}: {exc}',self.token)) from None
        try:body=response.json()
        except ValueError:body={}
        if response.status_code==200 and body.get('ok'):return body.get('result')
        code=body.get('error_code') or response.status_code
        message=scrub(f"Telegram {method}: HTTP {code} — {body.get('description') or 'нет описания'}",self.token)
        if code in (401,404):raise TelegramUnauthorized(message)  # 404: malformed bot<token> path
        if code==403:raise TelegramForbidden(message)
        if code==429:raise TelegramRetryAfter(message,int((body.get('parameters') or {}).get('retry_after') or 5))
        if code>=500 or code==0:raise TelegramUnavailable(message)
        raise TelegramError(message)

    def get_me(self):
        return self.call('getMe')

    def get_updates(self,offset,timeout=50):
        params={'timeout':timeout,'allowed_updates':['message','callback_query']}
        if offset is not None:params['offset']=offset
        return self.call('getUpdates',http_timeout=timeout+10,**params)

    @staticmethod
    def _markup(keyboard):
        return {'inline_keyboard':keyboard} if keyboard else None

    def send_message(self,chat_id,text,keyboard=None,menu=None):
        params={'chat_id':chat_id,'text':text,'parse_mode':'HTML','disable_web_page_preview':True}
        if keyboard:params['reply_markup']=self._markup(keyboard)
        # A message carries one keyboard: inline buttons or the persistent menu under the input field.
        elif menu:params['reply_markup']={'keyboard':[[{'text':t} for t in row] for row in menu],
                                          'resize_keyboard':True,'is_persistent':True}
        return self.call('sendMessage',**params)

    def set_commands(self,commands):
        return self.call('setMyCommands',commands=[{'command':c,'description':d} for c,d in commands])

    def edit_message(self,chat_id,message_id,text,keyboard=None):
        params={'chat_id':chat_id,'message_id':message_id,'text':text,'parse_mode':'HTML',
                'disable_web_page_preview':True,'reply_markup':self._markup(keyboard) or {'inline_keyboard':[]}}
        try:self.call('editMessageText',**params)
        except TelegramError as exc:
            if 'message is not modified' not in str(exc):raise

    def send_document(self,chat_id,path,caption=''):
        path=Path(path)
        with path.open('rb') as stream:
            return self.call('sendDocument',files={'document':(path.name,stream)},http_timeout=120,chat_id=chat_id,caption=caption)

    def send_photo(self,chat_id,path,caption='',keyboard=None):
        path=Path(path);params={'chat_id':chat_id,'caption':caption,'parse_mode':'HTML'}
        if keyboard:params['reply_markup']=self._markup(keyboard)
        with path.open('rb') as stream:
            return self.call('sendPhoto',files={'photo':(path.name,stream)},http_timeout=120,**params)

    def answer_callback(self,callback_id,text=''):
        self.call('answerCallbackQuery',callback_query_id=callback_id,text=text[:190])
