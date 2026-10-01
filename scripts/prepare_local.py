"""Prepare reusable v2 chunks without an embedding model, Ollama, or API key."""
import argparse
import json
from pathlib import Path
from rag.index_schema import prepare_chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--topic', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_chunks(args.root, args.topic), ensure_ascii=False))


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    main()
