import copy
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.runtime_deps'), str(ROOT / '.test_deps')]
from langchain_core.embeddings import Embeddings
from rag.index import (DEFAULT_SETTINGS, IndexNotReady, IndexChanged, build_index,
                       inspect_corpus, load_index, current_index, IndexRetriever, prepare_corpus)


class LocalEmbeddings(Embeddings):
    """Deterministic vectors for lifecycle tests, never used for the real corpus."""
    def __init__(self):
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        return [float(text.count('alpha')), float(text.count('beta')), 1.0]


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data' / 'topic'
        self.data.mkdir(parents=True)
        (self.data / 'a.txt').write_text('alpha information', encoding='utf-8')
        self.embeddings = LocalEmbeddings()
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.settings['embedding']['dimensions'] = 3

    def build(self, **kwargs):
        return build_index(self.root, 'topic', self.embeddings, self.settings, **kwargs)

    def test_build_has_relative_sources_stable_ids_and_noop_reuses_embeddings(self):
        result = self.build()
        version, manifest = current_index(self.root, 'topic', self.settings)
        records = [json.loads(line) for line in (version / 'chunks.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(records[0]['metadata']['source'], 'data/topic/a.txt')
        self.assertTrue(records[0]['metadata']['source_id'])
        self.assertEqual(manifest['document_count'], 1)
        calls = self.embeddings.calls
        again = self.build()
        self.assertEqual(again['status'], 'unchanged')
        self.assertEqual(self.embeddings.calls, calls)
        self.assertEqual(again['version'], result['version'])
        index = load_index(self.root, 'topic', self.embeddings, self.settings)
        self.assertEqual(index.similarity_search('alpha')[0].metadata['source'],
                         str(self.data / 'a.txt'))

    def test_added_modified_and_deleted_files_invalidate_and_rebuild(self):
        self.build()
        (self.data / 'b.txt').write_text('beta information', encoding='utf-8')
        with self.assertRaises(IndexChanged):
            current_index(self.root, 'topic', self.settings)
        self.build()
        (self.data / 'a.txt').write_text('alpha revised', encoding='utf-8')
        with self.assertRaises(IndexChanged):
            current_index(self.root, 'topic', self.settings)
        self.build()
        (self.data / 'a.txt').unlink()
        self.build()
        index = load_index(self.root, 'topic', self.embeddings, self.settings)
        self.assertEqual(index.index.ntotal, 1)
        self.assertTrue(index.similarity_search('beta')[0].metadata['source'].endswith('b.txt'))

    def test_embedding_failure_preserves_current_pointer(self):
        self.build()
        pointer = self.root / 'faiss_indexes/topic/current.json'
        before = pointer.read_bytes()
        (self.data / 'a.txt').write_text('beta update', encoding='utf-8')
        with patch.object(self.embeddings, 'embed_documents', side_effect=RuntimeError('API failure')):
            with self.assertRaises(RuntimeError):
                self.build()
        self.assertEqual(pointer.read_bytes(), before)
        self.assertFalse((pointer.parent / 'build.lock').exists())
        self.assertEqual(self.build()['status'], 'built')

    def test_publish_failure_preserves_current_pointer(self):
        self.build()
        pointer = self.root / 'faiss_indexes/topic/current.json'
        before = pointer.read_bytes()
        (self.data / 'a.txt').write_text('beta update', encoding='utf-8')
        with patch('rag.index.publish_current', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual(pointer.read_bytes(), before)

    def test_corpus_changed_during_embedding_is_not_published(self):
        original = self.embeddings.embed_documents
        def mutate(texts):
            vectors = original(texts)
            (self.data / 'a.txt').write_text('changed concurrently', encoding='utf-8')
            return vectors
        with patch.object(self.embeddings, 'embed_documents', side_effect=mutate):
            with self.assertRaises(IndexChanged):
                self.build()
        self.assertFalse((self.root / 'faiss_indexes/topic/current.json').exists())

    def test_incompatible_settings_and_corrupted_artifacts_are_rejected(self):
        self.build()
        changed = copy.deepcopy(self.settings)
        changed['chunking']['overlap'] = 20
        with self.assertRaises(IndexChanged):
            current_index(self.root, 'topic', changed)
        version, _ = current_index(self.root, 'topic', self.settings)
        (version / 'index.pkl').write_bytes(b'corrupted')
        with self.assertRaises(IndexNotReady):
            load_index(self.root, 'topic', self.embeddings, self.settings)

    def test_bad_dimension_and_concurrent_builder_are_rejected(self):
        wrong = copy.deepcopy(self.settings)
        wrong['embedding']['dimensions'] = 4
        with self.assertRaises(IndexNotReady):
            build_index(self.root, 'topic', self.embeddings, wrong)
        base = self.root / 'faiss_indexes/topic'
        (base / 'build.lock').write_text('other process', encoding='utf-8')
        with self.assertRaises(IndexNotReady):
            self.build()

    def test_runtime_reloads_new_version_and_rejects_stale_corpus(self):
        self.build()
        retriever = IndexRetriever(self.root, 'topic', self.embeddings, self.settings)
        self.assertIn('alpha', retriever.invoke('alpha')[0].page_content)
        (self.data / 'a.txt').write_text('beta updated', encoding='utf-8')
        with self.assertRaises(IndexChanged):
            retriever.invoke('alpha')
        self.build()
        self.assertIn('beta', retriever.invoke('beta')[0].page_content)

    def test_topic_cannot_escape_data_directory(self):
        for topic in ('../outside', '.', 'a/b', 'a\\b'):
            with self.assertRaises(IndexNotReady):
                inspect_corpus(self.root, topic, self.settings)

    def test_retrieval_mode_expands_candidates_and_preserves_distance_provenance(self):
        self.build()
        retriever = IndexRetriever(self.root, 'topic', self.embeddings, self.settings)
        result = retriever.invoke('alpha')
        self.assertEqual(result[0].metadata['retrieval_metric'], 'squared_l2')
        self.assertEqual(result[0].metadata['retrieval_distance'], 0.0)
        self.assertIn('index_fingerprint', result[0].metadata)
        self.assertIn('title', result[0].metadata)
        with patch.object(retriever._store, 'similarity_search_with_score',
                          wraps=retriever._store.similarity_search_with_score) as search:
            retriever.invoke('alpha', config={'metadata': {'retrieval_mode': 'overview'}})
            self.assertEqual(search.call_args.kwargs['k'], 24)
            retriever.invoke('alpha')
            self.assertEqual(search.call_args.kwargs['k'], 12)

    def test_index_can_be_moved_with_corpus_to_another_root(self):
        self.build()
        with tempfile.TemporaryDirectory() as other:
            target = Path(other)
            shutil.copytree(self.root / 'data', target / 'data')
            shutil.copytree(self.root / 'faiss_indexes', target / 'faiss_indexes')
            index = load_index(target, 'topic', self.embeddings, self.settings)
            source = index.similarity_search('alpha')[0].metadata['source']
            self.assertEqual(source, str(target / 'data/topic/a.txt'))

    def test_atomic_replace_error_leaves_old_pointer_and_no_temp_pointer(self):
        self.build()
        base = self.root / 'faiss_indexes/topic'
        before = (base / 'current.json').read_bytes()
        (self.data / 'a.txt').write_text('new beta', encoding='utf-8')
        with patch('rag.index.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual((base / 'current.json').read_bytes(), before)
        self.assertEqual(list(base.glob('current-*.tmp')), [])

    def test_malformed_pointer_and_empty_corpus_fail_closed(self):
        self.build()
        pointer = self.root / 'faiss_indexes/topic/current.json'
        original = pointer.read_bytes()
        pointer.write_text('[]', encoding='utf-8')
        with self.assertRaises(IndexNotReady):
            current_index(self.root, 'topic', self.settings)
        pointer.write_bytes(original)
        (self.data / 'a.txt').unlink()
        with self.assertRaises(IndexNotReady):
            self.build()
        self.assertEqual(pointer.read_bytes(), original)

    def test_prepared_corpus_has_no_vectors_and_build_reuses_verified_chunks(self):
        prepared = prepare_corpus(self.root, 'topic', self.settings)
        self.assertEqual(prepared['chunk_count'], 1)
        self.assertFalse((self.root / 'faiss_indexes/topic/current.json').exists())
        with patch('rag.index.extract_chunks', side_effect=AssertionError('must reuse prepared chunks')):
            self.assertEqual(self.build()['status'], 'built')

    def test_corrupted_preparation_is_rejected(self):
        prepared = prepare_corpus(self.root, 'topic', self.settings)
        (Path(prepared['path']) / 'chunks.jsonl').write_text('broken', encoding='utf-8')
        with self.assertRaises(IndexNotReady):
            self.build()

    def test_cyrillic_topic_and_filename_can_be_saved_and_loaded(self):
        data = self.root / 'data' / 'Банкинг'
        data.mkdir()
        (data / 'данные.txt').write_text('alpha банк', encoding='utf-8')
        build_index(self.root, 'Банкинг', self.embeddings, self.settings)
        index = load_index(self.root, 'Банкинг', self.embeddings, self.settings)
        self.assertEqual(index.index.ntotal, 1)
        self.assertEqual(index.similarity_search('alpha')[0].page_content, 'alpha банк')


if __name__ == '__main__':
    unittest.main()
