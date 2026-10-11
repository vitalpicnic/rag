"""CI-only fixture: synthetic vectors, real FAISS/SQLite, no LLM or model downloads."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

BASELINE='faiss_indexes/fixture-baseline.json'


def hashes(root):
    from rag.operations import DIRECTORIES
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for directory in DIRECTORIES for p in (root/directory).rglob('*')
            if p.is_file() and p.relative_to(root).as_posix()!=BASELINE}


def prepare(root):
    from scripts.check_deployment import STATE_DIRECTORIES,check_state
    from scripts.init_secrets import initialize
    from rag.embeddings import local_profile
    from rag.index_store import build_version,activate_bundle
    root=Path(root)
    root.mkdir()  # Refuse existing roots: this command never targets live state.
    for name in STATE_DIRECTORIES: (root/name).mkdir()
    (root/'.model-owner.lock').touch()
    (root/'policies/external.json').write_text(json.dumps({'schema_version':1,'topics':{}}))
    initialize(root/'secrets')
    (root/'data/smoke').mkdir()
    (root/'data/smoke/a.txt').write_text('Assets are 42.',encoding='utf-8')
    class FixtureEmbeddings:
        profile=local_profile('a'*40,{})
        def embed_documents(self,texts): return [[1.0]+[0.0]*383 for _ in texts]
    activate_bundle(root,'smoke',build_version(root,'smoke',FixtureEmbeddings()))
    db=sqlite3.connect(root/'prepared_tables/fixture.sqlite')
    try:
        db.execute('CREATE TABLE example(value INTEGER)')
        db.execute('INSERT INTO example VALUES (42)'); db.commit()
    finally: db.close()
    (root/BASELINE).write_text(json.dumps(hashes(root)),encoding='utf-8')
    if check_state(root,root/'secrets')['status']!='passed': raise ValueError('Fixture preflight failed')


def verify(root):
    from rag.operations import verify_restored
    root=Path(root)
    if hashes(root)!=json.loads((root/BASELINE).read_text()): raise ValueError('Persisted state changed')
    verify_restored(root)


def runtime_check(root):
    from rag.runtime_client import RuntimeClient
    client=RuntimeClient.from_env('offline-acceptance')
    if client.request('/health/live')!={'live':True}: raise ValueError('Liveness failed')
    state=client.request('/health/ready')
    if state.get('ready') is not False or state.get('reason')!='generation_disabled':
        raise ValueError('This smoke only accepts generation-disabled mode')
    if client.topics()!=['smoke']: raise ValueError('Persisted topic missing')
    try: client.query({'topic':'smoke','session':'fixture','mode':'text','question':'fixture'})
    except RuntimeError: pass
    else: raise ValueError('Disabled generation unexpectedly answered')
    verify(root)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','verify','runtime'])
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args()
    {'prepare':prepare,'verify':verify,'runtime':runtime_check}[args.action](args.root)
    print(json.dumps({'status':'passed','action':args.action,'real_generation':False,'real_embeddings':False}))


if __name__=='__main__': main()
