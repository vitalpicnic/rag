"""Explicit online bootstrap of a pinned CPU embedding snapshot; no corpus access."""
import argparse
import json
from pathlib import Path
import re


def bootstrap_model(destination, revision):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('Use the full immutable Hugging Face commit SHA, not main')
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('Choose a new model directory')
    from huggingface_hub import snapshot_download
    from rag.embeddings import MODEL
    from rag.index import _hash
    destination.mkdir(parents=True)
    snapshot_download(MODEL, revision=revision, local_dir=destination,
                      allow_patterns=['*.json', '*.txt', '*.model', '*.safetensors'],
                      ignore_patterns=['onnx/*', 'openvino/*'])
    files = {p.relative_to(destination).as_posix(): _hash(p) for p in sorted(destination.rglob('*'))
             if p.is_file() and '.cache' not in p.relative_to(destination).parts}
    required = ('config.json', 'modules.json', 'model.safetensors', '1_Pooling/config.json')
    if not all(name in files for name in required):
        raise RuntimeError('Downloaded snapshot is incomplete')
    with (destination/'model-manifest.json').open('x', encoding='utf-8') as handle:
        json.dump({'model': MODEL, 'revision': revision, 'files': files}, handle, indent=2)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    print(bootstrap_model(args.destination, args.revision))


if __name__ == '__main__':
    main()
