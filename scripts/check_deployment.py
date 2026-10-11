"""Read-only state preflight. No model loading, network calls or secret output."""
import argparse
import json
import os
from pathlib import Path
from rag.privacy import PolicyStore

STATE_DIRECTORIES=('data','faiss_indexes','prepared_tables','prepared_corpora','model_cache','policies','logs','bot')


def _read(path,limit):
    if path.resolve()!=path.absolute() or not path.is_file(): raise ValueError('Unsafe state file')
    with path.open('rb') as stream: data=stream.read(limit+1)
    if len(data)>limit: raise ValueError('Oversized configuration')
    return data.decode('utf-8')


def check_state(root,secrets_root):
    root,secrets_root=Path(root).absolute(),Path(secrets_root).absolute()
    checks={'state_directories':root.resolve()==root and all(
        (root/name).is_dir() and (root/name).resolve()==root/name for name in STATE_DIRECTORIES)}
    lock=root/'.model-owner.lock'
    checks['owner_lock']=lock.is_file() and lock.resolve()==lock
    try:
        if secrets_root.resolve()!=secrets_root: raise ValueError('Linked credentials')
        names=('cli','web','tg','health')
        paths=[secrets_root/(name+'.txt') for name in names]+[secrets_root/'adapters.json']
        values={name:_read(secrets_root/(name+'.txt'),4096).strip() for name in names}
        config=json.loads(_read(paths[-1],65536))
        checks['adapter_secrets']=(values==config and all(len(value)>=32 for value in values.values())
                                  and len(set(values.values()))==len(names))
        checks['secret_permissions']=(os.name!='posix' or all(
            path.stat().st_mode & 0o077==0 for path in [secrets_root,*paths]))
    except (OSError,ValueError,TypeError):
        checks['adapter_secrets']=False
        checks['secret_permissions']=False
    try:
        PolicyStore(root/'policies/external.json').read()
        checks['privacy_policy']=True
    except Exception: checks['privacy_policy']=False
    return {'status':'passed' if all(checks.values()) else 'failed','checks':checks,
            'generation_verified':False,'network_calls':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--secrets',type=Path,required=True)
    args=parser.parse_args()
    report=check_state(args.root,args.secrets)
    print(json.dumps(report))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__': raise SystemExit(main())
