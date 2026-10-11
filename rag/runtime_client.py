"""Small private-HTTP client. Does not import embeddings, FAISS or generation."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, quote
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


class RuntimeClient:
    def __init__(self,url,secret,principal):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username is not None or parsed.query or parsed.fragment:
            raise ValueError('Invalid runtime URL')
        if len(secret) < 32: raise ValueError('Runtime service secret is required')
        self.url,self.secret,self.principal = url.rstrip('/'),secret,principal
        self.opener = build_opener(ProxyHandler({}),NoRedirect())

    @classmethod
    def from_env(cls,principal):
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1]/'.env')
        value,path = os.getenv('RAG_SERVICE_SECRET'),os.getenv('RAG_SERVICE_SECRET_FILE')
        if value and path: raise ValueError('Use one service secret source')
        secret = Path(path).read_text().strip() if path else value or ''
        return cls(os.getenv('RAG_RUNTIME_URL','http://127.0.0.1:8080'),secret,principal)

    def request(self,path,body=None,binary=False):
        request = Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization':'Bearer '+self.secret,'X-RAG-Principal':self.principal,
                                   'Content-Type':'application/json'})
        try:
            with self.opener.open(request,timeout=65) as response: data = response.read(32*1024*1024+1)
        except HTTPError as exc:
            exc.close()
            raise RuntimeError('Runtime request failed') from None
        except Exception:
            raise RuntimeError('Runtime request failed') from None
        if len(data) > 32*1024*1024: raise RuntimeError('Runtime response too large')
        return data if binary else json.loads(data)

    def query(self,request):
        result = self.request('/query',request)
        result['sources'] = [SimpleNamespace(metadata=doc['metadata']) for doc in result.get('sources',[])]
        return result

    def topics(self): return self.request('/topics')['topics']
    def reset(self,session,topic): return self.request('/history/reset',{'session':session,'topic':topic})
    def download(self,kind,token):
        if kind not in ('sources','exports'): raise ValueError('Invalid resource type')
        return self.request('/'+kind+'/'+quote(token,safe=''),binary=True)
    def chart(self,choice):
        return self.chart_with_sources(choice)[:2]
    def chart_with_sources(self,choice):
        import base64
        result = self.request('/charts/'+quote(choice,safe=''),{'topic':'Банкинг'})
        return (base64.b64decode(result['image'],validate=True),result['caption'],
                [SimpleNamespace(metadata=doc['metadata']) for doc in result.get('sources',[])])


class RemoteChain:
    def __init__(self,client,topic): self.client,self.topic = client,topic
    def invoke(self,inputs,config):
        return self.client.query({'topic':self.topic,'session':config['configurable']['session_id'],
                                 'mode':inputs.get('mode','text'),'question':inputs['input']})
