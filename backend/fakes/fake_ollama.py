"""Фейковая Ollama — только разработка без видеокарты, тесты и CI.

Говорит на том же HTTP API, что настоящая (/api/embed, /api/chat, /api/tags), но вместо
нейросетей — простые правила, чтобы тесты были быстрыми и предсказуемыми:

- **Эмбеддинги.** Слова текста приводятся к «основе» (первые 5 букв: «наряда», «наряду» →
  «наряд») и раскладываются хэшем по 1024 координатам. Тексты с общими словами получают
  близкие векторы — для проверки векторного поиска этого достаточно.
- **Ответ.** Модель берёт из промпта пронумерованные фрагменты и ищет тот, где больше всего
  общих основ с вопросом. Нашёлся — отвечает первым подходящим предложением со ссылкой [n].
  Не нашёлся — «В доступных вам документах ответа…». Вопрос без букв («2+2») — отказ.

Состояние для тестов — объект FAKE_OLLAMA: prompts (что модель получила: проверяем, что
текст чужих документов туда не попал), first_token_delay (проверка heartbeat),
unavailable (Ollama «лежит»), embed_calls.
"""

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

DIM = 1024
NO_ANSWER = "В доступных вам документах ответа на этот вопрос нет."
OFF_TOPIC = "Я отвечаю только на вопросы по документам предприятия."

_WORD = re.compile(r"[0-9a-zа-яё]+")
STOP = {
    "какие",
    "какой",
    "какая",
    "каком",
    "какого",
    "что",
    "кто",
    "где",
    "когда",
    "как",
    "для",
    "при",
    "или",
    "это",
    "все",
    "его",
    "она",
    "они",
    "нужны",
    "нужно",
    "нужен",
    "должен",
    "есть",
    "ли",
    "по",
    "на",
    "в",
    "и",
    "а",
    "с",
    "к",
    "о",
    "об",
    "от",
    "до",
    "из",
    "за",
    "не",
    "же",
    "то",
    "ты",
    "мне",
    "наш",
    "нас",
    "сколько",
    "можно",
    "каким",
    "кому",
}


def stems(text: str) -> list[str]:
    return [w[:5] for w in _WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    v[0] = 0.05  # нулевой вектор нельзя: косинусное расстояние для него не определено
    for s in stems(text):
        h = int.from_bytes(hashlib.md5(s.encode()).digest()[:4], "little")  # noqa: S324
        v[1 + h % (DIM - 1)] += 1.0
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


class FakeOllamaState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.prompts: list[list[dict[str, str]]] = []
        self.first_token_delay = 0.0
        self.token_delay = 0.0
        self.unavailable = False
        self.embed_calls = 0
        self.models = ["qwen2.5:7b-instruct-q4_K_M", "qwen3:4b", "bge-m3:latest"]


FAKE_OLLAMA = FakeOllamaState()
router = APIRouter(prefix="/ollama", tags=["ollama"])


def _check_up() -> None:
    if FAKE_OLLAMA.unavailable:
        raise HTTPException(503, "ollama is down")


@router.get("/api/tags")
async def tags() -> dict[str, Any]:
    _check_up()
    return {"models": [{"name": m} for m in FAKE_OLLAMA.models]}


@router.post("/api/embed")
async def embed_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    _check_up()
    model = str(body.get("model", ""))
    if not any(m.split(":")[0] == model.split(":")[0] for m in FAKE_OLLAMA.models):
        raise HTTPException(404, f'model "{model}" not found, try pulling it first')
    FAKE_OLLAMA.embed_calls += 1
    inp = body.get("input")
    texts = [inp] if isinstance(inp, str) else list(inp or [])
    return {"model": model, "embeddings": [embed(str(t)) for t in texts]}


_FRAGMENT = re.compile(r"^\[(\d+)\] [^\n]*\n(.*?)(?=^\[\d+\] |\Z)", re.S | re.M)


def compose_answer(messages: list[dict[str, str]]) -> str:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    if not re.search(r"[a-zа-яё]{3,}", question.lower()):
        return OFF_TOPIC
    q = set(stems(question))
    fragments = system.split("Фрагменты:", 1)[-1]
    best: tuple[int, int, str] | None = None
    for m in _FRAGMENT.finditer(fragments):
        n, body = int(m.group(1)), m.group(2)
        overlap = len(q & set(stems(body)))
        if overlap and (best is None or overlap > best[0]):
            best = (overlap, n, body)
    if best is None:
        return NO_ANSWER
    _, n, body = best
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", body) if s.strip()]
    sentence = max(sentences, key=lambda s: len(q & set(stems(s))))
    sentence = re.sub(r"^[#|\s]+", "", sentence).rstrip(" |")
    return f"По документам: {sentence} [{n}]"


@router.post("/api/chat")
async def chat(body: dict[str, Any]) -> StreamingResponse:
    _check_up()
    model = str(body.get("model", ""))
    if model not in FAKE_OLLAMA.models:
        raise HTTPException(404, f'model "{model}" not found, try pulling it first')
    messages = body.get("messages") or []
    FAKE_OLLAMA.prompts.append(messages)
    answer = compose_answer(messages)
    words = re.findall(r"\S+\s*", answer)

    async def stream() -> AsyncIterator[bytes]:
        if FAKE_OLLAMA.first_token_delay:
            await asyncio.sleep(FAKE_OLLAMA.first_token_delay)
        for w in words:
            if FAKE_OLLAMA.token_delay:
                await asyncio.sleep(FAKE_OLLAMA.token_delay)
            yield (
                json.dumps(
                    {"model": model, "message": {"role": "assistant", "content": w}, "done": False},
                    ensure_ascii=False,
                )
                + "\n"
            ).encode()
        yield (
            json.dumps(
                {
                    "model": model,
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "eval_count": len(words),
                    "eval_duration": max(1, len(words)) * 50_000_000,
                }
            )
            + "\n"
        ).encode()

    return StreamingResponse(stream(), media_type="application/x-ndjson")
