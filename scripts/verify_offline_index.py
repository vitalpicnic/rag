"""Check full-corpus FAISS serialization in a temporary project, without APIs."""
import copy
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from langchain_core.embeddings import Embeddings
from rag.index import DEFAULT_SETTINGS, build_index, load_index, prepare_corpus


class OfflineTestEmbeddings(Embeddings):
    """Not semantic embeddings; never published into the real project index."""
    def embed_query(self, text):
        return [value / 255 for value in hashlib.sha256(text.encode('utf-8')).digest()]

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]


def main():
    topic = 'Банкинг'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'reports' / 'offline_index_verification.json')
    output = parser.parse_args().output
    if output.exists():
        raise SystemExit('Verification report already exists; preserve it before another run.')
    prepared = prepare_corpus(ROOT, topic)
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='rag-offline-') as tmp:
        sandbox = Path(tmp)
        shutil.copytree(ROOT / 'data' / topic, sandbox / 'data' / topic)
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings['embedding'].update(model='offline-sha256-test-only', dimensions=32)
        embedding = OfflineTestEmbeddings()
        result = build_index(sandbox, topic, embedding, settings)
        index = load_index(sandbox, topic, embedding, settings)
        assert index.index.ntotal == prepared['chunk_count']
        sample = index.docstore.search(index.index_to_docstore_id[0])
        found = index.similarity_search(sample.page_content, k=3)
        assert found and all(Path(doc.metadata['source']).is_file() for doc in found)
        assert build_index(sandbox, topic, embedding, settings)['status'] == 'unchanged'
        report = {'status': 'passed', 'scope': 'build/save/load/noop; not semantic quality',
                  'document_count': result['document_count'], 'chunk_count': index.index.ntotal,
                  'embedding': 'offline-sha256-test-only', 'dimensions': 32,
                  'google_compatible': False, 'api_calls': 0, 'production_index_published': False,
                  'prepared_corpus': prepared, 'duration_seconds': round(time.perf_counter() - start, 2)}
    report['temporary_index_removed'] = True
    with output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
