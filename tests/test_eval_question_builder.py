import importlib
import warnings
from types import SimpleNamespace

import scripts.build_eval_questions as builder


def test_eval_concept_extraction_jieba_happy(monkeypatch):
    class FakePosseg:
        @staticmethod
        def cut(text):
            terms = [
                ("局部性原理", "n"),
                ("运动学正解", "n"),
                ("缓存命中率", "vn"),
                ("Backpropagation", "eng"),
                ("function", "eng"),
                ("that", "eng"),
                ("部定位方", "x"),
                ("的", "uj"),
            ]
            return [SimpleNamespace(word=word, flag=flag) for word, flag in terms]

    def fake_import(name):
        if name == "jieba.posseg":
            return FakePosseg
        return importlib.import_module(name)

    monkeypatch.setattr(builder.importlib, "import_module", fake_import)
    chunks = [
        {"text": "局部性原理 局部性原理 运动学正解 缓存命中率 Backpropagation 部定位方"},
        {"text": "局部性原理 运动学正解 运动学正解 缓存命中率 Backpropagation"},
        {"text": "局部性原理 运动学正解 缓存命中率 Backpropagation"},
    ]

    concepts = builder.extract_concepts(chunks, top_n=10)

    assert "局部性原理" in concepts
    assert "运动学正解" in concepts
    assert "缓存命中率" in concepts
    assert "Backpropagation" in concepts
    assert "function" not in concepts
    assert "that" not in concepts
    assert "部定位方" not in concepts


def test_eval_concept_extraction_jieba_missing_warns_and_falls_back(monkeypatch):
    def fake_import(name):
        if name == "jieba.posseg":
            raise ModuleNotFoundError("No module named 'jieba'")
        return importlib.import_module(name)

    monkeypatch.setattr(builder.importlib, "import_module", fake_import)
    chunks = [
        {"text": "局部性原理 局部性原理 局部性原理"},
        {"text": "缓存系统 缓存系统 缓存系统"},
    ]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        concepts = builder.extract_concepts(chunks, top_n=10)

    assert "局部性原" in concepts
    assert "缓存系统" in concepts
    assert any("jieba not available" in str(w.message) for w in caught)
