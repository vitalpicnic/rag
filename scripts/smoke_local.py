"""Offline E5 -> verified FAISS -> runtime -> synthetic LLM, in a temporary corpus."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace


def smoke(embeddings):
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda
    from rag.index_store import build_version,activate_bundle
    from rag.engine import create_runtime
    from rag.token_budget import TokenContract,TokenCounter
    with tempfile.TemporaryDirectory(prefix='rag-smoke-') as temporary:
        root=Path(temporary)
        (root/'data/demo').mkdir(parents=True)
        (root/'data/demo/capital.txt').write_text('Столица России — Москва.',encoding='utf-8')
        bundle=build_version(root,'demo',embeddings)
        activate_bundle(root,'demo',bundle)
        hashes=lambda:{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in (root/'faiss_indexes').rglob('*.faiss')}
        before=hashes()
        settings=SimpleNamespace(model='offline-fixture',llm_provider='ollama',embedding_provider='local',profile='LOCAL')
        # This counter belongs only to the synthetic fixture, never an Ollama model.
        counter=TokenCounter(TokenContract('offline-fixture','fixture-characters',16384,4096),
                             lambda messages:sum(len(m.content)+4 for m in messages))
        generation=RunnableLambda(lambda prompt,max_output_tokens:AIMessage(content='Столица России — Москва [S1].'))
        runtime=create_runtime(root,settings,counter,model_factory=lambda:(embeddings,generation))
        try:
            answer=runtime.query({'principal':'smoke','session':'test','topic':'demo',
                                  'mode':'text','question':'Как называется столица России?'})
            if 'Москва' not in answer['answer'] or not answer['sources']:
                raise RuntimeError('Full offline pipeline smoke failed')
            metadata=answer['sources'][0].metadata
            if metadata.get('page')!=0 or metadata.get('index_version')!=bundle['index_version']:
                raise RuntimeError('Source provenance smoke failed')
        finally: runtime.drain()
        if before!=hashes(): raise RuntimeError('Serving changed FAISS files')
        return {'status':'passed','real_generation':False,'faiss_unchanged':True,
                'index_version':bundle['index_version'],'source_page':metadata['page'],
                'embedding_profile':embeddings.profile}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,required=True)
    args=parser.parse_args()
    def offline(event,unused):
        if event=='socket.connect': raise RuntimeError('Network forbidden during smoke')
    sys.addaudithook(offline)
    from rag.embeddings import load_local_embeddings
    print(json.dumps(smoke(load_local_embeddings(args.cache)),ensure_ascii=False))


if __name__=='__main__': main()
