import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.runtime_deps'), str(ROOT / '.test_deps')]
from build_index import create_embeddings
from rag.index import IndexNotReady


class IndexConfigTests(unittest.TestCase):
    def test_missing_key_fails_before_initializing_provider(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(IndexNotReady, 'GOOGLE_API_KEY'):
                create_embeddings(Path(tmp))

    def test_env_from_project_root_configures_matching_embedding_space(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            (Path(tmp) / '.env').write_text('GOOGLE_API_KEY=not-a-real-api-key\n', encoding='utf-8')
            embedding = create_embeddings(Path(tmp))
            self.assertEqual(embedding.model, 'gemini-embedding-001')
            self.assertEqual(embedding.output_dimensionality, 3072)
            self.assertFalse(embedding.vertexai)
            self.assertIsNone(embedding.task_type)
            embedding.client.close()


if __name__ == '__main__':
    unittest.main()
