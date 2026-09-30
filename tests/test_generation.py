import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from langchain_core.messages import HumanMessage
from rag.config import load_settings
from rag.generation import create_generation


class Handler(BaseHTTPRequestHandler):
    requests = []
    failure = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append((self.path, payload))
        if self.failure:
            self.send_response(503)
            self.end_headers()
            return
        data = ({'capabilities': ['completion'], 'model_info': {}} if self.path == '/api/show'
                else {'done': True, 'message': {'role': 'assistant', 'content': 'Ответ [S1]'},
                      'prompt_eval_count': 12, 'eval_count': 4, 'model': payload['model']})
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class GenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.requests = []
        Handler.failure = False
        self.settings = load_settings({'RAG_PROFILE': 'LOCAL', 'RAG_MODEL': 'qwen3:4b',
            'OLLAMA_BASE_URL': f'http://127.0.0.1:{self.server.server_port}'})

    def test_native_chat_preserves_messages_usage_and_role_budget(self):
        llm = create_generation(self.settings)
        message = llm.bind(max_output_tokens=1800).invoke([HumanMessage(content='Что известно?')])
        self.assertEqual(message.content, 'Ответ [S1]')
        self.assertEqual(message.usage_metadata['total_tokens'], 16)
        path, payload = Handler.requests[-1]
        self.assertEqual(path, '/api/chat')
        self.assertEqual(payload['messages'], [{'role': 'user', 'content': 'Что известно?'}])
        self.assertEqual(payload['options']['num_predict'], 1800)
        self.assertFalse(payload['stream'])
        self.assertFalse(payload['think'])
        self.assertNotIn('thinking_budget', payload)

    def test_local_never_falls_back(self):
        llm = create_generation(self.settings)
        Handler.failure = True
        with self.assertRaisesRegex(RuntimeError, 'Ollama'):
            llm.invoke([HumanMessage(content='private')])
        self.assertTrue(all(path.startswith('/api/') for path, _ in Handler.requests))

    def test_public_endpoint_and_cloud_model_rejected(self):
        for override in ({'OLLAMA_BASE_URL': 'https://ollama.com'}, {'RAG_MODEL': 'qwen3:cloud'},
                         {'OLLAMA_BASE_URL': 'http://127.0.0.1:11434/path'},
                         {'OLLAMA_BASE_URL': 'http://user:secret@127.0.0.1:11434'}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                load_settings({'RAG_PROFILE': 'LOCAL', **override})

    def test_unknown_contract_blocks_production_before_request(self):
        with self.assertRaisesRegex(ValueError, 'token'):
            create_generation(load_settings({'RAG_PROFILE': 'LOCAL', 'RAG_ENV': 'production'}))
        self.assertEqual(Handler.requests, [])
