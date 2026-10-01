"""Immutable FAISS v2 artifacts and atomic maintenance-only activation.

No pickle is read or written. Integrity hashes assume a trusted local writer;
they do not authenticate an attacker-controlled manifest.
"""
import json
import hashlib
import os
from pathlib import Path
import re
from uuid import uuid4

from rag.index import _paths, _build_lock, _hash, _json_bytes, inspect_corpus
from rag.index_schema import prepare_chunks, corpus_id, chunks_id, embedding_profile_id


def _base(root, topic):
    root, _, legacy = _paths(root, topic)
    base = legacy/'v2'
    if base.resolve() != base or (base.exists() and base.is_symlink()):
        raise ValueError('Linked index directories are forbidden')
    return root, base


def _sync_directory(path):
    # Windows has no POSIX directory fsync; Linux deployment must test this path.
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path, data):
    with path.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _json(path):
    if path.is_symlink() or path.is_junction():
        raise ValueError('Linked artifact is forbidden')
    return json.loads(path.read_text(encoding='utf-8'))


def _version(base, bundle):
    version = bundle.get('index_version', '')
    if not isinstance(version, str) or not re.fullmatch('[0-9a-f]{32}', version):
        raise ValueError('Invalid version ID')
    relative = 'versions/'+version
    if bundle.get('schema_version') != 2 or bundle.get('relative_path') != relative:
        raise ValueError('Invalid bundle path/schema')
    path = base/relative
    if path.resolve() != path or not path.is_dir():
        raise ValueError('Invalid version directory')
    return path


def current_bundle(root, topic):
    _, base = _base(root, topic)
    bundle = _json(base/'active.json')
    _version(base, bundle)
    return bundle


def _validate(root, topic, path, bundle, expected_profile=None):
    manifest_path = path/'manifest.json'
    if manifest_path.resolve() != manifest_path:
        raise ValueError('Linked manifest is forbidden')
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != bundle.get('manifest_sha256'):
        raise ValueError('Manifest hash mismatch')
    manifest = json.loads(manifest_bytes)
    if manifest.get('schema_version') != 2:
        raise ValueError('Invalid manifest schema')
    for name in ('index_version', 'corpus_id', 'chunks_id', 'embedding_profile_id'):
        if manifest.get(name) != bundle.get(name):
            raise ValueError('Bundle identity mismatch')
    profile = manifest['embedding_profile']
    if embedding_profile_id(profile) != bundle['embedding_profile_id']:
        raise ValueError('Embedding identity mismatch')
    if expected_profile is not None and embedding_profile_id(expected_profile) != bundle['embedding_profile_id']:
        raise ValueError('Incompatible embedding profile, even if dimensions match')
    snapshot = inspect_corpus(root, topic)
    if corpus_id(snapshot['files']) != bundle['corpus_id']:
        raise ValueError('Corpus changed; restore matching documents before activation')
    if chunks_id(bundle['corpus_id'], manifest['processing']) != bundle['chunks_id']:
        raise ValueError('Processing identity mismatch')
    if set(manifest['artifacts']) != {'index.faiss', 'chunks.jsonl'}:
        raise ValueError('Unexpected artifact set')
    artifacts = {}
    for name, digest in manifest['artifacts'].items():
        artifact = path/name
        if artifact.resolve() != artifact:
            raise ValueError('Linked artifact is forbidden')
        artifacts[name] = artifact.read_bytes()
        if hashlib.sha256(artifacts[name]).hexdigest() != digest:
            raise ValueError('Artifact hash mismatch')
    rows = [json.loads(line) for line in artifacts['chunks.jsonl'].decode('utf-8').splitlines()]
    ids = set()
    sources = {item['source']: item['sha256'] for item in snapshot['files']}
    for row in rows:
        metadata = row['metadata']
        chunk_id = metadata['chunk_id']
        if (not isinstance(row['text'], str) or not row['text'].strip()
                or chunk_id in ids or not re.fullmatch('[0-9a-f]{64}', chunk_id)
                or metadata.get('source_sha256') != sources.get(metadata.get('source'))
                or metadata.get('source') not in sources
                or type(metadata.get('page')) is not int or metadata['page'] < 0):
            raise ValueError('Invalid chunk mapping/provenance')
        ids.add(chunk_id)
    if not rows or len(rows) != manifest['chunk_count'] or len(rows) > 100000:
        raise ValueError('Invalid chunk count')
    import faiss
    import numpy as np
    # Deserialize verified bytes; avoid a second path read during the load.
    raw = artifacts['index.faiss']
    index = faiss.deserialize_index(np.frombuffer(raw, dtype='uint8'))
    if (not isinstance(index, faiss.IndexFlatL2) or index.d != profile['dimensions']
            or index.ntotal != len(rows)):
        raise ValueError('FAISS dimensions/count/type mismatch')
    return index, rows


def load_bundle(root, topic, bundle, expected_profile=None):
    root, base = _base(root, topic)
    return _validate(root, topic, _version(base, bundle), bundle, expected_profile)


def _save_version(root, topic, base, index, rows, prepared, profile):
    version = uuid4().hex
    staging = base/('staging-'+version)
    staging.mkdir()
    import faiss
    _write(staging/'index.faiss', faiss.serialize_index(index).tobytes())
    _write(staging/'chunks.jsonl', b''.join(_json_bytes(row).replace(b'\n', b'')+b'\n' for row in rows))
    manifest = {'schema_version': 2, 'index_version': version,
                'corpus_id': prepared['corpus_id'], 'chunks_id': prepared['chunks_id'],
                'embedding_profile_id': embedding_profile_id(profile), 'embedding_profile': profile,
                'processing': prepared['processing'], 'chunk_count': len(rows),
                'artifacts': {name: _hash(staging/name) for name in ('index.faiss', 'chunks.jsonl')}}
    _write(staging/'manifest.json', _json_bytes(manifest))
    bundle = {key: manifest[key] for key in ('schema_version', 'index_version', 'corpus_id',
                                           'chunks_id', 'embedding_profile_id')}
    bundle.update(relative_path='versions/'+version, manifest_sha256=_hash(staging/'manifest.json'))
    _validate(root, topic, staging, bundle, profile)
    _sync_directory(staging)
    versions = base/'versions'
    versions.mkdir(exist_ok=True)
    _sync_directory(base)
    os.rename(staging, versions/version)
    _sync_directory(versions)
    load_bundle(root, topic, bundle, profile)
    return bundle


def build_version(root, topic, embeddings, settings=None):
    import faiss
    import numpy as np
    root, base = _base(root, topic)
    result = prepare_chunks(root, topic, settings)
    path = Path(result['path'])
    prepared = _json(path/'manifest.json')
    if _hash(path/'chunks.jsonl') != prepared['chunks_sha256']:
        raise ValueError('Prepared chunks changed')
    rows = [json.loads(line) for line in (path/'chunks.jsonl').read_text(encoding='utf-8').splitlines()]
    if not 1 <= len(rows) <= 100000:
        raise ValueError('Chunk limit exceeded')
    profile = embeddings.profile
    embedding_profile_id(profile)
    base.mkdir(parents=True, exist_ok=True)
    _sync_directory(base.parent)
    with _build_lock(base):
        index = faiss.IndexFlatL2(profile['dimensions'])
        for start in range(0, len(rows), 8):
            batch = rows[start:start+8]
            vectors = np.asarray(embeddings.embed_documents([row['text'] for row in batch]), dtype='float32')
            if vectors.shape != (len(batch), index.d) or not np.isfinite(vectors).all():
                raise ValueError('Invalid embedding vectors')
            index.add(vectors)
        return _save_version(root, topic, base, index, rows, prepared, profile)


def _publish(base, bundle):
    temporary = base/('active-'+uuid4().hex+'.tmp')
    _write(temporary, _json_bytes(bundle))
    os.replace(temporary, base/'active.json')
    _sync_directory(base)


def activate_bundle(root, topic, bundle, *, warmup=None):
    """Caller must drain/unload serving first; use only in a maintenance window."""
    root, base = _base(root, topic)
    with _build_lock(base):
        load_bundle(root, topic, bundle)
        previous = current_bundle(root, topic) if (base/'active.json').exists() else None
        if previous is not None and previous['corpus_id'] == bundle['corpus_id']:
            load_bundle(root, topic, previous)
        try:
            _publish(base, bundle)
            if warmup is not None:
                warmup(bundle)
        except BaseException:
            if previous is not None:
                _publish(base, previous)
            elif (base/'active.json').exists():
                (base/'active.json').unlink()
                _sync_directory(base)
            raise


def rollback_bundle(root, topic, previous, *, warmup=None):
    activate_bundle(root, topic, previous, warmup=warmup)
