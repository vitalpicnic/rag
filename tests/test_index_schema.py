from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from rag.index import DEFAULT_SETTINGS
from rag.index_schema import corpus_id, chunks_id, embedding_profile_id, prepare_chunks


class SchemaTests(unittest.TestCase):
    def test_corpus_order_independent_and_content_sensitive(self):
        files = [{'source': 'data/t/a.txt', 'sha256': 'a'*64},
                 {'source': 'data/t/b.txt', 'sha256': 'b'*64}]
        self.assertEqual(corpus_id(files), corpus_id(files[::-1]))
        modified = deepcopy(files)
        modified[0]['sha256'] = 'c'*64
        self.assertNotEqual(corpus_id(files), corpus_id(modified))

    def test_llm_and_embedding_do_not_change_chunks(self):
        settings = deepcopy(DEFAULT_SETTINGS)
        first = chunks_id('a'*64, settings)
        settings['llm'] = 'another-model'
        settings['embedding']['model'] = 'another-embedding'
        self.assertEqual(first, chunks_id('a'*64, settings))
        settings['chunking']['size'] = 500
        self.assertNotEqual(first, chunks_id('a'*64, settings))

    def test_embedding_identity_requires_revision_and_changes_with_encoding(self):
        from rag.embeddings import local_profile
        profile = local_profile('a'*40, {'torch':'test', 'sentence-transformers':'test'})
        original = embedding_profile_id(profile)
        for field, value in [('revision', 'b'*40), ('query_prefix', 'different: '),
                             ('normalization', False), ('max_length', 256), ('pooling', 'cls')]:
            changed = {**profile, field: value}
            self.assertNotEqual(original, embedding_profile_id(changed), field)
        with self.assertRaises(ValueError):
            local_profile('main', {})

    def test_prepare_reuses_chunks_across_embeddings_without_touching_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'data/topic').mkdir(parents=True)
            (root/'data/topic/a.txt').write_text('Банк увеличил активы до 42 миллиардов.', encoding='utf-8')
            first = prepare_chunks(root, 'topic')
            settings = deepcopy(DEFAULT_SETTINGS)
            settings['embedding']['model'] = 'local'
            with patch('rag.index_schema.extract_chunks', side_effect=AssertionError('must reuse')):
                second = prepare_chunks(root, 'topic', settings)
            self.assertEqual(first['chunks_id'], second['chunks_id'])
            self.assertFalse((root/'faiss_indexes').exists())
            chunks = Path(first['path'])/'chunks.jsonl'
            chunks.write_text('corruption', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'corrupt'):
                prepare_chunks(root, 'topic')

    def test_unsafe_topic_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                prepare_chunks(Path(tmp), '../outside')

    def test_linked_corpus_entry_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'data/topic').mkdir(parents=True)
            (root/'data/topic/a.txt').write_text('evidence')
            with patch.object(Path, 'is_symlink', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'Linked'):
                    prepare_chunks(root, 'topic')
