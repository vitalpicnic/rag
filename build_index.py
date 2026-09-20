"""Inspect, check, or explicitly build a topic index outside the bot process."""
import argparse
import json
from pathlib import Path
import os
import sys

from rag.index import (DEFAULT_SETTINGS, IndexNotReady, inspect_corpus, extract_chunks,
                       current_index, build_index, prepare_corpus)

ROOT = Path(__file__).resolve().parent


def create_embeddings(root=ROOT):
    from dotenv import load_dotenv
    load_dotenv(Path(root) / '.env')
    if not os.getenv('GOOGLE_API_KEY'):
        raise IndexNotReady('Заполните GOOGLE_API_KEY в .env по образцу .env.example.')
    from importlib.metadata import version
    if version('langchain-google-genai') != DEFAULT_SETTINGS['embedding']['client_version']:
        raise IndexNotReady('Версия langchain-google-genai отличается от настроек manifest.')
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    embedding = GoogleGenerativeAIEmbeddings(
        model=DEFAULT_SETTINGS['embedding']['model'],
        output_dimensionality=DEFAULT_SETTINGS['embedding']['dimensions'],
        vertexai=False,
    )
    # langchain-google-genai 4.2.1 declares request_options but does not apply it.
    # Configure the public SDK client directly: milliseconds, at most 2 attempts.
    from google.genai import Client
    from google.genai.types import HttpOptions, HttpRetryOptions
    embedding.client.close()
    embedding.client = Client(api_key=os.environ['GOOGLE_API_KEY'], http_options=HttpOptions(
        timeout=15000, retry_options=HttpRetryOptions(attempts=2)))
    return embedding


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topic', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Verify published index (default); no API')
    mode.add_argument('--inspect', action='store_true', help='Extract and count all chunks; no API')
    mode.add_argument('--build', action='store_true', help='Build using Google embedding API')
    mode.add_argument('--prepare', action='store_true', help='Save reusable text chunks without keys or API')
    parser.add_argument('--output', type=Path, help='Save report to a new JSON file')
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error('Choose a new output file; existing reports are not overwritten')
    try:
        if args.prepare:
            report = prepare_corpus(ROOT, args.topic)
        elif args.inspect:
            snapshot = inspect_corpus(ROOT, args.topic)
            chunks, stats = extract_chunks(ROOT, snapshot)
            if inspect_corpus(ROOT, args.topic)['fingerprint'] != snapshot['fingerprint']:
                raise IndexNotReady('Корпус изменился во время проверки.')
            report = {'status': 'inspected', 'snapshot': snapshot, 'documents': stats,
                      'document_count': len(stats), 'chunk_count': len(chunks),
                      'page_count': sum(item['pages'] for item in stats),
                      'zero_chunk_documents': [item['source'] for item in stats if not item['chunks']],
                      'index_published': False}
        elif args.build:
            try:
                version, manifest = current_index(ROOT, args.topic)
                report = {'status': 'unchanged', 'version': version.name,
                          'document_count': manifest['document_count'], 'chunk_count': manifest['chunk_count']}
            except IndexNotReady:
                report = build_index(ROOT, args.topic, create_embeddings())
        else:
            version, manifest = current_index(ROOT, args.topic)
            report = {'status': 'ready', 'version': version.name,
                      'document_count': manifest['document_count'], 'chunk_count': manifest['chunk_count']}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x', encoding='utf-8') as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
        print(json.dumps({k: v for k, v in report.items() if k not in ('snapshot', 'documents')},
                         ensure_ascii=False, indent=2))
        return 0
    except IndexNotReady as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
