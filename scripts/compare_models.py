"""Offline scenarios or comparison of completed, manually reviewed evaluation reports."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag.models import PROFILES, PRICING_CHECKED, PRICING_URL, scenario, summarize_costs
from rag.evaluation import summarize_results


def compare(reports):
    if not reports or any(r.get('baseline_status') != 'run' or not r.get('results') for r in reports):
        raise ValueError('Comparison requires completed evaluation reports')
    for key in ('cases_sha256', 'index_fingerprint', 'code_sha256'):
        values = {r.get(key) for r in reports}
        if len(values) != 1 or None in values:
            raise ValueError('Reports must share ' + key)
    ids = [tuple(row['id'] for row in report['results']) for report in reports]
    if len(set(ids)) != 1 or any(len(set(group)) != len(group) for group in ids):
        raise ValueError('Reports must contain the same unique case IDs in order')
    rows = []
    for report in reports:
        results = report['results']
        rows.append({'model': report['model_options']['model'],
                     'summary': summarize_results(results),
                     'cost': summarize_costs([e for row in results for e in row.get('metrics', [])]),
                     'fully_reviewed': all(all(isinstance(row.get('review', {}).get(k), bool)
                                               for k in ('answer_correct', 'citations_support_answer', 'correct_refusal'))
                                           for row in results)})
    return {'status': 'measured_reports', 'models': rows, 'automatic_winner': None,
            'note': 'Review facts, citations, refusals and category regressions before changing the default.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='*', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error('Output exists; choose a new name')
    if args.reports:
        result = compare([json.loads(p.read_text(encoding='utf-8')) for p in args.reports])
    else:
        result = {'status': 'cloud_benchmark_not_run', 'pricing_checked': PRICING_CHECKED,
                  'pricing_url': PRICING_URL, 'scope': 'generation only; no cache discounts, embeddings, retries, taxes or hosting',
                  'scenarios': [scenario(model, 2000, 600, requests)
                                for model in PROFILES for requests in (300, 1500)]}
    if args.output:
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
