import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from rag.resources import ModelOwnerLock, ResourceBusy, IndexCache
from rag.runtime import RagRuntime, RuntimeBusy


REQUEST = {'principal':'user-1','session':'one','topic':'bank','mode':'text','question':'Q'}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def runtime(self, handler, **kwargs):
        model = Mock(return_value=object())
        runtime = RagRuntime(self.root, model, handler, **kwargs)
        self.addCleanup(runtime.drain)
        return runtime, model

    def test_one_model_across_topics_and_session_identity(self):
        keys, models = [], []
        def handler(model, request, key):
            keys.append(key); models.append(model)
            return {'answer':'ok'}
        runtime, factory = self.runtime(handler)
        for changes in [{}, {'topic':'other'}, {'principal':'other'}, {'mode':'expert'}, {'session':'two'}]:
            self.assertEqual(runtime.query({**REQUEST,**changes})['answer'], 'ok')
        factory.assert_called_once()
        self.assertEqual(len(set(keys)),5)
        self.assertTrue(all(model is models[0] for model in models))

    def test_bounded_queue_and_timeout_hold_actual_slot(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def handler(*args):
            calls.append(1); entered.set(); release.wait(3)
            return {'answer':'ok'}
        runtime, _ = self.runtime(handler, wait_timeout=.05)
        self.addCleanup(release.set)
        first = runtime.submit(REQUEST)
        self.assertTrue(entered.wait(1))
        waiting = [runtime.submit(REQUEST) for _ in range(8)]
        with self.assertRaises(RuntimeBusy): runtime.submit(REQUEST)
        self.assertFalse(first.cancel())
        self.assertEqual(runtime.readiness()['active'],1)
        for future in waiting: future.cancel()
        release.set(); first.result(1)
        self.assertEqual(len(calls),1)

    def test_client_timeout_does_not_release_worker(self):
        release = threading.Event()
        runtime, _ = self.runtime(lambda *args: release.wait(2), wait_timeout=.02)
        self.addCleanup(release.set)
        with self.assertRaises(TimeoutError): runtime.query(REQUEST)
        self.assertEqual(runtime.readiness()['active'],1)
        release.set()

    def test_drain_excludes_new_requests_and_releases_owner(self):
        runtime, _ = self.runtime(lambda *args: {})
        with self.assertRaises(ResourceBusy): ModelOwnerLock(self.root).acquire()
        runtime.drain()
        with self.assertRaises(RuntimeBusy): runtime.query(REQUEST)
        with ModelOwnerLock(self.root): pass

    def test_hung_worker_closes_admission_and_calls_process_terminator(self):
        release, fatal = threading.Event(), threading.Event()
        runtime, _ = self.runtime(lambda *args: release.wait(2), hung_timeout=.04,
                                 terminate=lambda code: fatal.set())
        self.addCleanup(release.set)
        runtime.submit(REQUEST)
        self.assertTrue(fatal.wait(1))
        self.assertFalse(runtime.readiness()['ready'])
        with self.assertRaises(RuntimeBusy): runtime.submit(REQUEST)
        with self.assertRaises(ResourceBusy): ModelOwnerLock(self.root).acquire()
        release.set()

    def test_cache_evicts_lru_but_never_pinned_or_oversized(self):
        cache = IndexCache(limit=10)
        closed = []
        with cache.pin('a',6,lambda:'a',closed.append):
            with self.assertRaises(ResourceBusy):
                with cache.pin('b',6,lambda:'b',closed.append): pass
        with cache.pin('b',6,lambda:'b',closed.append): pass
        self.assertEqual(closed,['a'])
        load = Mock()
        with self.assertRaises(ResourceBusy):
            with cache.pin('large',11,load,closed.append): pass
        load.assert_not_called()
        cache.clear()
        self.assertEqual(closed,['a','b'])

    def test_invalid_request_never_reaches_model(self):
        handler = Mock()
        runtime, _ = self.runtime(handler)
        for changes in [{'topic':'../other'},{'question':''},{'mode':'bogus'},{'principal':''}]:
            with self.assertRaises(ValueError): runtime.query({**REQUEST,**changes})
        handler.assert_not_called()

    def test_index_admin_cannot_create_model_while_runtime_owns_lock(self):
        from unittest.mock import patch
        from scripts.index_admin import main
        runtime, _ = self.runtime(lambda *args: {})
        with patch('sys.argv', ['index_admin','build','--root',str(self.root),'--topic','bank',
                               '--output',str(self.root/'bundle.json'),'--maintenance']), \
                patch('scripts.index_admin.create_embeddings') as create:
            with self.assertRaises(ResourceBusy): main()
            create.assert_not_called()

    def test_queued_request_expires_without_execution(self):
        release, entered = threading.Event(), threading.Event()
        calls = []
        def handle(*args):
            calls.append(1); entered.set(); release.wait(2)
            return {}
        runtime, _ = self.runtime(handle,wait_timeout=.02)
        self.addCleanup(release.set)
        first = runtime.submit(REQUEST)
        self.assertTrue(entered.wait(1))
        second = runtime.submit(REQUEST)
        time.sleep(.04)
        release.set(); first.result(1)
        with self.assertRaises(RuntimeBusy): second.result(1)
        self.assertEqual(len(calls),1)

    def test_hung_worker_really_exits_child_process(self):
        import subprocess
        import sys
        script = """
import sys, time
from rag.runtime import RagRuntime
runtime = RagRuntime(sys.argv[1], lambda: object(), lambda *args: time.sleep(5), hung_timeout=.1)
runtime.submit({'principal':'u','session':'s','topic':'bank','mode':'text','question':'Q'})
time.sleep(3)
raise SystemExit(99)
"""
        result = subprocess.run([sys.executable,'-c',script,str(self.root)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,70,result.stderr.decode(errors='replace'))
        with ModelOwnerLock(self.root): pass

    def test_owner_lock_excludes_other_process(self):
        import subprocess
        import sys
        runtime, _ = self.runtime(lambda *args: {})
        script = """
import sys
from rag.resources import ModelOwnerLock, ResourceBusy
try:
    ModelOwnerLock(sys.argv[1]).acquire()
except ResourceBusy:
    raise SystemExit(42)
"""
        result = subprocess.run([sys.executable,'-c',script,str(self.root)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,42)

    def test_async_adapter_cancellation_keeps_inference_slot(self):
        import asyncio
        from rag.bot_support import query_runtime
        release, entered = threading.Event(), threading.Event()
        def handler(*args):
            entered.set(); release.wait(2)
            return {}
        runtime, _ = self.runtime(handler)
        self.addCleanup(release.set)
        async def scenario():
            task = asyncio.create_task(query_runtime(runtime,REQUEST))
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(runtime.readiness()['active'],1)
            release.set()
        asyncio.run(scenario())

    def test_failed_model_initialization_releases_owner(self):
        with self.assertRaises(ValueError):
            RagRuntime(self.root,Mock(side_effect=ValueError('initialization failed')),lambda *args: {})
        with ModelOwnerLock(self.root): pass

    def test_async_cancelled_waiter_never_starts_generation(self):
        import asyncio
        from rag.bot_support import query_runtime
        release, entered = threading.Event(), threading.Event()
        calls = []
        def handler(*args):
            calls.append(1); entered.set(); release.wait(2)
            return {}
        runtime, _ = self.runtime(handler)
        self.addCleanup(release.set)
        first = runtime.submit(REQUEST)
        self.assertTrue(entered.wait(1))
        async def scenario():
            task = asyncio.create_task(query_runtime(runtime,REQUEST))
            while runtime.readiness()['queued'] == 0: await asyncio.sleep(.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            await asyncio.sleep(0)
            release.set()
            while runtime.readiness()['queued'] or runtime.readiness()['active']:
                await asyncio.sleep(.001)
            await asyncio.to_thread(runtime.drain)
        asyncio.run(scenario())
        first.result(1)
        self.assertEqual(len(calls),1)


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        from rag.embeddings import local_profile
        from rag.index_store import build_version, activate_bundle
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        class Embedding:
            profile = local_profile('a'*40, {})
            def embed_documents(self, texts): return [[1.0]+[0.0]*383 for _ in texts]
            def embed_query(self, text): return [1.0]+[0.0]*383
        self.embedding = Embedding()
        for topic in ('bank','other'):
            (self.root/'data'/topic).mkdir(parents=True)
            (self.root/'data'/topic/'a.txt').write_text('Assets are 42.',encoding='utf-8')
            bundle = build_version(self.root,topic,self.embedding)
            activate_bundle(self.root,topic,bundle)

    def test_real_faiss_packing_pipeline_and_one_model_factory(self):
        from types import SimpleNamespace
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda
        from rag.engine import create_runtime
        from rag.token_budget import TokenContract, TokenCounter
        calls = []
        def generate(prompt, max_output_tokens):
            calls.append(prompt.to_messages())
            return AIMessage(content='Assets are 42 [S1].')
        generation = RunnableLambda(generate)
        factory = Mock(return_value=(self.embedding,generation))
        settings = SimpleNamespace(model='test',llm_provider='ollama',embedding_provider='local',profile='LOCAL')
        counter = TokenCounter(TokenContract('test','test-counter',16384,4096),
                               lambda messages: sum(len(m.content)+4 for m in messages))
        runtime = create_runtime(self.root,settings,counter,model_factory=factory)
        self.addCleanup(runtime.drain)
        for topic in ('bank','other'):
            answer = runtime.query({**REQUEST,'topic':topic})
            self.assertIn('42',answer['answer'])
            self.assertEqual(answer['sources'][0].metadata['index_version'],answer['index_version'])
            self.assertTrue(runtime.readiness(topic)['ready'])
        factory.assert_called_once()
        self.assertEqual(len(calls),2)
        runtime.query({**REQUEST,'principal':'another-user'})
        self.assertEqual(len(calls[-1]),2)
        runtime.query(REQUEST)
        self.assertEqual(len(calls[-1]),4)
        self.assertLessEqual(runtime.handler.cache.used,512*1024*1024)
        runtime.drain()
        self.assertEqual(runtime.handler.cache.used,0)

    def test_pointer_change_during_generation_discards_answer_and_history(self):
        from types import SimpleNamespace
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda
        from rag.engine import create_runtime
        from rag.runtime import RuntimeFailure
        from rag.token_budget import TokenContract, TokenCounter
        def generate(prompt, max_output_tokens):
            (self.root/'faiss_indexes/bank/v2/active.json').write_text('{}')
            return AIMessage(content='Assets are 42 [S1].')
        settings = SimpleNamespace(model='test',llm_provider='ollama',embedding_provider='local',profile='LOCAL')
        counter = TokenCounter(TokenContract('test','test-counter',16384,4096),
                               lambda messages: sum(len(m.content)+4 for m in messages))
        runtime = create_runtime(self.root,settings,counter,
                                 model_factory=lambda: (self.embedding,RunnableLambda(generate)))
        self.addCleanup(runtime.drain)
        with self.assertRaises(RuntimeFailure): runtime.query(REQUEST)
        self.assertEqual(len(runtime.handler.histories),0)
        self.assertFalse(runtime.readiness('bank')['ready'])

    def test_oversized_snapshot_is_rejected_before_faiss_load(self):
        from unittest.mock import patch
        from rag.resources import SnapshotCache
        snapshots = SnapshotCache(self.root,limit=1)
        with patch('rag.verification.load_bundle') as load:
            with self.assertRaises(ResourceBusy):
                with snapshots.pin('bank'): pass
            load.assert_not_called()


if __name__ == '__main__': unittest.main()
