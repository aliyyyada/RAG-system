"""Реранкер на onnxruntime: проверяем «обвязку» на крошечной модели.

Настоящая bge-reranker-v2-m3 весит сотни мегабайт и в CI не скачивается. Поэтому здесь
собирается игрушечная ONNX-модель с тем же интерфейсом (input_ids, attention_mask → logits
[batch, 1]): её оценка = сколько раз во фрагменте встречается слово «наряд» минус 0,5.
Так проверяется всё, что делает наш код: токенизация пар «вопрос — фрагмент»,
выравнивание длины (padding), обрезка, сигмоида и порядок результатов.
"""

from pathlib import Path

import pytest

onnx = pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")
pytest.importorskip("tokenizers")

from onnx import TensorProto, helper  # noqa: E402
from tokenizers import Tokenizer, models, pre_tokenizers, processors  # noqa: E402

from app.config import Settings  # noqa: E402
from app.search import rerank  # noqa: E402

VOCAB = {"<pad>": 0, "<s>": 1, "</s>": 2, "<unk>": 3, "наряд": 4, "допуск": 5, "кран": 6}


def _make_model(dir_: Path) -> None:
    tok = Tokenizer(models.WordLevel(VOCAB, unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.post_processor = processors.TemplateProcessing(
        single="<s> $A </s>",
        pair="<s> $A </s> </s> $B </s>",
        special_tokens=[("<s>", 1), ("</s>", 2)],
    )
    tok.save(str(dir_ / "tokenizer.json"))

    ids = helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["b", "l"])
    mask = helper.make_tensor_value_info("attention_mask", TensorProto.INT64, ["b", "l"])
    out = helper.make_tensor_value_info("logits", TensorProto.FLOAT, ["b", 1])
    nodes = [
        helper.make_node(
            "Constant", [], ["k"], value=helper.make_tensor("k", TensorProto.INT64, [], [4])
        ),
        helper.make_node("Equal", ["input_ids", "k"], ["eq"]),
        helper.make_node("Cast", ["eq"], ["eqf"], to=TensorProto.FLOAT),
        helper.make_node("Cast", ["attention_mask"], ["mf"], to=TensorProto.FLOAT),
        helper.make_node("Mul", ["eqf", "mf"], ["hits"]),
        helper.make_node(
            "Constant", [], ["ax"], value=helper.make_tensor("ax", TensorProto.INT64, [1], [1])
        ),
        helper.make_node("ReduceSum", ["hits", "ax"], ["sum"], keepdims=1),
        helper.make_node(
            "Constant", [], ["half"], value=helper.make_tensor("half", TensorProto.FLOAT, [], [0.5])
        ),
        helper.make_node("Sub", ["sum", "half"], ["logits"]),
    ]
    graph = helper.make_graph(nodes, "toy-reranker", [ids, mask], [out])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.save(model, str(dir_ / "model.onnx"))


async def test_onnx_reranker_scores_pairs(tmp_path: Path) -> None:
    _make_model(tmp_path)
    r = rerank.OnnxReranker(tmp_path, threads=1, max_tokens=64)
    scores = await r.score(
        "наряд допуск", ["кран кран", "наряд допуск наряд", "наряд", "что-то незнакомое"]
    )
    # «наряд» встречается: 1 раз в вопросе + 0 / 2 / 1 / 0 во фрагменте.
    assert scores[1] > scores[2] > scores[0]
    assert scores[0] == pytest.approx(scores[3])
    assert all(0 < s < 1 for s in scores)


async def test_long_fragment_is_truncated(tmp_path: Path) -> None:
    _make_model(tmp_path)
    r = rerank.OnnxReranker(tmp_path, threads=1, max_tokens=16)
    [score] = await r.score("кран", ["наряд " * 500])
    # Обрезано до 16 токенов: из них 4 служебных + 1 вопрос → 11 раз «наряд».
    assert score == pytest.approx(1 / (1 + pow(2.718281828, -(11 - 0.5))), rel=1e-3)


def test_missing_model_means_no_reranker(tmp_path: Path) -> None:
    rerank.set_reranker_override(None, active=False)
    rerank._loaded = False
    try:
        s = Settings(reranker="onnx", reranker_dir=str(tmp_path / "нет"))
        assert rerank.get_reranker(s) is None
        assert rerank.reranker_status(s) == {"enabled": False, "name": None}
    finally:
        rerank._loaded = False
        rerank._cached = None
