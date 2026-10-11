"""Create adapter credentials once, without displaying their contents."""
import argparse
import json
import os
from pathlib import Path
import secrets
from rag.paths import unlinked_path


def initialize(destination):
    destination = unlinked_path(destination)
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(destination,0o700)
    names = ('cli','web','tg','health')
    targets = [destination/(name+'.txt') for name in names]+[destination/'adapters.json']
    if any(p.is_symlink() for p in targets): raise ValueError('Linked secret file forbidden')
    if any(p.exists() for p in targets):
        if not all(p.is_file() for p in targets): raise ValueError('Partial secrets setup; inspect before retry')
        config = json.loads(targets[-1].read_text())
        if (not isinstance(config,dict) or set(config)!=set(names)
                or any(not isinstance(v,str) or len(v)<32 for v in config.values())
                or len(set(config.values()))!=len(names)):
            raise ValueError('Invalid existing adapter credentials')
        if any(config.get(name) != (destination/(name+'.txt')).read_text().strip() for name in names):
            raise ValueError('Secret files disagree; no files overwritten')
        os.chmod(destination,0o700)
        for path in targets: os.chmod(path,0o600)
        return
    values = {name:secrets.token_hex(32) for name in names}
    for name in names:
        path = destination/(name+'.txt')
        with path.open('x',encoding='utf-8') as handle: handle.write(values[name])
        os.chmod(path,0o600)
    with targets[-1].open('x',encoding='utf-8') as handle: json.dump(values,handle)
    os.chmod(targets[-1],0o600)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',type=Path)
    initialize(parser.parse_args().destination)
