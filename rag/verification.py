"""Verified v2 snapshots: cheap admission, explicit invalidation, bounded freshness."""
from dataclasses import dataclass
import json
from pathlib import Path
from threading import Event, Lock, RLock, Thread
import time

from rag.index import _paths
from rag.index_store import _base, _version, load_bundle


class VerificationRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedSnapshot:
    topic: str
    bundle: dict
    index: object
    rows: tuple
    signatures: tuple
    pointer: bytes
    generation: int
    verified_at: float
    expires_at: float
    owner: object


@dataclass
class _State:
    snapshot: VerifiedSnapshot | None = None
    generation: int = 0
    blocked: bool = False
    reason: str = ''
    last_watch: float = 0
    last_attempt: float = float('-inf')
    pending_pointer: bytes | None = None
    failed_pointer: bytes | None = None


class SnapshotVerifier:
    WATCH_INTERVAL = 30
    VERIFY_INTERVAL = 3600
    MAX_AGE = 7200

    def __init__(self, root, *, clock=time.monotonic):
        self.root = Path(root).resolve()
        self.clock = clock
        self._states = {}
        self._lock = RLock()
        self._scan_lock = Lock()  # One full scan across all topics.
        self._stop = Event()
        self._threads = []
        self._owner = object()

    def _observe_pointer(self, topic, pointer):
        state = self._state(topic)
        if (state.snapshot is not None and pointer != state.snapshot.pointer
                and state.pending_pointer != pointer):
            self.invalidate(topic, 'pointer changed')
            state.pending_pointer = pointer

    def _state(self, topic):
        _paths(self.root, topic)  # Validate before using user-controlled topic as key.
        return self._states.setdefault(topic, _State())

    def _pointer(self, topic):
        _, base = _base(self.root, topic)
        path = base/'active.json'
        if path.resolve() != path:
            raise VerificationRequired('Linked pointer forbidden')
        with path.open('rb') as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise VerificationRequired('Oversized index pointer')
        bundle = json.loads(raw)
        _version(base, bundle)
        return raw, bundle

    def _signatures(self, topic, bundle):
        _, data, _ = _paths(self.root, topic)
        _, base = _base(self.root, topic)
        result = []
        for root in (data, _version(base, bundle)):
            for path in sorted([root, *root.rglob('*')]):
                if path.is_symlink() or path.is_junction():
                    raise VerificationRequired('Linked corpus/artifact forbidden')
                stat = path.stat()
                result.append((path.relative_to(self.root).as_posix(), stat.st_size,
                               stat.st_mtime_ns, stat.st_ino, stat.st_mode))
        return tuple(result)

    def invalidate(self, topic, reason):
        with self._lock:
            state = self._state(topic)
            state.generation += 1
            state.blocked = True
            state.reason = reason

    def acquire(self, topic):
        with self._lock:
            state = self._state(topic)
            try:
                pointer, _ = self._pointer(topic)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                self.invalidate(topic, 'pointer unavailable')
                raise VerificationRequired('Index pointer unavailable') from exc
            snapshot = state.snapshot
            if snapshot is not None and pointer != snapshot.pointer:
                if state.failed_pointer == pointer:
                    raise VerificationRequired('Changed pointer failed verification')
                self._observe_pointer(topic, pointer)
                needs_verify = True
            elif state.blocked:
                raise VerificationRequired('Index requires explicit or background verification')
            elif snapshot is not None:
                if self.clock() >= snapshot.expires_at:
                    raise VerificationRequired('Index verification expired')
                return snapshot
            else:
                needs_verify = True
        if needs_verify:
            return self._verify(topic, force=False)

    def full_verify(self, topic):
        return self._verify(topic, force=True)

    def _verify(self, topic, *, force):
        with self._scan_lock:
            try:
                pointer, bundle = self._pointer(topic)
                with self._lock:
                    state = self._state(topic)
                    if (not force and not state.blocked and state.snapshot is not None
                            and state.snapshot.pointer == pointer and self.clock() < state.snapshot.expires_at):
                        return state.snapshot
                    generation = state.generation
                    state.last_attempt = self.clock()
                before = self._signatures(topic, bundle)
                index, rows = load_bundle(self.root, topic, bundle)
                after = self._signatures(topic, bundle)
                current, _ = self._pointer(topic)
                with self._lock:
                    state = self._state(topic)
                    if before != after or current != pointer or state.generation != generation:
                        raise VerificationRequired('Index changed during verification')
                    finished = self.clock()
                    snapshot = VerifiedSnapshot(topic, bundle, index, tuple(rows), after, pointer,
                                                generation, finished, finished+self.MAX_AGE, self._owner)
                    state.snapshot = snapshot
                    state.blocked, state.reason = False, ''
                    state.pending_pointer = None
                    state.failed_pointer = None
                    state.last_watch = finished
                    return snapshot
            except Exception as exc:
                self.invalidate(topic, 'verification failed')
                with self._lock:
                    self._state(topic).failed_pointer = locals().get('pointer')
                raise VerificationRequired('Full index verification failed') from exc

    def validate_lease(self, snapshot):
        with self._lock:
            state = self._state(snapshot.topic)
            if snapshot.owner is not self._owner or state.snapshot is None:
                raise VerificationRequired('Snapshot belongs to a different verifier instance')
            try:
                pointer, _ = self._pointer(snapshot.topic)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                self.invalidate(snapshot.topic, 'pointer unavailable')
                raise VerificationRequired('Index pointer unavailable') from exc
            self._observe_pointer(snapshot.topic, pointer)
            if (state.blocked or state.generation != snapshot.generation
                    or pointer != snapshot.pointer or self.clock() >= snapshot.expires_at):
                raise VerificationRequired('Snapshot lease revoked or expired')

    def maintenance(self, *, watch=True, verify=True):
        """Call from a background scheduler, never from a user request handler."""
        with self._lock:
            topics = list(self._states)
        for topic in topics:
            try:
                with self._lock:
                    state = self._states[topic]
                    snapshot = state.snapshot
                    watch_due = self.clock()-state.last_watch >= self.WATCH_INTERVAL
                    verify_due = snapshot is not None and self.clock()-snapshot.verified_at >= self.VERIFY_INTERVAL
                    blocked = state.blocked
                    retry_due = self.clock()-state.last_attempt >= self.WATCH_INTERVAL
                if snapshot is None:
                    continue
                changed = False
                if watch and watch_due:
                    signatures = self._signatures(topic, snapshot.bundle)
                    pointer, _ = self._pointer(topic)
                    if signatures != snapshot.signatures or pointer != snapshot.pointer:
                        if not blocked:
                            self.invalidate(topic, 'watcher detected change')
                        blocked = True
                        changed = True
                    with self._lock:
                        self._states[topic].last_watch = self.clock()
                if verify and (changed or (retry_due and (verify_due or blocked))):
                    self.full_verify(topic)
            except Exception:
                self.invalidate(topic, 'background verification failed')

    def start(self):
        """Start a single cooperative periodic worker; full startup verify stays explicit."""
        with self._lock:
            if any(thread.is_alive() for thread in self._threads):
                return
            self._stop.clear()
            def worker(watch, verify):
                while not self._stop.wait(1):
                    self.maintenance(watch=watch, verify=verify)
            self._threads = [Thread(target=worker, args=(True, False), name='rag-index-watcher', daemon=True),
                             Thread(target=worker, args=(False, True), name='rag-index-verifier', daemon=True)]
            for thread in self._threads:
                thread.start()

    def close(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
