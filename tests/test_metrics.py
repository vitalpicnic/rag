import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import rag.metrics


class MetricsTests(unittest.TestCase):
    def test_langchain_usage_does_not_double_count_thinking(self):
        message = SimpleNamespace(usage_metadata={
            'input_tokens': 100, 'output_tokens': 30, 'total_tokens': 130,
            'input_token_details': {'cache_read': 20},
            'output_token_details': {'reasoning': 10},
        })
        self.assertEqual(rag.metrics.extract_usage(message), {
            'input_tokens': 100, 'output_tokens': 30, 'thinking_tokens': 10,
            'cache_read_tokens': 20, 'total_tokens': 130,
        })

    def test_google_usage_includes_separate_thinking_in_output(self):
        message = SimpleNamespace(usage_metadata=SimpleNamespace(
            prompt_token_count=100, candidates_token_count=20,
            thoughts_token_count=10, cached_content_token_count=0,
            total_token_count=130))
        self.assertEqual(rag.metrics.extract_usage(message)['output_tokens'], 30)
        self.assertEqual(rag.metrics.extract_usage(message)['cache_read_tokens'], 0)

    def test_unknown_usage_is_not_zero(self):
        self.assertTrue(all(v is None for v in rag.metrics.extract_usage([]).values()))
        partial = SimpleNamespace(usage_metadata={'prompt_token_count': 100})
        self.assertIsNone(rag.metrics.extract_usage(partial)['output_tokens'])

    def test_missing_google_thinking_is_not_silently_assumed_zero(self):
        partial = SimpleNamespace(usage_metadata={'candidates_token_count': 20})
        self.assertIsNone(rag.metrics.extract_usage(partial)['output_tokens'])

    def test_success_preserves_response_and_records_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'metrics.jsonl'
            result = SimpleNamespace(usage_metadata={'input_tokens': 2, 'output_tokens': 3})
            actual = rag.metrics.measure_call(lambda: result, stage='generation',
                model='test-model', request_id='r1', metrics_path=path)
            self.assertIs(actual, result)
            event = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual((event['status'], event['request_id'], event['input_tokens']),
                             ('ok', 'r1', 2))
            self.assertGreaterEqual(event['duration_ms'], 0)
            self.assertIsNone(event['internal_retry_count'])

    def test_error_is_logged_without_message_and_reraised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'metrics.jsonl'
            def fail():
                raise ValueError('secret prompt')
            with self.assertRaisesRegex(ValueError, 'secret prompt'):
                rag.metrics.measure_call(fail, stage='retrieval', model='m',
                    request_id='r2', metrics_path=path)
            raw = path.read_text(encoding='utf-8')
            self.assertNotIn('secret prompt', raw)
            self.assertEqual(json.loads(raw)['error_type'], 'ValueError')

    def test_unwritable_metrics_do_not_hide_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs('rag.metrics', level='WARNING'):
                self.assertEqual(rag.metrics.measure_call(lambda: 'answer',
                    stage='generation', model='m', request_id='r3',
                    metrics_path=Path(tmp)), 'answer')

    def test_concurrent_writes_keep_complete_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'metrics.jsonl'
            def run(i):
                return rag.metrics.measure_call(lambda: i, stage='retrieval', model='m',
                    request_id=str(i), metrics_path=path)
            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(list(pool.map(run, range(30))), list(range(30)))
            events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual({e['request_id'] for e in events}, {str(i) for i in range(30)})


if __name__ == '__main__':
    unittest.main()
