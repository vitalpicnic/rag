"""Independent v2 identities and reusable chunks; legacy indexes stay untouched."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from rag.index import DEFAULT_SETTINGS, _paths, _build_lock, _hash, inspect_corpus, extract_chunks


def _identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def corpus_id(files):
    return _identity({'schema': 2, 'files': sorted(
        [{'source': row['source'], 'sha256': row['sha256']} for row in files], key=lambda row: row['source'])})


def processing_settings(settings):
    return {key: deepcopy(settings[key]) for key in ('pipeline_version', 'metadata_version', 'chunking', 'extraction')}


def chunks_id(corpus, processing):
    return _identity({'schema': 2, 'corpus_id': corpus, 'processing': processing_settings(processing)})


def embedding_profile_id(profile):
    required = {'provider', 'model', 'revision', 'dimensions', 'normalization',
                'query_prefix', 'document_prefix', 'tokenizer', 'max_length', 'pooling', 'runtime'}
    if not required <= profile.keys():
        raise ValueError('Incomplete embedding profile')
    return _identity({'schema': 2, 'embedding': profile})


def prepare_chunks(root, topic, settings=None):
    settings = deepcopy(settings or DEFAULT_SETTINGS)
    root, data, _ = _paths(root, topic)
    for entry in data.rglob('*'):
        if entry.is_symlink() or entry.is_junction():
            raise RuntimeError('Linked corpus entries are forbidden')
    snapshot = inspect_corpus(root, topic, settings)
    corpus = corpus_id(snapshot['files'])
    identity = chunks_id(corpus, settings)
    base = (root/'prepared_corpora'/'v2'/topic).resolve()
    if not base.is_relative_to(root/'prepared_corpora'):
        raise RuntimeError('Prepared path escapes root')
    base.mkdir(parents=True, exist_ok=True)
    target = base/identity
    with _build_lock(base):
        if target.exists():
            try:
                manifest = json.loads((target/'manifest.json').read_text(encoding='utf-8'))
                if (manifest['chunks_id'] != identity or manifest['corpus_id'] != corpus
                        or manifest['chunks_sha256'] != _hash(target/'chunks.jsonl')):
                    raise ValueError('Hash/identity mismatch')
            except (OSError, KeyError, ValueError) as exc:
                raise RuntimeError('Prepared chunks are corrupt') from exc
        else:
            documents, stats = extract_chunks(root, snapshot)
            if corpus_id(inspect_corpus(root, topic, settings)['files']) != corpus:
                raise RuntimeError('Corpus changed while extracting')
            staging = base/('staging-'+uuid4().hex)
            staging.mkdir()
            with (staging/'chunks.jsonl').open('x', encoding='utf-8') as handle:
                for doc in documents:
                    handle.write(json.dumps({'text': doc.page_content, 'metadata': doc.metadata}, ensure_ascii=False)+'\n')
                handle.flush()
                os.fsync(handle.fileno())
            manifest = {'schema_version': 2, 'corpus_id': corpus, 'chunks_id': identity,
                        'processing': processing_settings(settings), 'files': snapshot['files'],
                        'documents': stats, 'chunk_count': len(documents),
                        'chunks_sha256': _hash(staging/'chunks.jsonl')}
            with (staging/'manifest.json').open('x', encoding='utf-8') as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(staging, target)
    return {'path': str(target), 'corpus_id': corpus, 'chunks_id': identity,
            'chunk_count': manifest['chunk_count'], 'index_published': False}
