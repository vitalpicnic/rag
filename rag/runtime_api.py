"""Private authenticated HTTP boundary; no model construction in interface adapters."""
from collections import OrderedDict
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
from threading import RLock, BoundedSemaphore
import time
from types import SimpleNamespace
from urllib.parse import unquote


class APIError(RuntimeError):
    def __init__(self, status=403, message='Request denied'):
        self.status = status
        super().__init__(message)


class RuntimeAPI:
    def __init__(self, root, runtime, adapter_secrets, *, clock=time.monotonic):
        if not adapter_secrets or any(len(key) < 32 or not re.fullmatch(r'[a-z0-9_-]{1,16}',value)
                                     for key,value in adapter_secrets.items()):
            raise ValueError('Distinct strong adapter secrets are required')
        self.root, self.runtime = Path(root).resolve(), runtime
        self.secrets, self.clock = dict(adapter_secrets), clock
        self.tokens, self.lock, self.epoch = OrderedDict(), RLock(), 0
        self.files = BoundedSemaphore(1)

    def _identity(self, secret, principal):
        if not isinstance(principal,str) or not re.fullmatch(r'[A-Za-z0-9_:@.-]{1,100}',principal):
            raise APIError()
        for value, adapter in self.secrets.items():
            if hmac.compare_digest(value,secret): return adapter+':'+principal
        raise APIError(401,'Invalid service credentials')

    def _put(self, kind, principal, session, topic, value):
        with self.lock:
            token = secrets.token_hex(16)
            self.tokens[token] = (kind,principal,session,topic,value,self.clock()+900)
            while len(self.tokens) > 128: self.tokens.popitem(last=False)
            return token

    def _get(self, token, kind, principal):
        with self.lock:
            item = self.tokens.get(token)
            if item is None or item[0] != kind or item[1] != principal or item[5] <= self.clock():
                raise APIError(404,'Resource expired or unavailable')
            return item[4]

    def _source(self, topic, metadata):
        relative = metadata.get('source_relative',metadata.get('source',''))
        path = Path(relative)
        if not path.is_absolute():
            path = self.root/path if path.parts and path.parts[0] == 'data' else self.root/'data'/path
        path = path.absolute()
        base = self.root/'data'/topic
        if path.resolve() != path or not path.is_relative_to(base) or not path.is_file():
            raise APIError(403,'Invalid source location')
        expected = metadata.get('source_sha256')
        if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):
            raise APIError(409,'Unverified source')
        self._read(path,expected)
        return path.relative_to(self.root).as_posix(),expected

    def _read(self, relative, expected):
        path = self.root/relative
        if path.resolve() != path or not path.is_relative_to(self.root/'data'):
            raise APIError()
        if path.stat().st_size > 32*1024*1024: raise APIError(413,'Source exceeds download limit')
        with path.open('rb') as handle: content = handle.read(32*1024*1024+1)
        if len(content) > 32*1024*1024 or hashlib.sha256(content).hexdigest() != expected:
            raise APIError(409,'Source changed')
        return content

    def dispatch(self, method, path, secret, principal, body):
        principal = self._identity(secret,principal)
        if method == 'GET' and path == '/health/live': return {'live':True}
        if method == 'GET' and path == '/health/ready':
            from rag.operations import resource_snapshot
            return {**self.runtime.readiness(),'resources':resource_snapshot(self.root)}
        if method == 'GET' and path == '/topics':
            data = self.root/'data'
            return {'topics':sorted(p.name for p in data.iterdir() if p.is_dir() and p.resolve()==p)} if data.exists() else {'topics':[]}
        if method == 'POST' and path in ('/query','/history/reset'):
            from rag.runtime import RagRuntime
            request = RagRuntime._request({**body,'principal':principal,
                'question':body.get('question','reset'),'mode':body.get('mode','text')})
            if path == '/history/reset':
                with self.lock:
                    self.epoch += 1
                    for token,item in list(self.tokens.items()):
                        if item[1:4] == (principal,request['session'],request['topic']): del self.tokens[token]
                self.runtime.reset_history(principal,request['session'],request['topic'])
                return {'reset':True}
            with self.lock: epoch = self.epoch
            result = self.runtime.query(request)
            clean = {key:result.get(key) for key in ('answer','mode','question','request_id','generated_at',
                       'evidence_status','retrieval_calibrated','index_version')}
            sources, references = [], []
            with self.files:
                for doc in result.get('sources',[]):
                    ref = self._source(request['topic'],doc.metadata)
                    references.append(ref)
                    metadata = {k:v for k,v in doc.metadata.items() if k in
                                ('title','page','source_kind','source_url','source_sha256')}
                    metadata['source'] = ref[0][5:]
                    metadata['download_token'] = self._put('sources',principal,request['session'],request['topic'],ref)
                    sources.append({'metadata':metadata})
            clean['sources'] = sources
            from rag.slides import can_export
            exportable = {**clean,'sources':[SimpleNamespace(metadata=doc['metadata']) for doc in sources]}
            clean['export_token'] = (self._put('exports',principal,request['session'],request['topic'],(exportable,references))
                                     if can_export(exportable) else None)
            with self.lock:
                if epoch != self.epoch: raise APIError(409,'Session reset during request')
            return clean
        if method == 'GET' and path.startswith(('/sources/','/exports/')):
            kind, token = path[1:].split('/',1)
            if not re.fullmatch('[0-9a-f]{32}',token): raise APIError(404)
            value = self._get(token,kind,principal)
            with self.files:
                if kind == 'sources':
                    content = self._read(*value)
                    result = content,'application/octet-stream',Path(value[0]).name
                else:
                    from rag.slides import export_answer_pptx, PPTX_MIME
                    answer, refs = value
                    for ref in refs: self._read(*ref)
                    result = export_answer_pptx(answer),PPTX_MIME,'analysis.pptx'
                self._get(token,kind,principal)
            return result
        if method == 'POST' and path.startswith('/charts/'):
            import base64
            from rag.tables import render_chart, CHOICES, SOURCE, SOURCE_HASH
            choice = unquote(path[len('/charts/'):])
            if choice not in CHOICES or body.get('topic') != 'Банкинг': raise APIError(404)
            with self.files: content,caption = render_chart(self.root,None,choice)
            token = self._put('sources',principal,'chart','Банкинг',(SOURCE,SOURCE_HASH))
            return {'image':base64.b64encode(content).decode('ascii'),'caption':caption,
                    'sources':[{'metadata':{'source':SOURCE[5:],'download_token':token,'page':0}}]}
        raise APIError(404,'Unknown route')


def make_server(api, host='127.0.0.1', port=8080):
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        slots = BoundedSemaphore(16)
        def process_request(self, request, address):
            if not self.slots.acquire(blocking=False):
                request.close(); return
            try: super().process_request(request,address)
            except BaseException:
                self.slots.release(); raise
        def process_request_thread(self, request, address):
            try: super().process_request_thread(request,address)
            finally: self.slots.release()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass  # URLs contain capability tokens.
        def setup(self):
            super().setup(); self.connection.settimeout(65)
        def do_GET(self): self.handle_api()
        def do_POST(self): self.handle_api()
        def handle_api(self):
            status, mime = 200,'application/json'
            try:
                if self.headers.get('Transfer-Encoding'): raise APIError(400)
                length = int(self.headers.get('Content-Length','0'))
                if not 0 <= length <= 65536: raise APIError(413)
                body = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(body,dict): raise APIError(400)
                result = api.dispatch(self.command,self.path,self.headers.get('Authorization','').removeprefix('Bearer '),
                                      self.headers.get('X-RAG-Principal',''),body)
                if isinstance(result,tuple): payload,mime,_ = result
                else: payload = json.dumps(result,ensure_ascii=False).encode('utf-8')
            except APIError as exc:
                status,payload = exc.status,json.dumps({'error':str(exc)}).encode()
            except (ValueError,TypeError): status,payload = 400,b'{"error":"Invalid request"}'
            except Exception: status,payload = 503,b'{"error":"Runtime unavailable"}'
            self.send_response(status)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(payload)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(payload)
    return Server((host,port),Handler)
