"""Замер скорости и расхода VRAM моделей Ollama на нашей видеокарте.

Запускать на сервере завода, когда система поднята с пробросом порта Ollama
на localhost (docker-compose.dev.yml):
    python scripts/bench/ollama_bench.py
Без аргументов меряет LLM_MODEL и LLM_FALLBACK_MODEL с несколькими num_ctx.

Что выводит по каждой комбинации «модель × num_ctx»:
- tokens/s генерации и время до первого токена;
- размер модели в VRAM и долю, загруженную на GPU (из /api/ps).
  Если size_vram < size — часть слоёв ушла на CPU, и скорость резко падает:
  значит, num_ctx слишком велик для 8 ГБ.

Только стандартная библиотека Python: скрипт можно запустить где угодно без pip.
"""

import argparse
import json
import os
import time
import urllib.request

PROMPT = (
    "Ты помощник по документам завода. Кратко перечисли пять типичных требований "
    "к допуску персонала к работам на высоте."
)


def call(url: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url + path, data=data, headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=600) as r:  # noqa: S310 — локальный Ollama
        return json.loads(r.read())


def bench(url: str, model: str, num_ctx: int, embed_model: str | None) -> None:
    if embed_model:
        call(url, "/api/embed", {"model": embed_model, "input": "прогрев"})  # держим bge-m3 рядом
    t0 = time.monotonic()
    res = call(
        url,
        "/api/generate",
        {
            "model": model,
            "prompt": PROMPT,
            "stream": False,
            "options": {"num_ctx": num_ctx, "num_predict": 200, "temperature": 0},
        },
    )
    wall = time.monotonic() - t0
    ps = {m["name"]: m for m in call(url, "/api/ps").get("models", [])}
    m = ps.get(model) or next((v for k, v in ps.items() if k.startswith(model)), {})
    tps = res["eval_count"] / (res["eval_duration"] / 1e9) if res.get("eval_duration") else 0
    first = (res.get("load_duration", 0) + res.get("prompt_eval_duration", 0)) / 1e9
    gb = 1024**3
    total_vram = sum(v.get("size_vram", 0) for v in ps.values()) / gb
    print(
        f"{model:32} ctx={num_ctx:<6} {tps:6.1f} tok/s  first={first:5.1f}s  wall={wall:5.1f}s  "
        f"model={m.get('size', 0) / gb:4.1f}GB on_gpu={m.get('size_vram', 0) / gb:4.1f}GB  "
        f"all_models_vram={total_vram:4.1f}GB"
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    p.add_argument(
        "--models",
        nargs="*",
        default=[
            os.environ.get("LLM_MODEL", "qwen2.5:7b-instruct-q4_K_M"),
            os.environ.get("LLM_FALLBACK_MODEL", "qwen3:4b"),
        ],
    )
    p.add_argument("--ctx", nargs="*", type=int, default=[4096, 8192, 12288, 16384])
    p.add_argument(
        "--embed",
        default=os.environ.get("EMBEDDING_MODEL", "bge-m3"),
        help="модель эмбеддингов, которая должна помещаться в VRAM вместе с LLM",
    )
    a = p.parse_args()
    for model in a.models:
        for ctx in a.ctx:
            try:
                bench(a.url, model, ctx, a.embed)
            except Exception as e:  # noqa: BLE001
                print(f"{model} ctx={ctx}: ошибка {e}")


if __name__ == "__main__":
    main()
