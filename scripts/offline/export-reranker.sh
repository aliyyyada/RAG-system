#!/usr/bin/env bash
# Готовит реранкер bge-reranker-v2-m3 (ONNX, int8) в папку $1 (по умолчанию ./offline-bundle/reranker).
# Нужны интернет и Docker. Вызывается из build-bundle.sh; можно запустить и отдельно.
set -euo pipefail
cd "$(dirname "$0")"
OUT="$(mkdir -p "${1:-../../offline-bundle/reranker}" && cd "${1:-../../offline-bundle/reranker}" && pwd)"
# RERANKER_PLATFORM — платформа контейнера (build-bundle.sh передаёт платформу этой машины,
# чтобы torch на Mac с Apple Silicon не работал через эмуляцию x86).
docker run --rm --platform "${RERANKER_PLATFORM:-linux/$(docker version --format '{{.Server.Arch}}')}" \
  -v "$(pwd)/export_reranker.py:/work/export_reranker.py:ro" \
  -v "$OUT:/out" \
  python:3.12-slim-bookworm bash -c "
    pip install -q --index-url https://download.pytorch.org/whl/cpu torch==2.15.0 &&
    pip install -q transformers==5.22.1 onnx==1.23.2 onnxruntime==1.31.1 sentencepiece protobuf &&
    python /work/export_reranker.py /out"
ls -lh "$OUT"
