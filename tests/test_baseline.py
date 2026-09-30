import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import capture_baseline as baseline


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.corpus = self.root / 'data'
        self.corpus.mkdir()
        (self.corpus / 'example.txt').write_text('unchanged evidence', encoding='utf-8')
        self.output = self.root / 'capture'
        self.checkout = Path(__file__).resolve().parents[1]

    def capture(self, **kwargs):
        return baseline.capture_baseline(self.checkout, self.corpus, self.output, **kwargs)

    def test_refuses_existing_output(self):
        self.output.mkdir()
        sentinel = self.output / 'report.json'
        sentinel.write_text('keep', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.capture()
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_missing_credentials_is_blocked(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(baseline, '_run') as run:
            report = json.loads(self.capture(allow_external=True).read_text())
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('GOOGLE_API_KEY', report['reason'])
        run.assert_not_called()

    def test_explicit_external_permission_required(self):
        with patch.dict(os.environ, {'GOOGLE_API_KEY': 'secret-test-value'}):
            report_path = self.capture()
        self.assertEqual(json.loads(report_path.read_text())['status'], 'blocked')
        self.assertNotIn('secret-test-value', report_path.read_text())

    def test_never_modifies_source_root(self):
        before = {p.relative_to(self.corpus): p.read_bytes() for p in self.corpus.rglob('*') if p.is_file()}
        with patch.dict(os.environ, {}, clear=True):
            self.capture(allow_external=True)
        after = {p.relative_to(self.corpus): p.read_bytes() for p in self.corpus.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.checkout / 'baseline.json').exists())

    def test_output_inside_corpus_rejected_before_creation(self):
        self.output = self.corpus / 'capture'
        with self.assertRaises(ValueError):
            self.capture()
        self.assertFalse(self.output.exists())

    def test_missing_corpus_is_blocked(self):
        self.corpus = self.root / 'missing'
        with patch.dict(os.environ, {'GOOGLE_API_KEY': 'secret-test-value'}):
            report = json.loads(self.capture(allow_external=True).read_text())
        self.assertEqual(report['status'], 'blocked')

    def test_archive_paths_cannot_escape(self):
        for name in ('../escape', '/absolute', 'C:/absolute', 'a/../../escape', 'a\\..\\escape'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                baseline._safe_member(name)

    def test_captured_run_uses_frozen_isolated_code_and_corpus(self):
        before = baseline._files(self.corpus)
        calls = []

        def fake_cloud(args, cwd, env, log):
            calls.append(args)
            self.assertEqual(cwd, self.output / 'baseline')
            self.assertNotIn('RAG_MODEL', env)
            self.assertTrue((cwd / 'rag/engine.py').is_file())
            self.assertEqual((cwd / 'data/example.txt').read_bytes(), (self.corpus / 'example.txt').read_bytes())
            log.write_text('private fixture log', encoding='utf-8')
            if args[0] == 'scripts/evaluate_rag.py':
                (self.output / 'results.json').write_text(json.dumps({
                    'baseline_status': 'run', 'results': [{'answer': 'fixture'}],
                    'summary': {'error_count': 0}, 'model_options': {'model': 'fixture'},
                }), encoding='utf-8')

        with patch.dict(os.environ, {'GOOGLE_API_KEY': 'test', 'RAG_MODEL': 'must-not-leak'}), \
                patch.object(baseline, '_run', side_effect=fake_cloud):
            result = json.loads(self.capture(allow_external=True).read_text())
        self.assertEqual(result['status'], 'captured', result)
        self.assertTrue(result['baseline_commit'].startswith('b2c8022'))
        self.assertEqual(baseline._files(self.corpus), before)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1][:2], ['-c', baseline.UNIT_RUNNER])
        self.assertEqual(calls[2][0], 'build_index.py')

    def test_failed_unit_gate_never_reaches_cloud_build(self):
        calls = []

        def failed_units(args, cwd, env, log):
            calls.append(args)
            if args[:2] == ['-c', baseline.UNIT_RUNNER]:
                raise subprocess.CalledProcessError(1, ['private-secret-command'])

        with patch.dict(os.environ, {'GOOGLE_API_KEY': 'test'}), \
                patch.object(baseline, '_run', side_effect=failed_units):
            path = self.capture(allow_external=True)
        report = json.loads(path.read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(len(calls), 2)
        self.assertNotIn('private-secret', path.read_text())

    def test_unit_runner_records_structured_counts(self):
        suite = self.root / 'unit-checkout'
        (suite / 'tests').mkdir(parents=True)
        (suite / 'tests/test_example.py').write_text(
            'import unittest\nclass Example(unittest.TestCase):\n'
            ' def test_ok(self): self.assertTrue(True)\n'
            ' @unittest.skip("fixture")\n def test_skip(self): pass\n', encoding='utf-8')
        report = self.root / 'units.json'
        baseline._run_units(suite, os.environ.copy(), self.root / 'unit.log', report)
        self.assertEqual(json.loads(report.read_text()),
                         {'run': 2, 'passed': 1, 'failures': 0, 'errors': 0, 'skipped': 1,
                          'expected_failures': 0, 'unexpected_successes': 0})


if __name__ == '__main__':
    unittest.main()
