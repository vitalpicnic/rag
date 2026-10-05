"""Process ownership and bounded, pinned LRU resources for a single runtime."""
from collections import OrderedDict
from contextlib import contextmanager
import os
import json
import hashlib
from pathlib import Path
from threading import RLock


class ResourceBusy(RuntimeError):
    pass


class ModelOwnerLock:
    """Kernel lock shared by cooperating serving/indexing processes. Never unlink."""
    def __init__(self, root):
        self.path = Path(root).resolve() / '.model-owner.lock'
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            raise ResourceBusy('Model owner lock already held')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ResourceBusy('Linked owner lock forbidden')
        handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b'0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ResourceBusy('Another model owner is active') from None
        self.handle = handle
        return self

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def __enter__(self): return self.acquire()
    def __exit__(self, *args): self.close()


class IndexCache:
    def __init__(self, limit=512*1024*1024):
        if type(limit) is not int or limit <= 0:
            raise ValueError('Invalid cache limit')
        self.limit, self.used = limit, 0
        self._items = OrderedDict()
        self._lock = RLock()

    def _evict(self, key):
        value, size, pins, close = self._items[key]
        if pins:
            raise ResourceBusy('Index is pinned')
        close(value)
        del self._items[key]
        self.used -= size

    @contextmanager
    def pin(self, key, size, load, close=lambda value: None):
        with self._lock:
            if type(size) is not int or not 0 < size <= self.limit:
                raise ResourceBusy('Index exceeds cache allocation budget')
            if key not in self._items:
                while self.used + size > self.limit:
                    candidate = next((k for k, v in self._items.items() if not v[2]), None)
                    if candidate is None:
                        raise ResourceBusy('All cache entries are pinned')
                    self._evict(candidate)
                value = load()  # Allocation only after capacity admission.
                self._items[key] = [value, size, 0, close]
                self.used += size
            entry = self._items[key]
            self._items.move_to_end(key)
            entry[2] += 1
        try:
            yield entry[0]
        finally:
            with self._lock:
                entry[2] -= 1

    def clear(self):
        with self._lock:
            if any(entry[2] for entry in self._items.values()):
                raise ResourceBusy('Cannot clear pinned indexes')
            for key in list(self._items): self._evict(key)

    def keys(self):
        with self._lock: return tuple(self._items)

    def discard(self, key):
        with self._lock:
            if key in self._items: self._evict(key)

    def entries(self):
        with self._lock:
            return [(key, entry[0], entry[1]) for key, entry in self._items.items()]


class SnapshotCache:
    """Cache owns verifiers too, so evicted FAISS/docstore objects have no hidden owner."""
    def __init__(self, root, limit=512*1024*1024):
        self.root = Path(root).resolve()
        self.cache = IndexCache(limit)

    @property
    def used(self): return self.cache.used

    def _descriptor(self, topic):
        from rag.verification import SnapshotVerifier
        from rag.index_store import _base, _version
        verifier = SnapshotVerifier(self.root)
        raw, bundle = verifier._pointer(topic)
        _, base = _base(self.root, topic)
        directory = _version(base, bundle)
        manifest_path = directory/'manifest.json'
        if manifest_path.resolve() != manifest_path or manifest_path.stat().st_size > 65536:
            raise ResourceBusy('Invalid index manifest allocation')
        manifest = json.loads(manifest_path.read_bytes())
        count = manifest.get('chunk_count')
        if type(count) is not int or not 1 <= count <= 100000:
            raise ResourceBusy('Chunk count exceeds runtime allocation limit')
        sizes = []
        for name in ('index.faiss','chunks.jsonl'):
            path = directory/name
            if path.resolve() != path: raise ResourceBusy('Linked index artifact forbidden')
            sizes.append(path.stat().st_size)
        # Includes transient raw/decoded data and a second snapshot during verification.
        estimate = 4*sizes[0] + 24*sizes[1] + 8192*count + 2*1024*1024
        return (topic, hashlib.sha256(raw).hexdigest()), estimate, verifier, raw

    @contextmanager
    def pin(self, topic):
        key, size, verifier, raw = self._descriptor(topic)
        for old in self.cache.keys():
            if old[0] == topic and old != key: self.cache.discard(old)
        for existing, _, reserved in self.cache.entries():
            if existing == key and size > reserved: self.cache.discard(key)
        def load():
            snapshot = verifier.acquire(topic)
            if snapshot.pointer != raw:
                raise ResourceBusy('Index pointer changed during allocation')
            return verifier
        with self.cache.pin(key,size,load,lambda item: item.close()) as current:
            yield current, current.acquire(topic)

    def maintenance(self):
        # Called by the same runtime worker, never concurrently with inference.
        for key, verifier, reserved in self.cache.entries():
            try:
                current, size, _, _ = self._descriptor(key[0])
                if current != key or size > reserved:
                    self.cache.discard(key)
                    continue
                verifier.maintenance()
            except Exception:
                verifier.invalidate(key[0], 'runtime maintenance failed')

    def readiness(self, topic):
        for key, verifier, _ in self.cache.entries():
            if key[0] != topic: continue
            with verifier._lock:
                state = verifier._states.get(topic)
                snapshot = state.snapshot if state is not None else None
            if snapshot is not None:
                try:
                    verifier.validate_lease(snapshot)
                    return True
                except Exception:
                    pass
        return False

    def close(self): self.cache.clear()
