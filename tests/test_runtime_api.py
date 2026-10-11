import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from rag.runtime_api import RuntimeAPI, APIError, make_server
from rag.runtime_client import RuntimeClient


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'data/bank/a.txt'
        self.source.parent.mkdir(parents=True); self.source.write_bytes(b'Assets 42')
        self.backend = Mock()
        self.backend.query.return_value = {'answer':'42 [S1]','mode':'text','question':'Q',
            'sources':[SimpleNamespace(metadata={'source':'data/bank/a.txt','source_sha256':hashlib.sha256(b'Assets 42').hexdigest(),'page':0})]}
        self.backend.readiness.return_value = {'ready':True}
        self.secret = 's'*32
        self.api = RuntimeAPI(self.root,self.backend,{self.secret:'test'})

    def call(self, path='/query', principal='one', secret=None, method='POST', body=None):
        return self.api.dispatch(method,path,secret or self.secret,principal,
                                 body or {'session':'session','topic':'bank','mode':'text','question':'Q'})

    def test_auth_before_backend(self):
        for secret in ('wrong',''):
            with self.assertRaises(APIError): self.api.dispatch('POST','/query',secret,'one',{})
        self.backend.query.assert_not_called()

    def test_client_keeps_project_dotenv_support(self):
        import os
        from unittest.mock import patch
        (self.root/'.env').write_text('RAG_RUNTIME_URL=http://127.0.0.1:8123\nRAG_SERVICE_SECRET='+'x'*32+'\n')
        with patch.dict(os.environ,{},clear=True), patch('rag.runtime_client.__file__',str(self.root/'rag/runtime_client.py')):
            client=RuntimeClient.from_env('one')
            self.assertEqual(client.url,'http://127.0.0.1:8123')
            self.assertEqual(client.secret,'x'*32)

    def test_source_tokens_are_user_bound_and_hash_checked(self):
        response = self.call()
        token = response['sources'][0]['metadata']['download_token']
        self.assertNotIn('page_content',response['sources'][0])
        with self.assertRaises(APIError): self.call('/sources/'+token,principal='two',method='GET')
        self.assertEqual(self.call('/sources/'+token,method='GET')[0],b'Assets 42')
        self.source.write_bytes(b'changed')
        with self.assertRaises(APIError): self.call('/sources/'+token,method='GET')

    def test_traversal_never_creates_download(self):
        self.backend.query.return_value['sources'][0].metadata['source'] = '../secret.txt'
        with self.assertRaises(APIError): self.call()

    def test_reset_revokes_exports(self):
        result = self.call()
        self.call('/history/reset')
        self.backend.reset_history.assert_called_once()
        with self.assertRaises(APIError): self.call('/exports/'+result['export_token'],method='GET')

    def test_loopback_http_client_and_secret(self):
        server = make_server(self.api,'127.0.0.1',0)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        client = RuntimeClient('http://127.0.0.1:'+str(server.server_port),self.secret,'one')
        result = client.query({'session':'s','topic':'bank','mode':'expert','question':'Q'})
        self.assertEqual(result['answer'],'42 [S1]')
        self.assertEqual(self.backend.query.call_args.args[0]['principal'],'test:one')
        self.assertEqual(client.topics(),['bank'])

    def test_expired_token_is_refused(self):
        self.api.clock = lambda: 0
        token = self.call()['export_token']
        self.api.clock = lambda: 901
        with self.assertRaises(APIError): self.call('/exports/'+token,method='GET')

    def test_pptx_export_never_regenerates(self):
        result = self.call()
        data,mime,name = self.call('/exports/'+result['export_token'],method='GET')
        self.assertTrue(data.startswith(b'PK'))
        self.assertEqual(name,'analysis.pptx')
        self.backend.query.assert_called_once()

    def test_heartbeat_and_polling_lock_fail_closed(self):
        from unittest.mock import patch
        from rag.health import heartbeat,heartbeat_ready
        from rag.resources import ModelOwnerLock,ResourceBusy
        path = self.root/'heartbeat.json'
        self.assertFalse(heartbeat_ready(path))
        with patch('rag.health.time.time',return_value=100): heartbeat(path)
        with patch('rag.health.time.time',return_value=116): self.assertFalse(heartbeat_ready(path))
        with ModelOwnerLock(self.root,'.polling.lock'):
            with self.assertRaises(ResourceBusy): ModelOwnerLock(self.root,'.polling.lock').acquire()
