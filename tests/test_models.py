import unittest
from unittest.mock import patch
from rag.models import model_options, generation_cost, summarize_costs, scenario


class ModelTests(unittest.TestCase):
    def test_explicit_model_overrides_environment_and_invalid_fails(self):
        with patch.dict('os.environ', {'RAG_MODEL': 'gemini-2.5-flash-lite'}):
            self.assertEqual(model_options()['model'], 'gemini-2.5-flash-lite')
            self.assertEqual(model_options('gemini-2.5-flash')['model'], 'gemini-2.5-flash')
        with self.assertRaises(ValueError):
            model_options('unknown')
        self.assertNotIn('thinking_budget', model_options('gemini-3.1-flash-lite'))

    def test_cost_includes_thinking_once_and_does_not_guess_unknown_usage(self):
        event = dict(model='gemini-2.5-flash', stage='generation', input_tokens=2000,
                     output_tokens=600, thinking_tokens=100, cache_read_tokens=0)
        self.assertAlmostEqual(generation_cost(event), .0021)
        event['output_tokens'] = None
        self.assertIsNone(generation_cost(event))
        summary = summarize_costs([event])
        self.assertIsNone(summary['generation_usd_upper_estimate'])
        self.assertEqual(summary['unknown_usage_calls'], 1)
        self.assertIsNone(summarize_costs([])['generation_usd_upper_estimate'])

    def test_scenario_is_separate_from_measurements(self):
        result = scenario('gemini-2.5-flash-lite', 2000, 600, 1500)
        self.assertAlmostEqual(result['generation_usd'], .66)
        self.assertEqual(result['status'], 'scenario_not_measurement')
        with self.assertRaises(ValueError):
            scenario('gemini-2.5-flash', -1, 600, 300)

    def test_comparison_rejects_different_data_or_unfinished_runs(self):
        from scripts.compare_models import compare
        report = dict(baseline_status='run', cases_sha256='cases', index_fingerprint='index',
                      code_sha256='code', model_options={'model': 'gemini-2.5-flash'},
                      results=[dict(id='a', status='ok', duration_ms=10, review={})])
        result = compare([report, report])
        self.assertFalse(result['models'][0]['fully_reviewed'])
        self.assertIsNone(result['automatic_winner'])
        with self.assertRaises(ValueError):
            compare([report, {**report, 'index_fingerprint': 'different'}])
        with self.assertRaises(ValueError):
            compare([{**report, 'baseline_status': 'not_run'}])
