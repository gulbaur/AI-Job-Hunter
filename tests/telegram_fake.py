"""In-memory stand-in for telegram_api.Bot: records calls, serves queued updates, injects one failure."""


class FakeApi:
    def __init__(self):
        self.sent=[]
        self.updates=[]
        self.fail_next=None
        self.fail_methods={}  # method name -> list of exceptions raised by its next calls
        self.next_id=100

    def _maybe_fail(self,method=None):
        if self.fail_next is not None:
            error,self.fail_next=self.fail_next,None
            raise error
        queue=self.fail_methods.get(method) if method else None
        if queue:raise queue.pop(0)

    def _record(self,method,**params):
        self._maybe_fail(method)
        self.sent.append((method,params))

    def get_me(self):
        self._maybe_fail()
        return {'username':'test_bot'}

    def get_updates(self,offset,timeout=50):
        self._record('getUpdates',offset=offset,timeout=timeout)
        batch,self.updates=self.updates,[]
        return batch

    def send_message(self,chat_id,text,keyboard=None,menu=None):
        self._record('sendMessage',chat_id=chat_id,text=text,keyboard=keyboard,menu=menu)
        self.next_id+=1
        return {'message_id':self.next_id}

    def set_commands(self,commands):
        self._record('setMyCommands',commands=commands)

    def edit_message(self,chat_id,message_id,text,keyboard=None):
        self._record('editMessageText',chat_id=chat_id,message_id=message_id,text=text,keyboard=keyboard)

    def send_document(self,chat_id,path,caption=''):
        self._record('sendDocument',chat_id=chat_id,path=str(path),caption=caption)
        self.next_id+=1
        return {'message_id':self.next_id}

    def send_photo(self,chat_id,path,caption='',keyboard=None):
        self._record('sendPhoto',chat_id=chat_id,path=str(path),caption=caption,keyboard=keyboard)
        self.next_id+=1
        return {'message_id':self.next_id}

    def answer_callback(self,callback_id,text=''):
        self._record('answerCallbackQuery',callback_id=callback_id,text=text)

    # Helpers for assertions
    def calls(self,method):
        return [params for name,params in self.sent if name==method]

    def texts(self):
        return [p['text'] for name,p in self.sent if name in {'sendMessage','editMessageText','answerCallbackQuery'}]
