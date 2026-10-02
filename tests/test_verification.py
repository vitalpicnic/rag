from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from rag.embeddings import local_profile
from rag.index_store import build_version, activate_bundle
from rag.verification import SnapshotVerifier, VerificationRequired


class Embedding:
    profile = local_profile('a'*40, {})
    def embed_documents(self, texts): return [[1.0]+[0.0]*383 for _ in texts]
    def embed_query(self, text): return [1.0]+[0.0]*383


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'data/topic').mkdir(parents=True)
        self.source = self.root/'data/topic/a.txt'
        self.source.write_text('Bank assets are 42.', encoding='utf-8')
        self.bundle = build_version(self.root, 'topic', Embedding())
        activate_bundle(self.root, 'topic', self.bundle)
        self.now = 0
        self.verifier = SnapshotVerifier(self.root, clock=lambda: self.now)

    def test_warm_queries_do_not_hash_or_scan(self):
        first = self.verifier.acquire('topic')
        with patch('rag.verification.load_bundle', side_effect=AssertionError('full load')), \
                patch.object(self.verifier, '_signatures', side_effect=AssertionError('scan')):
            for _ in range(100):
                self.assertIs(self.verifier.acquire('topic'), first)

    def test_concurrent_startup_coalesces_verify(self):
        from rag.index_store import load_bundle
        with patch('rag.verification.load_bundle', wraps=load_bundle) as load:
            with ThreadPoolExecutor(4) as pool:
                results = list(pool.map(lambda _: self.verifier.acquire('topic'), range(8)))
            self.assertEqual(load.call_count, 1)
        self.assertTrue(all(item is results[0] for item in results))

    def test_new_pointer_invalidates_inflight(self):
        old = self.verifier.acquire('topic')
        new = build_version(self.root, 'topic', Embedding())
        activate_bundle(self.root, 'topic', new)
        with self.assertRaises(VerificationRequired): self.verifier.validate_lease(old)
        self.assertNotEqual(self.verifier.acquire('topic').bundle, old.bundle)

    def test_expiry_fail_closed_without_synchronous_scan(self):
        old = self.verifier.acquire('topic')
        self.now = 7200
        with patch('rag.verification.load_bundle', side_effect=AssertionError('no request scan')):
            with self.assertRaises(VerificationRequired): self.verifier.acquire('topic')
            with self.assertRaises(VerificationRequired): self.verifier.validate_lease(old)

    def test_watcher_rejects_changed_corpus(self):
        old = self.verifier.acquire('topic')
        self.source.write_text('changed', encoding='utf-8')
        self.now = 30
        self.verifier.maintenance()
        with self.assertRaises(VerificationRequired): self.verifier.validate_lease(old)

    def test_stat_preserving_tamper_detected_periodically(self):
        import os
        old = self.verifier.acquire('topic')
        stat = self.source.stat()
        self.source.write_bytes(b'X'*stat.st_size)
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.now = 3600
        self.verifier.maintenance()
        with self.assertRaises(VerificationRequired): self.verifier.acquire('topic')
        with self.assertRaises(VerificationRequired): self.verifier.validate_lease(old)

    def test_explicit_invalidation_revokes_lease(self):
        old = self.verifier.acquire('topic')
        self.verifier.invalidate('topic', 'policy revoked')
        with self.assertRaises(VerificationRequired): self.verifier.validate_lease(old)
        new = self.verifier.full_verify('topic')
        self.assertIsNot(new, old)

    def test_scan_race_never_publishes_cache(self):
        from rag.index_store import load_bundle
        def racing(*args):
            result = load_bundle(*args)
            self.source.write_text('changed after verification', encoding='utf-8')
            return result
        with patch('rag.verification.load_bundle', side_effect=racing):
            with self.assertRaises(VerificationRequired): self.verifier.acquire('topic')

    def test_old_lease_cannot_revoke_new_snapshot(self):
        old = self.verifier.acquire('topic')
        activate_bundle(self.root, 'topic', build_version(self.root, 'topic', Embedding()))
        new = self.verifier.acquire('topic')
        with self.assertRaises(VerificationRequired): self.verifier.validate_lease(old)
        self.verifier.validate_lease(new)

    def test_failed_periodic_scan_is_not_retried_every_second(self):
        self.verifier.acquire('topic')
        self.now = 3600
        with patch('rag.verification.load_bundle', side_effect=ValueError('bad')) as load:
            self.verifier.maintenance()
            self.now += 1
            self.verifier.maintenance()
            self.assertEqual(load.call_count, 1)

    def test_verified_retriever_reuses_store_and_preserves_provenance(self):
        from rag.index import VerifiedIndexRetriever
        retriever = VerifiedIndexRetriever(self.verifier, 'topic', Embedding())
        first = retriever.invoke('assets')
        self.assertEqual(first[0].metadata['source'], 'data/topic/a.txt')
        self.assertEqual(first[0].metadata['embedding_model'], Embedding.profile['model'])
        with patch('rag.verification.load_bundle', side_effect=AssertionError('reloaded')):
            self.assertEqual(retriever.invoke('assets')[0].page_content, first[0].page_content)

    def test_failed_new_pointer_not_retried_by_each_request(self):
        self.verifier.acquire('topic')
        next_bundle = build_version(self.root, 'topic', Embedding())
        activate_bundle(self.root, 'topic', next_bundle)
        with patch('rag.verification.load_bundle', side_effect=ValueError('bad')) as load:
            for _ in range(3):
                with self.assertRaises(VerificationRequired): self.verifier.acquire('topic')
            self.assertEqual(load.call_count, 1)

    def test_new_verifier_does_not_trust_previous_instance_lease(self):
        old = self.verifier.acquire('topic')
        restarted = SnapshotVerifier(self.root, clock=lambda: self.now)
        with self.assertRaises(VerificationRequired): restarted.validate_lease(old)

    def test_new_pointer_concurrent_requests_share_one_scan(self):
        self.verifier.acquire('topic')
        activate_bundle(self.root, 'topic', build_version(self.root, 'topic', Embedding()))
        from rag.index_store import load_bundle
        with patch('rag.verification.load_bundle', wraps=load_bundle) as load:
            with ThreadPoolExecutor(4) as pool:
                results = list(pool.map(lambda _: self.verifier.acquire('topic'), range(8)))
            self.assertEqual(load.call_count, 1)
        self.assertTrue(all(item is results[0] for item in results))

    def test_background_lifecycle_has_single_pair_of_workers(self):
        self.verifier.start()
        threads = list(self.verifier._threads)
        try:
            self.verifier.start()
            self.assertEqual(self.verifier._threads, threads)
            self.assertTrue(all(thread.is_alive() for thread in threads))
        finally:
            self.verifier.close()
        self.assertFalse(any(thread.is_alive() for thread in threads))
