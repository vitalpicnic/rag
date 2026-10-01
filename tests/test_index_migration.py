from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from rag.index import DEFAULT_SETTINGS, build_index
from rag.index_schema import embedding_profile_id
from scripts.migrate_index import migrate_trusted_legacy


class LegacyEmbedding:
    def embed_documents(self, texts): return [[1.0, 0.0] for _ in texts]
    def embed_query(self, text): return [1.0, 0.0]
    def __call__(self, text): return self.embed_query(text)


class MigrationTests(unittest.TestCase):
    def test_untrusted_input_rejected_before_loading_pickle(self):
        with self.assertRaises(ValueError):
            migrate_trusted_legacy(Path('missing'), 'topic')

    def test_trusted_mapping_and_vectors_preserved_without_overwriting_v1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'data/topic').mkdir(parents=True)
            (root/'data/topic/a.txt').write_text('Bank assets are 42.', encoding='utf-8')
            settings = deepcopy(DEFAULT_SETTINGS)
            settings['embedding']['dimensions'] = 2
            build_index(root, 'topic', LegacyEmbedding(), settings)
            pointer = root/'faiss_indexes/topic/current.json'
            before = pointer.read_bytes()
            bundle = migrate_trusted_legacy(root, 'topic', trusted=True, settings=settings)
            from rag.index_store import load_bundle
            index, rows = load_bundle(root, 'topic', bundle)
            self.assertEqual(index.reconstruct(0).tolist(), [1.0, 0.0])
            self.assertIn('Bank assets', rows[0]['text'])
            self.assertEqual(pointer.read_bytes(), before)
            self.assertFalse((pointer.parent/'v2/active.json').exists())
