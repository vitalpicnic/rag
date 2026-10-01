import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rag.embeddings import local_profile
from rag.index_store import build_version, activate_bundle, load_bundle, current_bundle, rollback_bundle


class Embedding:
    profile = local_profile('a'*40, {})
    def embed_documents(self, texts): return [[1.0]+[0.0]*383 for _ in texts]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'data/topic').mkdir(parents=True)
        (self.root/'data/topic/a.txt').write_text('Активы банка составляют 42 миллиарда.', encoding='utf-8')
        self.embedding = Embedding()

    def build(self): return build_version(self.root, 'topic', self.embedding)

    def test_build_and_activation_leave_legacy_untouched(self):
        legacy = self.root/'faiss_indexes/topic/current.json'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('legacy')
        bundle = self.build()
        self.assertFalse((legacy.parent/'v2/active.json').exists())
        activate_bundle(self.root, 'topic', bundle)
        self.assertEqual(current_bundle(self.root, 'topic'), bundle)
        index, rows = load_bundle(self.root, 'topic', bundle, self.embedding.profile)
        self.assertEqual(index.ntotal, len(rows))
        self.assertEqual(index.d, 384)
        self.assertEqual(legacy.read_text(), 'legacy')
        self.assertFalse(list((legacy.parent/'v2').rglob('*.pkl')))

    def test_same_dimension_different_profile_rejected(self):
        bundle = self.build()
        with self.assertRaises(ValueError):
            load_bundle(self.root, 'topic', bundle, {**self.embedding.profile, 'revision': 'b'*40})

    def test_interrupted_build_keeps_active(self):
        first = self.build()
        activate_bundle(self.root, 'topic', first)
        with patch.object(self.embedding, 'embed_documents', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError): self.build()
        self.assertEqual(current_bundle(self.root, 'topic'), first)

    def test_failed_pointer_replace_keeps_active(self):
        first, second = self.build(), self.build()
        activate_bundle(self.root, 'topic', first)
        with patch('rag.index_store.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): activate_bundle(self.root, 'topic', second)
        self.assertEqual(current_bundle(self.root, 'topic'), first)

    def test_failed_warmup_rolls_back(self):
        first, second = self.build(), self.build()
        activate_bundle(self.root, 'topic', first)
        with self.assertRaises(RuntimeError):
            activate_bundle(self.root, 'topic', second, warmup=lambda _: (_ for _ in ()).throw(RuntimeError('warmup')))
        self.assertEqual(current_bundle(self.root, 'topic'), first)
        activate_bundle(self.root, 'topic', second)
        rollback_bundle(self.root, 'topic', first)
        self.assertEqual(current_bundle(self.root, 'topic'), first)

    def test_changed_corpus_and_tampered_artifact_fail_closed(self):
        bundle = self.build()
        (self.root/'data/topic/a.txt').write_text('changed')
        with self.assertRaises(ValueError): activate_bundle(self.root, 'topic', bundle)

    def test_tampered_artifact_is_rejected_before_faiss_load(self):
        bundle = self.build()
        version = self.root/'faiss_indexes/topic/v2'/bundle['relative_path']
        (version/'index.faiss').write_bytes(b'bad')
        with patch('faiss.read_index', side_effect=AssertionError('must not load')):
            with self.assertRaises(ValueError): load_bundle(self.root, 'topic', bundle)

    def test_unsafe_version_path_rejected(self):
        bundle = self.build()
        with self.assertRaises(ValueError):
            load_bundle(self.root, 'topic', {**bundle, 'relative_path': '../outside'})

    def test_new_corpus_can_replace_stale_active_but_not_rollback(self):
        first = self.build()
        activate_bundle(self.root, 'topic', first)
        (self.root/'data/topic/a.txt').write_text('Новая отчётность банка.', encoding='utf-8')
        second = self.build()
        activate_bundle(self.root, 'topic', second)
        with self.assertRaises(ValueError): rollback_bundle(self.root, 'topic', first)
        self.assertEqual(current_bundle(self.root, 'topic'), second)

    def test_lock_prevents_concurrent_publish(self):
        bundle = self.build()
        lock = self.root/'faiss_indexes/topic/v2/build.lock'
        lock.write_text('another writer')
        with self.assertRaises(RuntimeError): activate_bundle(self.root, 'topic', bundle)
        self.assertEqual(lock.read_text(), 'another writer')

    def test_fsync_failure_keeps_pointer(self):
        first, second = self.build(), self.build()
        activate_bundle(self.root, 'topic', first)
        with patch('rag.index_store.os.fsync', side_effect=OSError('fsync failure')):
            with self.assertRaises(OSError): activate_bundle(self.root, 'topic', second)
        self.assertEqual(current_bundle(self.root, 'topic'), first)

    def test_parser_uses_the_same_bytes_that_were_verified(self):
        bundle = self.build()
        original = Path.read_text
        def changed_read(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path.name == 'chunks.jsonl':
                rows = [json.loads(line) for line in value.splitlines()]
                rows[0]['text'] = 'UNVERIFIED REPLACEMENT'
                return '\n'.join(json.dumps(row) for row in rows)
            return value
        with patch.object(Path, 'read_text', changed_read):
            _, rows = load_bundle(self.root, 'topic', bundle)
        self.assertNotEqual(rows[0]['text'], 'UNVERIFIED REPLACEMENT')
