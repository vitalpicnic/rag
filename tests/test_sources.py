import json
from pathlib import Path
import tempfile
import unittest
from rag.sources import load_source_registry, apply_source_registry


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.entry = {'sha256': 'a' * 64, 'kind': 'official_report',
                      'url': 'https://bank.example/report.pdf', 'title': 'Отчёт банка'}

    def write(self, entry):
        (self.folder / 'sources.json').write_text(json.dumps({'a.pdf': entry}), encoding='utf-8')

    def test_explicit_classification_requires_matching_hash(self):
        self.write(self.entry)
        registry = load_source_registry(self.folder)
        meta = {'source_relative': 'data/topic/a.pdf', 'source_sha256': 'a' * 64}
        result = apply_source_registry(meta, registry, 'topic')
        self.assertEqual(result['source_kind'], 'official_report')
        self.assertEqual(result['source_url'], self.entry['url'])
        meta['source_sha256'] = 'b' * 64
        self.assertEqual(apply_source_registry(meta, registry, 'topic')['source_kind'], 'unknown')

    def test_missing_registry_never_infers_authority_from_filename(self):
        registry = load_source_registry(self.folder)
        self.assertEqual(registry, {})
        result = apply_source_registry({'source': 'data/topic/ЦБ_отчёт.pdf'}, registry, 'topic')
        self.assertEqual(result['source_kind'], 'unknown')

    def test_reject_invalid_registry_and_unsafe_links(self):
        for entry in [dict(self.entry, url='file:///secret'), dict(self.entry, kind='official'),
                      dict(self.entry, sha256='wrong')]:
            self.write(entry)
            with self.assertRaises(ValueError):
                load_source_registry(self.folder)


if __name__ == '__main__':
    unittest.main()
