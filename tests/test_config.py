from pathlib import Path
import tempfile
import unittest

from rag.config import load_settings


class ConfigTests(unittest.TestCase):
    def test_local_needs_no_google_key(self):
        settings = load_settings({'RAG_PROFILE': 'LOCAL'})
        self.assertEqual(settings.llm_provider, 'ollama')
        self.assertEqual(settings.embedding_provider, 'local')

    def test_strict_profile_matrix(self):
        for env in ({'RAG_PROFILE': 'LOCAL', 'RAG_LLM_PROVIDER': 'gemini'},
                    {'RAG_PROFILE': 'LOCAL', 'RAG_EMBEDDING_PROVIDER': 'google'},
                    {'RAG_PROFILE': 'unknown'}, {'RAG_LLM_PROVIDER': 'unknown'}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                load_settings(env)

    def test_env_file_conflict_and_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'key'
            path.write_text('private-value')
            with self.assertRaises(ValueError):
                load_settings({'GOOGLE_API_KEY': 'other', 'GOOGLE_API_KEY_FILE': str(path)})
            settings = load_settings({'GOOGLE_API_KEY_FILE': str(path)})
            self.assertEqual(settings.google_key, 'private-value')
            self.assertNotIn('private-value', repr(settings))
