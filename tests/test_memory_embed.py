import importlib
import sys
import unittest
from unittest import mock

from everthine import memory_embed


class TestEmbedTexts(unittest.TestCase):
    def test_uses_injected_fake_and_never_touches_real_model(self):
        # 11. After set_embed_fn(fake), embed_texts(["a","b"], "any-model")
        # returns exactly [fake("a"), fake("b")] and never touches the real model.
        def fake(text):
            return [float(len(text)), 0.0]

        memory_embed.set_embed_fn(fake)
        self.addCleanup(memory_embed.set_embed_fn, None)

        with mock.patch.object(
            memory_embed, "_get_model",
            side_effect=AssertionError("must not touch the real model"),
        ):
            result = memory_embed.embed_texts(["a", "b"], "any-model")

        self.assertEqual(result, [fake("a"), fake("b")])

    def test_set_embed_fn_none_restores_real_model_path(self):
        # 12. set_embed_fn(None) restores the real-model path: a counting fake
        # stops being called once reset. The real model is never actually
        # loaded -- _get_model itself is replaced with a stub.
        calls = []

        def fake(text):
            calls.append(text)
            return [1.0, 0.0]

        memory_embed.set_embed_fn(fake)
        self.addCleanup(memory_embed.set_embed_fn, None)

        memory_embed.embed_texts(["hello"], "any-model")
        self.assertEqual(calls, ["hello"])

        memory_embed.set_embed_fn(None)

        class _FakeVector(list):
            def tolist(self):
                return list(self)

        class _FakeModel:
            def encode(self, texts, **kwargs):
                calls.append("real-path")
                return [_FakeVector([0.0, 1.0]) for _ in texts]

        with mock.patch.object(
            memory_embed, "_get_model", return_value=_FakeModel(),
        ) as mocked_get_model:
            result = memory_embed.embed_texts(["world"], "any-model")

        mocked_get_model.assert_called_once_with("any-model")
        # the fake stopped being consulted -- only the real-path sentinel was added
        self.assertEqual(calls, ["hello", "real-path"])
        self.assertEqual(result, [[0.0, 1.0]])


class TestDeferredImport(unittest.TestCase):
    def test_import_succeeds_without_sentence_transformers(self):
        # 13. Deferred-import pin: importing everthine.memory_embed must succeed
        # even when sentence_transformers is not importable. _get_model itself
        # may raise in that state, but that is not under test here.
        with mock.patch.dict(sys.modules, {"sentence_transformers": None}):
            importlib.reload(memory_embed)  # must not raise
        importlib.reload(memory_embed)  # restore normal module state afterward
        self.assertTrue(hasattr(memory_embed, "embed_texts"))
        self.assertTrue(hasattr(memory_embed, "set_embed_fn"))


if __name__ == "__main__":
    unittest.main()
