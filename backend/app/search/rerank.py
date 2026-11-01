"""Реранкер bge-reranker-v2-m3: переоценивает пары «вопрос — фрагмент».

Зачем. Гибридный поиск быстрый, но грубый: вектор сравнивает смысл вопроса и фрагмента
по отдельности, полнотекстовый — только слова. Реранкер (cross-encoder) читает вопрос
и фрагмент вместе и даёт оценку 0…1 «насколько фрагмент отвечает на вопрос».
Пример: на «кто подписывает наряд-допуск» фрагмент «наряд-допуск выдаётся на 15 дней»
получит меньше, чем «наряд-допуск … подписывает ответственный руководитель работ».

Как запускается. Ollama реранкеры не умеет (docs/open-questions.md, вопрос 11), поэтому
модель работает прямо в контейнере app через onnxruntime (MIT) на CPU: квантованная
int8-модель ~570 МБ, на i3-12100 ~2–4 с на 20 фрагментов (оценка, замер — этап 8).
Файлы модели (model.onnx или model_quantized.onnx + tokenizer.json) кладёт в
RERANKER_DIR офлайн-установщик (scripts/offline/export-reranker.sh готовит их на машине
с интернетом). Нет файлов — поиск работает без реранкера (порядок RRF), в журнал
пишется предупреждение, в «Состоянии системы» будет видно.
"""

import asyncio
import logging
import math
import threading
from pathlib import Path
from typing import Any, Protocol

from app.config import Settings

log = logging.getLogger(__name__)

MODEL_FILES = ("model_quantized.onnx", "model.onnx")


class Reranker(Protocol):
    name: str

    async def score(self, query: str, texts: list[str]) -> list[float]: ...


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


class OnnxReranker:
    """Cross-encoder в формате ONNX + токенизатор HuggingFace (tokenizer.json)."""

    name = "bge-reranker-v2-m3 (onnx, CPU)"

    def __init__(self, model_dir: Path, *, threads: int, max_tokens: int) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model = next((model_dir / f for f in MODEL_FILES if (model_dir / f).exists()), None)
        if model is None:
            raise FileNotFoundError(f"В {model_dir} нет {' или '.join(MODEL_FILES)}")
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=max_tokens, strategy="only_second")
        pad_token = "<pad>" if self.tokenizer.token_to_id("<pad>") is not None else "[PAD]"
        pad_id = self.tokenizer.token_to_id(pad_token)
        self.tokenizer.enable_padding(pad_id=pad_id or 0, pad_token=pad_token)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.inputs = {i.name for i in self.session.get_inputs()}
        # Одна оценка за раз: параллельные запуски на 4 ядрах только мешают друг другу.
        self._lock = threading.Lock()
        log.info("Реранкер загружен: %s", model)

    def _score_sync(self, query: str, texts: list[str]) -> list[float]:
        import numpy as np

        enc = self.tokenizer.encode_batch([(query, t) for t in texts])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        feed = {k: v for k, v in feed.items() if k in self.inputs}
        with self._lock:
            logits = self.session.run(None, feed)[0]
        flat = np.asarray(logits, dtype=np.float64).reshape(len(texts), -1)[:, 0]
        return [_sigmoid(float(x)) for x in flat]

    async def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        return await asyncio.to_thread(self._score_sync, query, texts)


_override: Reranker | None = None
_override_set = False
_cached: Reranker | None = None
_loaded = False
_load_lock = threading.Lock()


def set_reranker_override(reranker: Reranker | None, *, active: bool = True) -> None:
    """Тесты: подменить реранкер (active=False — вернуть обычную загрузку)."""
    global _override, _override_set
    _override, _override_set = reranker, active


def get_reranker(settings: Settings) -> Reranker | None:
    """Реранкер или None (выключен в настройках или нет файлов модели)."""
    global _cached, _loaded
    if _override_set:
        return _override
    if settings.reranker == "none":
        return None
    if _loaded:
        return _cached
    with _load_lock:
        if not _loaded:
            try:
                _cached = OnnxReranker(
                    Path(settings.reranker_dir),
                    threads=settings.reranker_threads,
                    max_tokens=settings.reranker_max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 — без реранкера поиск всё равно работает
                log.warning("Реранкер не загружен (%s), поиск без него", exc)
                _cached = None
            _loaded = True
    return _cached


def reranker_status(settings: Settings) -> dict[str, Any]:
    r = get_reranker(settings)
    return {"enabled": r is not None, "name": r.name if r else None}
