"""One bounded OpenRouter JSON request; no automatic retries or credential logging."""

import json
import math

import requests

from common import CheckError


class RejectedOutput(CheckError):
    def __init__(self, message: str, result, usage: dict):
        super().__init__(message)
        self.result = result
        self.usage = usage


class ModelUnavailable(CheckError):
    pass


def request_json(session, payload: dict, key: str, validator) -> tuple[dict, dict]:
    try:
        response = session.post("https://openrouter.ai/api/v1/chat/completions",
                                headers={"Authorization": f"Bearer {key}"}, json=payload,
                                timeout=(10, 90), allow_redirects=False)
    except requests.RequestException:
        raise ModelUnavailable("Запрос OpenRouter не завершён; повтор доступен по вашему действию.") from None
    with response:
        if response.status_code != 200:
            raise ModelUnavailable(f"OpenRouter: HTTP {response.status_code}; запрос остановлен.")
        try:
            body = response.json()
        except ValueError:
            raise CheckError("OpenRouter вернул некорректный ответ.") from None
        usage = body.get("usage") if isinstance(body, dict) else None
        clean_usage = {}
        if isinstance(usage, dict):
            for field in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
                value = usage.get(field)
                if type(value) in {int, float} and math.isfinite(value) and value >= 0:
                    clean_usage[field] = value
        raw = None
        try:
            choice = body["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise CheckError("Ответ модели не завершён; результат отклонён.")
            raw = json.loads(choice["message"]["content"])
            result = validator(raw)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise RejectedOutput("OpenRouter вернул неверную структуру ответа.", raw, clean_usage) from None
        except CheckError as exc:
            raise RejectedOutput(str(exc), raw, clean_usage) from None
    return result, clean_usage
