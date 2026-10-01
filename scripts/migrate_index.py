"""ADMIN ONLY: trusted v1 pickle migration. Never import this in a serving process.

CLI requires non-root Linux, no API secrets, explicit trust and isolation flags.
Use a disposable network-none container, read-only input COPY and writable output.
Checksums are not authenticity: hostile pickle is forbidden regardless of hashes.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from rag.index import current_index, _hash, _build_lock
from rag.index_schema import corpus_id, chunks_id, processing_settings
from rag.index_store import _base, _save_version


def migrate_trusted_legacy(root, topic, *, trusted=False, settings=None, output_root=None):
    if not trusted:
        raise ValueError('Migration requires trusted input; SHA is not authentication')
    version, manifest = current_index(root, topic, settings)
    # This is the sole v2 administrative path allowed to deserialize legacy pickle.
    import pickle
    import faiss
    import numpy as np
    rows = [json.loads(line) for line in (version/'chunks.jsonl').read_text(encoding='utf-8').splitlines()]
    with (version/'index.pkl').open('rb') as handle:
        docstore, mapping = pickle.load(handle)
    if set(mapping) != set(range(len(rows))):
        raise ValueError('Legacy mapping is not contiguous')
    for i, row in enumerate(rows):
        if mapping[i] != row['metadata']['chunk_id']:
            raise ValueError('Legacy vector/chunk mapping differs')
        doc = docstore.search(mapping[i])
        if doc.page_content != row['text'] or doc.metadata != row['metadata']:
            raise ValueError('Legacy docstore differs from prepared chunks')
    index = faiss.deserialize_index(np.frombuffer((version/'index.faiss').read_bytes(), dtype='uint8'))
    old = manifest['snapshot']['settings']['embedding']
    profile = {'provider': 'google', 'model': old['model'], 'revision': 'legacy-unversioned',
               'dimensions': old['dimensions'], 'normalization': old['normalization'],
               'query_prefix': old['prefix'], 'document_prefix': old['prefix'],
               'query_task': old['query_task'], 'document_task': old['document_task'],
               'tokenizer': 'provider-managed', 'max_length': 'provider-managed',
               'pooling': 'provider-managed', 'runtime': {old['client']: old['client_version']}}
    if index.d != profile['dimensions'] or index.ntotal != len(rows):
        raise ValueError('Legacy dimensions/count mismatch')
    for name, digest in manifest['artifacts'].items():
        if _hash(version/name) != digest:
            raise ValueError('Legacy artifacts changed during migration')
    output_root = Path(output_root or root).resolve()
    _, base = _base(output_root, topic)
    base.mkdir(parents=True, exist_ok=True)
    corpus = corpus_id(manifest['snapshot']['files'])
    processing = processing_settings(manifest['snapshot']['settings'])
    prepared = {'corpus_id': corpus, 'chunks_id': chunks_id(corpus, processing), 'processing': processing}
    with _build_lock(base):
        return _save_version(output_root, topic, base, index, rows, prepared, profile)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-root', required=True, type=Path)
    parser.add_argument('--output-root', required=True, type=Path)
    parser.add_argument('--topic', required=True)
    parser.add_argument('--trusted-input', action='store_true')
    parser.add_argument('--isolated', action='store_true')
    args = parser.parse_args()
    if not args.trusted_input or not args.isolated or os.name != 'posix' or os.geteuid() == 0:
        parser.error('Use explicit trusted input in an isolated non-root Linux container')
    if args.input_root.resolve() == args.output_root.resolve():
        parser.error('Input copy and output must be distinct roots')
    if any(value for key, value in os.environ.items() if 'API_KEY' in key or 'TOKEN' in key):
        parser.error('Migration container must not contain API secrets')
    # Defense in depth only: --network none and trusted provenance remain mandatory.
    def offline(event, _args):
        if event == 'socket.connect':
            raise RuntimeError('Networking forbidden during migration')
    sys.addaudithook(offline)
    bundle = migrate_trusted_legacy(args.input_root, args.topic, trusted=True, output_root=args.output_root)
    print(json.dumps(bundle))


if __name__ == '__main__':
    main()
