"""OpenAI 兼容的 Chat / Embedding 客户端，带指数退避重试。"""

import asyncio
import json
import time
import urllib.error
import urllib.request

import httpx


class AIError(Exception):
    pass


def parse_json(text: str) -> dict:
    """解析模型输出，容忍 ```json 围栏与前后多余文本。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise AIError("模型未返回 JSON 对象")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AIError(f"模型 JSON 解析失败: {exc}") from exc


class AIClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        chat_model: str,
        embedding_model: str = "",
        embedding_base_url: str = "",
        embedding_api_key: str = "",
        reasoning_effort: str = "",
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.chat_model = chat_model or ""
        self.embedding_model = embedding_model or ""
        self.embedding_base_url = (embedding_base_url or self.base_url).rstrip("/")
        self.embedding_api_key = embedding_api_key or self.api_key
        self.reasoning_effort = (reasoning_effort or "").strip()
        self.usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def _record_usage(self, data: dict) -> None:
        usage = data.get("usage") or {}
        for key in self.usage:
            value = usage.get(key)
            if isinstance(value, int):
                self.usage[key] += value

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.chat_model)

    @property
    def embeddings_configured(self) -> bool:
        return bool(self.embedding_base_url and self.embedding_model)

    def _post(
        self,
        path: str,
        payload: dict,
        timeout: int = 180,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        headers = {"Content-Type": "application/json"}
        token = self.api_key if api_key is None else api_key
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{(base_url or self.base_url)}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise AIError(f"AI 服务返回 HTTP {exc.code}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise AIError(f"无法连接 AI 服务: {exc.reason}") from exc
        except TimeoutError as exc:
            raise AIError("AI 服务响应超时") from exc
        except json.JSONDecodeError as exc:
            raise AIError("AI 服务返回了非 JSON 响应") from exc

    def chat_json(self, system: str, user: str, retries: int = 3) -> dict:
        payload = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        }
        last: Exception | None = None
        for attempt in range(retries):
            try:
                data = self._post("/chat/completions", payload)
                self._record_usage(data)
                content = data["choices"][0]["message"]["content"]
                return parse_json(content)
            except (AIError, KeyError, IndexError, TypeError) as exc:
                last = exc
                if attempt + 1 < retries:
                    time.sleep(2**attempt)
        raise AIError(f"AI 调用失败（重试 {retries} 次）: {last}")

    def _text_payload(self, system: str, user: str, temperature: float) -> dict:
        payload = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def chat_text(
        self, system: str, user: str, retries: int = 3, temperature: float = 0.3
    ) -> str:
        payload = self._text_payload(system, user, temperature)
        last: Exception | None = None
        for attempt in range(retries):
            try:
                data = self._post("/chat/completions", payload)
                self._record_usage(data)
                content = data["choices"][0]["message"]["content"]
                if not (content or "").strip():
                    raise AIError("模型返回空内容")
                return content
            except (AIError, KeyError, IndexError, TypeError) as exc:
                last = exc
                if attempt + 1 < retries:
                    time.sleep(2**attempt)
        raise AIError(f"AI 调用失败（重试 {retries} 次）: {last}")

    async def chat_text_async(
        self, system: str, user: str, retries: int = 3, temperature: float = 0.3
    ) -> str:
        """异步版本，任务被取消时立即中断在途 HTTP 请求。"""
        payload = self._text_payload(system, user, temperature)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last: Exception | None = None
        async with httpx.AsyncClient(timeout=180) as http:
            for attempt in range(retries):
                try:
                    response = await http.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    response.raise_for_status()
                    data = response.json()
                    self._record_usage(data)
                    content = data["choices"][0]["message"]["content"]
                    if not (content or "").strip():
                        raise AIError("模型返回空内容")
                    return content
                except httpx.HTTPStatusError as exc:
                    body = exc.response.text[:300]
                    last = AIError(
                        f"AI 服务返回 HTTP {exc.response.status_code}: {body}"
                    )
                except httpx.TimeoutException as exc:
                    last = AIError("AI 服务响应超时")
                except httpx.HTTPError as exc:
                    last = AIError(f"无法连接 AI 服务: {exc}")
                except (AIError, KeyError, IndexError, TypeError, ValueError) as exc:
                    last = exc
                if attempt + 1 < retries:
                    await asyncio.sleep(2**attempt)
        raise AIError(f"AI 调用失败（重试 {retries} 次）: {last}")

    async def chat_text_stream(self, messages: list[dict], temperature: float = 0.3):
        """流式返回文本增量；调用方取消时 httpx 请求随生成器一起中断。"""
        payload = {
            "model": self.chat_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300)) as http:
                async with http.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        raise AIError(
                            f"AI 服务返回 HTTP {response.status_code}: {body[:300]}"
                        )
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            self._record_usage(chunk)
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        text = (choices[0].get("delta") or {}).get("content")
                        if text:
                            yield text
        except httpx.TimeoutException as exc:
            raise AIError("AI 服务响应超时") from exc
        except httpx.HTTPError as exc:
            raise AIError(f"无法连接 AI 服务: {exc}") from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = self._post(
            "/embeddings",
            {"model": self.embedding_model, "input": texts},
            base_url=self.embedding_base_url,
            api_key=self.embedding_api_key,
        )
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in items]
        if len(vectors) != len(texts) or any(vector is None for vector in vectors):
            raise AIError("Embedding 返回数量与请求不一致")
        return vectors
