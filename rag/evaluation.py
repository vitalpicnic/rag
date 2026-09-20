"""Offline evaluation helpers; answer correctness requires a human review."""
import math
import statistics
import time
from uuid import uuid4


def _source_key(source):
    parts = str(source).replace('\\', '/').split('/')
    # Old index absolute roots can differ; keep topic to avoid filename collisions.
    if 'data' in parts:
        return '/'.join(parts[parts.index('data'):])
    return '/'.join(parts)


def score_retrieval(expected_sources, retrieved_metadata):
    gold = {(_source_key(item['source']), item['page']) for item in expected_sources}
    if not gold:
        return {'page_recall': None, 'all_pages_found': None}
    found = {(_source_key(item.get('source', '')), item['page'] + 1)
             for item in retrieved_metadata if isinstance(item.get('page'), int)}
    return {'page_recall': len(gold & found) / len(gold), 'all_pages_found': gold <= found}


def summarize_results(rows):
    recalls = [r['page_recall'] for r in rows if r.get('page_recall') is not None]
    reviewed = [r.get('review', {}).get('answer_correct') for r in rows]
    reviewed = [value for value in reviewed if isinstance(value, bool)]
    latencies = sorted(r['duration_ms'] for r in rows if r.get('duration_ms') is not None)
    return {
        'case_count': len(rows),
        'error_count': sum(r['status'] != 'ok' for r in rows),
        'retrieval_scored_count': len(recalls),
        'mean_page_recall': statistics.mean(recalls) if recalls else None,
        'answer_reviewed_count': len(reviewed),
        'answer_accuracy_reviewed': statistics.mean(reviewed) if reviewed else None,
        'latency_p50_ms': statistics.median(latencies) if latencies else None,
        'latency_p95_ms': latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None,
    }


def run_cases(chain, cases, prepare_history, clear_history):
    """Replay fixed dialogue history, never a generated answer from another case."""
    rows = []
    for case in cases:
        session_id = 'eval_' + uuid4().hex
        request_id = uuid4().hex
        row = {
            'id': case['id'], 'category': case['category'],
            'question': case['question'], 'history': case.get('history', []),
            'expected_answer': case['expected_answer'],
            'expected_sources': case['expected_sources'], 'request_id': request_id,
            'answer': None, 'sources': [], 'status': 'ok', 'error_type': None,
            'page_recall': None, 'all_pages_found': None,
            'review': {'answer_correct': None, 'citations_support_answer': None,
                       'correct_refusal': None, 'notes': ''},
        }
        start = time.perf_counter()
        try:
            prepare_history(session_id, case.get('history', []))
            response = chain.invoke({'input': case['question'], 'request_id': request_id,
                                     'mode': 'overview' if case['category'] == 'overview' else 'text'},
                                    config={'configurable': {'session_id': session_id}})
            row['answer'] = response['answer']
            row['evidence_status'] = response.get('evidence_status')
            row['retrieval_calibrated'] = response.get('retrieval_calibrated', False)
            row['sources'] = [{**doc.metadata, 'text': doc.page_content}
                              for doc in response.get('sources', [])]
            row.update(score_retrieval(case['expected_sources'], row['sources']))
        except Exception as exc:
            row.update(status='error', error_type=type(exc).__name__)
        finally:
            row['duration_ms'] = round((time.perf_counter() - start) * 1000, 3)
            clear_history(session_id)
        rows.append(row)
    return rows
