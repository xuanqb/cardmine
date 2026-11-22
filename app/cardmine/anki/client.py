"""AnkiConnect 无头容器客户端。"""

import json
import time
import urllib.error
import urllib.request

READ_ACTIONS = {
    "version",
    "modelNames",
    "modelFieldNames",
    "deckNames",
    "findNotes",
    "notesInfo",
    "retrieveMediaFile",
}


class AnkiError(Exception):
    pass


class AnkiTransientError(AnkiError):
    """连接失败、超时或 5xx，读请求可以重试。"""


class AnkiClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def call(self, action: str, timeout: int = 60, **params):
        attempts = 3 if action in READ_ACTIONS else 1
        for attempt in range(attempts):
            try:
                return self._call_once(action, timeout, **params)
            except AnkiTransientError:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.5 * (2**attempt))

    def _call_once(self, action: str, timeout: int, **params):
        payload = json.dumps(
            {"action": action, "version": 6, "params": params}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            message = f"Anki 容器返回 HTTP {exc.code}: {body[:200]}"
            if exc.code >= 500:
                raise AnkiTransientError(message) from exc
            raise AnkiError(message) from exc
        except urllib.error.URLError as exc:
            raise AnkiTransientError(f"无法连接 Anki 容器: {exc.reason}") from exc
        except TimeoutError as exc:
            raise AnkiTransientError("Anki 容器响应超时") from exc
        except json.JSONDecodeError as exc:
            raise AnkiTransientError("Anki 容器返回了非 JSON 响应") from exc

        if data.get("error"):
            raise AnkiError(str(data["error"]))
        return data.get("result")

    def validate(self, model_name: str = "Basic", timeout: int = 60) -> dict:
        version = self.call("version", timeout=timeout)
        models = self.call("modelNames", timeout=timeout)
        if model_name not in models:
            raise AnkiError(f"Anki 中不存在模型 {model_name}")
        fields = self.call("modelFieldNames", modelName=model_name, timeout=timeout)
        if fields != ["Front", "Back"]:
            raise AnkiError(f"模型 {model_name} 字段应为 Front/Back，实际为 {fields}")
        return {"version": version, "model": model_name, "fields": fields}
