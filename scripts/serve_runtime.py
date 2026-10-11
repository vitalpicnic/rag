"""Private service entry point. Model-free mode exposes utilities, never fake answers."""
import importlib
import json
import os
from pathlib import Path
import signal
from threading import Thread

from rag.runtime_api import RuntimeAPI, make_server
from rag.resources import ModelOwnerLock


class NoGenerationRuntime:
    def __init__(self,root): self.owner = ModelOwnerLock(root).acquire()
    def query(self,request): raise RuntimeError('Generation is not configured')
    def reset_history(self,*args): return None
    def readiness(self): return {'ready':False,'reason':'generation_disabled','active':0,'queued':0}
    def drain(self): self.owner.close()


def main():
    root = Path(os.getenv('RAG_ROOT','/srv/rag')).resolve()
    credentials = json.loads(Path(os.environ['RAG_ADAPTER_SECRETS_FILE']).read_text())
    secrets = {value:key for key,value in credentials.items()}
    if len(secrets) != len(credentials): raise ValueError('Adapter secrets must be distinct')
    if os.getenv('RAG_GENERATION_ENABLED','false') == 'true':
        from rag.config import load_settings
        from rag.engine import create_runtime
        from rag.privacy import PolicyStore
        module,separator,name = os.getenv('RAG_COUNTER_FACTORY','').partition(':')
        if not separator: raise ValueError('A verified model-specific counter factory is required')
        settings = load_settings(os.environ)
        counter = getattr(importlib.import_module(module),name)(settings)
        runtime = create_runtime(root,settings,counter,
                    privacy_store=PolicyStore(root/'policies/external.json'))
    else:
        runtime = NoGenerationRuntime(root)
    try:
        api = RuntimeAPI(root,runtime,secrets)
        server = make_server(api,os.getenv('RAG_BIND','127.0.0.1'),int(os.getenv('RAG_PORT','8080')))
        def stop(*args): Thread(target=server.shutdown,daemon=True).start()
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        try: server.serve_forever()
        finally: server.server_close()
    finally: runtime.drain()


if __name__ == '__main__': main()
