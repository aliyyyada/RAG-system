"""Готовит реранкер bge-reranker-v2-m3 для офлайн-пакета (запускается на машине с интернетом).

Скачивает модель BAAI/bge-reranker-v2-m3 (Apache-2.0) с HuggingFace, сохраняет её в формате
ONNX и квантует веса в int8 (≈570 МБ вместо 2,2 ГБ, на CPU в 2–3 раза быстрее, качество
почти не меняется). В папку OUT кладутся два файла, которые читает app/search/rerank.py:

    model_quantized.onnx  — сама модель
    tokenizer.json        — токенизатор

Запуск (внутри контейнера python, см. export-reranker.sh):
    python export_reranker.py /out
"""

import sys
from pathlib import Path

import torch
from onnxruntime.quantization import QuantType, quantize_dynamic
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MODEL = "BAAI/bge-reranker-v2-m3"


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "fp32"
    tmp.mkdir(exist_ok=True)
    tok = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL).eval()

    sample = tok(["вопрос"], ["фрагмент документа"], return_tensors="pt")
    with torch.no_grad():
        torch.onnx.export(
            model,
            (sample["input_ids"], sample["attention_mask"]),
            str(tmp / "model.onnx"),
            input_names=["input_ids", "attention_mask"],
            output_names=["logits"],
            dynamic_axes={
                "input_ids": {0: "batch", 1: "seq"},
                "attention_mask": {0: "batch", 1: "seq"},
                "logits": {0: "batch"},
            },
            opset_version=17,
            dynamo=False,
        )
    quantize_dynamic(
        str(tmp / "model.onnx"),
        str(out / "model_quantized.onnx"),
        weight_type=QuantType.QInt8,
        use_external_data_format=False,
    )
    tok.backend_tokenizer.save(str(out / "tokenizer.json"))
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()

    # Самопроверка: релевантный фрагмент должен получить оценку выше нерелевантного.
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    t = Tokenizer.from_file(str(out / "tokenizer.json"))
    t.enable_padding(pad_id=t.token_to_id("<pad>"), pad_token="<pad>")
    enc = t.encode_batch(
        [
            (
                "Кто подписывает наряд-допуск?",
                "Наряд-допуск подписывает ответственный руководитель работ.",
            ),
            ("Кто подписывает наряд-допуск?", "Краны обслуживаются раз в квартал."),
        ]
    )
    sess = ort.InferenceSession(
        str(out / "model_quantized.onnx"), providers=["CPUExecutionProvider"]
    )
    logits = sess.run(
        None,
        {
            "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
        },
    )[0]
    print("Проверка реранкера (логиты):", logits.ravel().tolist())
    assert logits[0, 0] > logits[1, 0], "реранкер работает неправильно"


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "reranker"))
