import json
from pathlib import Path
import tempfile
import unittest

from rag.embeddings import LocalEmbeddings, local_profile


class Encoder:
    def __init__(self): self.calls = []
    def encode(self, texts, **kwargs):
        self.calls.append((texts, kwargs))
        return [[1.0]+[0.0]*383 for _ in texts]


class EmbeddingTests(unittest.TestCase):
    def test_prefixes_batch_and_normalization(self):
        encoder = Encoder()
        model = LocalEmbeddings(encoder, local_profile('a'*40, {}), batch_size=8)
        self.assertEqual(len(model.embed_query('Вопрос')), 384)
        model.embed_documents(['Документ'])
        self.assertEqual(encoder.calls[0][0], ['query: Вопрос'])
        self.assertEqual(encoder.calls[1][0], ['passage: Документ'])
        self.assertTrue(encoder.calls[0][1]['normalize_embeddings'])
        self.assertEqual(encoder.calls[0][1]['batch_size'], 8)

    def test_empty_batch_never_calls_encoder(self):
        encoder = Encoder()
        model = LocalEmbeddings(encoder, local_profile('a'*40, {}))
        self.assertEqual(model.embed_documents([]), [])
        self.assertEqual(encoder.calls, [])

    def test_missing_cache_fails_before_import_or_download(self):
        from rag.embeddings import load_local_embeddings
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, 'bootstrap'):
                load_local_embeddings(Path(tmp))

    def test_invalid_vectors_rejected(self):
        class Bad:
            def encode(self, *args, **kwargs): return [[float('nan')]*384]
        model = LocalEmbeddings(Bad(), local_profile('a'*40, {}))
        with self.assertRaises(ValueError): model.embed_query('q')

    def test_factory_uses_local_cache_without_google(self):
        from rag.config import load_settings
        from rag.embeddings import create_embeddings
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings({'RAG_PROFILE': 'LOCAL', 'RAG_EMBEDDING_CACHE': tmp})
            with self.assertRaisesRegex(RuntimeError, 'bootstrap'):
                create_embeddings(settings)

    def test_bootstrap_requires_pinned_revision(self):
        from scripts.bootstrap_model import bootstrap_model
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'model'
            with self.assertRaises(ValueError): bootstrap_model(target, 'main')
            self.assertFalse(target.exists())
