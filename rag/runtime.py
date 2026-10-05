"""One model owner and one actual execution across all trusted interface adapters."""
from collections import deque
from concurrent.futures import Future, TimeoutError
import os
from threading import Condition, Event, Thread
import time

from rag.resources import ModelOwnerLock


class RuntimeBusy(RuntimeError):
    pass


class RuntimeFailure(RuntimeError):
    pass


class RagRuntime:
    def __init__(self, root, model_factory, handler, *, wait_timeout=60,
                 hung_timeout=180, terminate=os._exit):
        if not 0 < wait_timeout <= 60 or hung_timeout <= 0:
            raise ValueError('Invalid runtime deadlines')
        self._condition = Condition()
        self._queue = deque()
        self._accepting, self._active = True, False
        self._started = 0
        self._stop = Event()
        self._closed = False
        self.wait_timeout, self.hung_timeout = wait_timeout, hung_timeout
        self._terminate, self.handler = terminate, handler
        self.owner = ModelOwnerLock(root).acquire()
        try:
            self.model = model_factory()
        except BaseException:
            self.owner.close()
            raise
        self._worker = Thread(target=self._run, name='rag-runtime', daemon=True)
        self._watchdog = Thread(target=self._watch, name='rag-watchdog', daemon=True)
        self._worker.start(); self._watchdog.start()

    @staticmethod
    def _request(request):
        from rag.index import _paths, IndexNotReady
        from rag.analysis import response_instructions
        if not isinstance(request, dict): raise ValueError('Invalid request')
        fields = ('principal','session','topic','mode','question')
        result = {field: request.get(field) for field in fields}
        if any(not isinstance(v, str) or not v for v in result.values()):
            raise ValueError('Request identity and question are required')
        if len(result['question']) > 8000 or any(len(result[k]) > 128 for k in fields[:-1]):
            raise ValueError('Request field exceeds limit')
        try:
            _paths('.', result['topic'])
        except IndexNotReady:
            raise ValueError('Invalid topic') from None
        response_instructions(result['mode'])
        return result

    def submit(self, request):
        request = self._request(request)
        future = Future()
        with self._condition:
            if not self._accepting or len(self._queue) >= 8:
                raise RuntimeBusy('Runtime unavailable or queue full')
            self._queue.append((future, request, time.monotonic()+self.wait_timeout))
            self._condition.notify()
        return future

    def query(self, request):
        future = self.submit(request)
        try:
            return future.result(timeout=self.wait_timeout)
        except TimeoutError:
            future.cancel()  # Running work cannot be cancelled and still occupies the slot.
            raise

    def _run(self):
        while True:
            # Verification is serialized with model work; watchdog also covers its I/O.
            if hasattr(self.handler, 'maintenance'):
                with self._condition:
                    if not self._accepting and not self._queue: return
                    self._active, self._started = True, time.monotonic()
                try:
                    self.handler.maintenance()
                except Exception:
                    with self._condition: self._accepting = False
                finally:
                    with self._condition: self._active = False
            with self._condition:
                if not self._queue and self._accepting:
                    self._condition.wait(timeout=1)
                if not self._queue and not self._accepting: return
                if not self._queue: continue
                future, request, deadline = self._queue.popleft()
                if not future.set_running_or_notify_cancel(): continue
                if time.monotonic() >= deadline:
                    future.set_exception(RuntimeBusy('Queue wait deadline exceeded'))
                    continue
                self._active, self._started = True, time.monotonic()
            try:
                key = tuple(request[k] for k in ('principal','session','topic','mode'))
                result = self.handler(self.model, request, key)
            except BaseException:
                # Do not retain model-bearing traceback frames in client futures.
                future.set_exception(RuntimeFailure('RAG request failed'))
            else:
                future.set_result(result)
                del result
            finally:
                with self._condition:
                    self._active = False
                    self._condition.notify_all()
            del future, request

    def _watch(self):
        while not self._stop.wait(min(1, self.hung_timeout/4)):
            with self._condition:
                hung = self._active and time.monotonic()-self._started > self.hung_timeout
                if hung:
                    self._accepting = False
                    while self._queue: self._queue.popleft()[0].cancel()
                    self._condition.notify_all()
            if hung:
                self._terminate(70)  # No replacement worker in a possibly damaged process.
                return

    def readiness(self, topic=None):
        with self._condition:
            result = {'ready':self._accepting, 'active':int(self._active),
                      'queued':len(self._queue), 'queue_limit':8}
        if topic is not None and hasattr(self.handler, 'readiness'):
            result['ready'] = result['ready'] and self.handler.readiness(topic)
        return result

    def drain(self):
        with self._condition:
            if self._closed: return
            self._accepting = False
            while self._queue: self._queue.popleft()[0].cancel()
            self._condition.notify_all()
        self._worker.join(timeout=self.hung_timeout+1)
        if self._worker.is_alive():
            raise RuntimeBusy('Worker still running; owner lock retained')
        self._stop.set()
        self._watchdog.join(timeout=2)
        if hasattr(self.handler, 'close'): self.handler.close()
        self.model = None
        # Drop cyclic chain/model references before handing ownership to the indexer.
        import gc
        gc.collect()
        self.owner.close()
        self._closed = True
