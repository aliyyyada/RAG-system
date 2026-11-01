"""Фейковая Ollama — только разработка без видеокарты, тесты и CI.

Говорит на том же HTTP API, что настоящая (/api/embed, /api/chat, /api/tags), но вместо
нейросетей — простые правила, чтобы тесты были быстрыми и предсказуемыми:

- **Эмбеддинги.** Слова текста приводятся к «основе» (первые 5 букв: «наряда», «наряду» →
  «наряд») и раскладываются хэшем по 1024 координатам. Тексты с общими словами получают
  близкие векторы — для проверки векторного поиска этого достаточно.
- **Ответ.** Модель берёт из промпта пронумерованные фрагменты и ищет тот, где больше всего
  общих основ с вопросом. Нашёлся — отвечает первым подходящим предложением со ссылкой [n].
  Не нашёлся — «В доступных вам документах ответа…». Вопрос без букв («2+2») — отказ.

- **Извлечение для графа (этап 5)**, запрос с `format` (JSON-схема): сущности находятся
  регулярными выражениями по словарю фейковых документов (люди «Фамилия И. О.», должности,
  подразделения, оборудование, участки, «работы на высоте», сроки), связи — по словам
  в том же предложении («ответственным … назначить» → responsible_for).

Состояние для тестов — объект FAKE_OLLAMA: prompts (что модель получила: проверяем, что
текст чужих документов туда не попал), first_token_delay (проверка heartbeat),
unavailable (Ollama «лежит»), embed_calls, extract_calls, extract_prompts, bad_json.
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
        self.extract_calls = 0
        self.extract_prompts: list[list[dict[str, str]]] = []
        self.bad_json = False
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
    # Справка графа (этап 5): «- Приказ № 112 … отменяет Приказ № 45 … [1]». Если связь
    # подходит к вопросу лучше фрагментов — отвечаем ей, как сделала бы настоящая модель.
    head = system.split("Фрагменты:", 1)[0]
    if "графа знаний" in head:
        facts = re.findall(r"^- (.+?) \[(\d+)\]$", head, re.M)
        scored = [(len(q & set(stems(t))), t, n) for t, n in facts]
        scored = [x for x in scored if x[0] >= 2]
        if scored:
            _, fact, n = max(scored, key=lambda x: x[0])
            return f"По документам: {fact} [{n}]"
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
    if body.get("format") is not None:
        return _structured(model, messages)  # type: ignore[return-value]
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


# ---------------------------------------------------------------------------
# Извлечение сущностей (этап 5)
# ---------------------------------------------------------------------------

_SURNAMES = {"Иванову": "Иванова", "Петрова": "Петров", "Смирнова": "Смирнов"}
_PATTERNS: list[tuple[str, re.Pattern[str], Any]] = [
    ("person", re.compile(r"([А-ЯЁ][а-яё]{2,})\s+([А-ЯЁ])\.\s?([А-ЯЁ])\."), None),
    (
        "position",
        re.compile(r"начальник\w*\s+службы\s+ОТ\s+и\s+ПБ", re.I),
        "начальник службы ОТ и ПБ",
    ),
    ("position", re.compile(r"мастер\w*\s+участка", re.I), "мастер участка"),
    ("position", re.compile(r"главн\w+\s+инженер\w*", re.I), "главный инженер"),
    (
        "position",
        re.compile(r"ответственн\w+\s+руководител\w+\s+работ", re.I),
        "ответственный руководитель работ",
    ),
    ("department", re.compile(r"служб\w+\s+ОТ\s+и\s+ПБ", re.I), "служба ОТ и ПБ"),
    ("department", re.compile(r"литейн\w+\s+цех\w*\s+№\s*(\d+)", re.I), "литейный цех № {0}"),
    ("equipment", re.compile(r"кран\w*\s+мостов\w+\s+(КМ-\d+)", re.I), "кран мостовой {0}"),
    ("equipment", re.compile(r"сварочн\w+\s+аппарат\w*", re.I), "сварочный аппарат"),
    ("site", re.compile(r"\bучасток\s+(\d+)", re.I), "участок {0}"),
    ("work", re.compile(r"работ\w*\s+на\s+высоте", re.I), "работы на высоте"),
    (
        "deadline",
        re.compile(r"не\s+более\s+(\d+)\s+(?:календарных\s+)?дней", re.I),
        "не более {0} дней",
    ),
]


def _entities_in(sentence: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for etype, pat, template in _PATTERNS:
        for m in pat.finditer(sentence):
            if etype == "person":
                name = f"{_SURNAMES.get(m.group(1), m.group(1))} {m.group(2)}. {m.group(3)}."
            else:
                name = str(template).format(*m.groups())
            if (etype, name) not in out:
                out.append((etype, name))
    return out


def extract_graph(fragment: str) -> dict[str, Any]:
    entities: list[dict[str, str]] = []
    relations: list[dict[str, str]] = []
    for sentence in re.split(r"(?<![А-ЯЁ]\.)(?<=[.!?])\s+(?=[А-ЯЁ0-9])|\n+", fragment):
        found = _entities_in(sentence)
        for etype, name in found:
            if {"type": etype, "name": name} not in entities:
                entities.append({"type": etype, "name": name})
        of = {
            t: [n for tt, n in found if tt == t]
            for t in ("person", "position", "department", "equipment", "site", "work", "deadline")
        }
        low = sentence.lower()
        if "ответствен" in low or "возложить" in low:
            actors = of["person"] or of["position"]
            targets = of["work"] or of["department"] or of["equipment"]
            for a in actors:
                for t in targets:
                    relations.append({"subject": a, "predicate": "responsible_for", "object": t})
        for p in of["person"]:
            for pos in of["position"]:
                if pos != "ответственный руководитель работ" or len(of["position"]) == 1:
                    relations.append({"subject": p, "predicate": "holds_position", "object": pos})
        for pos in of["position"]:
            for d in of["department"]:
                if "службы" in pos and "служба" in d:
                    relations.append({"subject": pos, "predicate": "belongs_to", "object": d})
        for e in of["equipment"]:
            for site in of["site"]:
                relations.append({"subject": e, "predicate": "located_at", "object": site})
        for w in of["work"]:
            for dl in of["deadline"]:
                relations.append({"subject": w, "predicate": "has_deadline", "object": dl})
    return {"entities": entities, "relations": relations}


def _structured(model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    FAKE_OLLAMA.extract_calls += 1
    FAKE_OLLAMA.extract_prompts.append(messages)
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    fragment = user.split("Фрагмент:", 1)[-1]
    content = (
        "{не json"
        if FAKE_OLLAMA.bad_json
        else json.dumps(extract_graph(fragment), ensure_ascii=False)
    )
    return {
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": True,
        "eval_count": 120,
        "eval_duration": 120 * 30_000_000,
        "prompt_eval_count": 600,
        "prompt_eval_duration": 600 * 2_000_000,
    }
