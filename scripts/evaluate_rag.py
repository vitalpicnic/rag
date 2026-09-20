"""Check local gold evidence or explicitly run a baseline against an existing index."""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rag.evaluation import run_cases, summarize_results
from rag.index import current_index, IndexNotReady
from rag.models import PROFILES, model_options, summarize_costs


def read_dataset(path):
    dataset = json.loads(path.read_text(encoding='utf-8'))
    if dataset['schema_version'] != 1:
        raise ValueError('Unsupported dataset version')
    ids = set()
    for case in dataset['cases']:
        if case['id'] in ids:
            raise ValueError('Duplicate case ID: ' + case['id'])
        ids.add(case['id'])
        for source in case['expected_sources']:
            source['source'] = dataset['documents'][source['document']]
            path = (ROOT / source['source']).resolve()
            if not path.is_relative_to(ROOT / 'data') or source['page'] < 1:
                raise ValueError('Invalid expected source')
    return dataset


def check_dataset(dataset):
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.path.insert(0, str(ROOT / '.audit_deps'))
        from pypdf import PdfReader
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    readers, hashes = {}, {}
    for name, source in dataset['documents'].items():
        path = ROOT / source
        readers[name] = PdfReader(path)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    failures, checked = [], 0
    texts = {}
    for case in dataset['cases']:
        for source in case['expected_sources']:
            key = source['document'], source['page']
            if key not in texts:
                texts[key] = ' '.join(readers[key[0]].pages[key[1] - 1].extract_text().split())
            for evidence in source['evidence']:
                checked += 1
                if ' '.join(evidence.split()) not in texts[key]:
                    failures.append({'id': case['id'], 'page': key[1], 'evidence': evidence})
    corpus = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted((ROOT / 'data' / dataset['topic']).iterdir())
              if path.suffix.lower() in ('.pdf', '.txt')}
    corpus_version = hashlib.sha256(json.dumps(corpus, sort_keys=True).encode()).hexdigest()
    return {'case_count': len(dataset['cases']), 'evidence_checks': checked,
            'failures': failures, 'document_sha256': hashes, 'corpus_sha256': corpus_version,
            'corpus_file_count': len(corpus)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Local PDF evidence check (default)')
    mode.add_argument('--run', action='store_true', help='Calls configured cloud models; never builds an index')
    mode.add_argument('--summarize', type=Path, help='Recompute summary after human review')
    parser.add_argument('--cases', type=Path, default=ROOT / 'tests' / 'fixtures' / 'rag_eval_cases.json')
    parser.add_argument('--output', type=Path, help='New JSON report; existing files are not overwritten')
    parser.add_argument('--model', choices=PROFILES, help='Explicit generation model; otherwise RAG_MODEL')
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error('Output already exists; choose a new filename')
    if args.summarize:
        report = json.loads(args.summarize.read_text(encoding='utf-8'))
        report['summary'] = summarize_results(report['results'])
        report['cost'] = summarize_costs([event for row in report['results'] for event in row.get('metrics', [])])
    else:
        dataset = read_dataset(args.cases)
        check = check_dataset(dataset)
        report = {'check': check, 'baseline_status': 'not_run',
                  'model_options': model_options(args.model),
                  'cases_sha256': hashlib.sha256(args.cases.read_bytes()).hexdigest()}
        pipeline_files = ['rag/engine.py', 'rag/pipeline.py', 'rag/evidence.py', 'rag/history.py', 'rag/index.py']
        report['code_sha256'] = hashlib.sha256(b''.join((ROOT / name).read_bytes() for name in pipeline_files)).hexdigest()
        if check['failures']:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        try:
            version, manifest = current_index(ROOT, dataset['topic'])
            report.update(index_ready=True, index_version=version.name,
                          index_fingerprint=manifest['snapshot']['fingerprint'])
        except IndexNotReady as exc:
            report.update(index_ready=False, index_reason=str(exc))
        if args.run:
            if not args.output:
                parser.error('--run requires --output to preserve answers and measurements')
            if not report['index_ready']:
                parser.error('Topic index is missing, stale, or invalid. No API calls made.')
            # Imports and model initialization occur only after the read-only checks.
            from rag.engine import setup_rag_chain, get_session_history, store
            metrics_path = args.output.with_suffix('.metrics.jsonl').resolve()
            if metrics_path.exists():
                parser.error('Metrics file already exists; choose a new output name')
            os.environ['RAG_METRICS_PATH'] = str(metrics_path)
            chain = setup_rag_chain(dataset['topic'], model_name=report['model_options']['model'])
            def prepare(session_id, history):
                session = get_session_history(session_id)
                for message in history:
                    if message['role'] == 'user':
                        session.add_user_message(message['content'])
                    elif message['role'] == 'assistant':
                        session.add_ai_message(message['content'])
                    else:
                        raise ValueError('Unsupported history role')
            rows = run_cases(chain, dataset['cases'], prepare, lambda sid: store.pop(sid, None))
            events = [json.loads(line) for line in metrics_path.read_text(encoding='utf-8').splitlines()] if metrics_path.exists() else []
            for row in rows:
                row['metrics'] = [event for event in events if event['request_id'] == row['request_id']]
            report.update(baseline_status='run', results=rows, summary=summarize_results(rows),
                          cost=summarize_costs(events),
                          metrics_path=str(metrics_path),
                          scope='Text RAG only; local charts and Telegram transport excluded')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'results'}, ensure_ascii=False, indent=2))
    return int(report.get('summary', {}).get('error_count', 0) > 0)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
