"""Build/verify/activate/rollback v2 indexes; serving must be stopped for mutations."""
import argparse
import json
import os
from pathlib import Path

from rag.config import load_settings
from rag.embeddings import create_embeddings
from rag.index_store import build_version, load_bundle, current_bundle, activate_bundle, rollback_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'verify', 'activate', 'rollback'])
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--topic', required=True)
    parser.add_argument('--bundle', type=Path, help='Saved bundle JSON for verify/activate/rollback')
    parser.add_argument('--output', type=Path, help='New bundle JSON file for build')
    parser.add_argument('--maintenance', action='store_true', help='Operator confirms all serving processes are stopped')
    args = parser.parse_args()
    if args.action != 'verify' and not args.maintenance:
        parser.error('Stop bot/web/CLI/runtime and pass --maintenance')
    if args.action == 'build':
        if not args.output or args.output.exists():
            parser.error('Build requires a new --output file')
        settings = load_settings(os.environ)
        if settings.embedding_provider != 'local':
            parser.error('This build command requires RAG_EMBEDDING_PROVIDER=local')
        bundle = build_version(args.root, args.topic, create_embeddings(settings))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(bundle, handle, indent=2)
        print(json.dumps({'status': 'built_not_activated', 'bundle': bundle}))
        return
    bundle = json.loads(args.bundle.read_text(encoding='utf-8')) if args.bundle else current_bundle(args.root, args.topic)
    if args.action == 'verify':
        index, _ = load_bundle(args.root, args.topic, bundle)
        print(json.dumps({'status': 'verified', 'vectors': index.ntotal, 'dimensions': index.d}))
    else:
        if not args.bundle:
            parser.error('Activate/rollback requires explicit --bundle')
        operation = activate_bundle if args.action == 'activate' else rollback_bundle
        operation(args.root, args.topic, bundle)
        print(json.dumps({'status': 'activated', 'bundle': bundle}))


if __name__ == '__main__':
    main()
