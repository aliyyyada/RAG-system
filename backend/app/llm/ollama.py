"""Клиент Ollama.

    client = Ollama(settings)
    vectors = await client.embed(["текст фрагмента", ...])      # bge-m3, 1024 числа
    async for delta in client.chat_stream(messages):              # qwen2.5:7b, по кусочку
        print(delta.text, end="")

В тестах и CI вместо настоящей Ollama — фейковая (fakes/fake_ollama.py), подставляется
через `set_transport_override(ASGITransport(app=fake_app))`.
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport_override
    _transport_override = transport


class OllamaError(Exception):
    """Ollama ответила ошибкой."""


class OllamaUnavailable(OllamaError):
    """Ollama не отвечает (контейнер не запущен, модель не загрузилась, таймаут)."""


class ModelNotFound(OllamaError):
    pass


@dataclass
class ChatDelta:
    text: str
    done: bool = False
    # Итог от Ollama в последнем куске: eval_count (токенов), eval_duration (нс) и т. п.
    stats: dict[str, Any] = field(default_factory=dict)
    model: str | None = None


class Ollama:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.ollama_url.rstrip("/"),
            transport=_transport_override,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "Ollama":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            r = await self._client.post(
                "/api/embed",
                json={"model": self.settings.embedding_model, "input": texts, "truncate": True},
            )
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(f"Ollama недоступна: {type(exc).__name__}") from exc
        if r.status_code == 404:
            raise ModelNotFound(f"Модель {self.settings.embedding_model} не загружена в Ollama")
        if r.status_code >= 500:
            raise OllamaUnavailable(f"Ollama: HTTP {r.status_code}")
        if r.status_code != 200:
            raise OllamaError(f"Ollama: HTTP {r.status_code} {r.text[:200]}")
        vectors = r.json().get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaError("Ollama вернула не столько векторов, сколько текстов")
        return [[float(x) for x in v] for v in vectors]

    async def chat_stream(self, messages: list[dict[str, str]]) -> AsyncIterator[ChatDelta]:
        """Ответ модели по кусочкам. Если основной модели нет в Ollama — запасная (qwen3:4b)."""
        models = [self.settings.llm_model]
        if self.settings.llm_fallback_model and self.settings.llm_fallback_model not in models:
            models.append(self.settings.llm_fallback_model)
        for i, model in enumerate(models):
            try:
                async for delta in self._chat(model, messages):
                    yield delta
                return
            except ModelNotFound:
                if i == len(models) - 1:
                    raise
                log.warning("Модели %s нет в Ollama, переключаемся на %s", model, models[i + 1])

    async def _chat(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[ChatDelta]:
        s = self.settings
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "num_ctx": s.llm_num_ctx,
                "temperature": s.llm_temperature,
                "num_predict": s.llm_max_tokens,
            },
        }
        if model.startswith("qwen3"):
            # У qwen3 «размышления» вслух: в ответ пользователю они не нужны и съедают время.
            body["think"] = False
        try:
            async with self._client.stream("POST", "/api/chat", json=body) as r:
                if r.status_code == 404:
                    raise ModelNotFound(f"Модель {model} не загружена в Ollama")
                if r.status_code != 200:
                    text = (await r.aread()).decode("utf-8", "replace")[:200]
                    cls = OllamaUnavailable if r.status_code >= 500 else OllamaError
                    raise cls(f"Ollama: HTTP {r.status_code} {text}")
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if item.get("error"):
                        raise OllamaError(str(item["error"]))
                    text = (item.get("message") or {}).get("content") or ""
                    done = bool(item.get("done"))
                    stats = (
                        {
                            k: item[k]
                            for k in (
                                "eval_count",
                                "eval_duration",
                                "prompt_eval_count",
                                "prompt_eval_duration",
                                "load_duration",
                                "total_duration",
                            )
                            if k in item
                        }
                        if done
                        else {}
                    )
                    if text or done:
                        yield ChatDelta(text=text, done=done, stats=stats, model=model)
                    if done:
                        return
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(f"Ollama недоступна: {type(exc).__name__}") from exc

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        max_tokens: int,
        model: str | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Ответ модели строго по JSON-схеме (structured outputs Ollama: поле `format`).

        Для фоновых задач (граф, противоречия): без потока, температура 0. Возвращает
        разобранный JSON и статистику Ollama (eval_count, eval_duration…)."""
        s = self.settings
        name = model or s.llm_model
        body: dict[str, Any] = {
            "model": name,
            "messages": messages,
            "stream": False,
            "format": schema,
            "options": {"num_ctx": s.llm_num_ctx, "temperature": 0, "num_predict": max_tokens},
        }
        if name.startswith("qwen3"):
            body["think"] = False
        try:
            r = await self._client.post("/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(f"Ollama недоступна: {type(exc).__name__}") from exc
        if r.status_code == 404:
            raise ModelNotFound(f"Модель {name} не загружена в Ollama")
        if r.status_code >= 500:
            raise OllamaUnavailable(f"Ollama: HTTP {r.status_code}")
        if r.status_code != 200:
            raise OllamaError(f"Ollama: HTTP {r.status_code} {r.text[:200]}")
        item = r.json()
        content = (item.get("message") or {}).get("content") or ""
        stats = {
            k: item[k]
            for k in ("eval_count", "eval_duration", "prompt_eval_count", "prompt_eval_duration")
            if k in item
        }
        try:
            return json.loads(content), stats
        except ValueError as exc:
            raise OllamaError("Модель вернула не JSON") from exc
