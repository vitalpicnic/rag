"""Local, read-only corpus inspection. No model or network calls."""
import ast
import collections
import hashlib
import json
import logging
import pickletools
import statistics
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.audit_deps'))
from pypdf import PdfReader

logging.getLogger('pypdf').setLevel(logging.ERROR)
records = []
hashes = collections.defaultdict(list)
page_hashes = collections.defaultdict(list)
for path in sorted((ROOT / 'data').rglob('*.pdf')):
    record = {'file': str(path.relative_to(ROOT)), 'bytes': path.stat().st_size}
    hashes[hashlib.sha256(path.read_bytes()).hexdigest()].append(record['file'])
    try:
        reader = PdfReader(path)
        texts = [page.extract_text() or '' for page in reader.pages]
        lengths = [len(text) for text in texts]
        record.update(pages=len(texts), chars=sum(lengths),
                      low_text_pages=[i + 1 for i, n in enumerate(lengths) if n < 100],
                      empty_pages=[i + 1 for i, n in enumerate(lengths) if not n],
                      replacement_chars=sum(text.count('\ufffd') for text in texts),
                      first_page_sample=texts[0][:1000] if texts else '',
                      samples=[{'page': i + 1, 'text': texts[i][:4500]}
                               for i in sorted(set([min(1, len(texts)-1), len(texts)//2])) if i >= 0])
        for i, text in enumerate(texts):
            normalized = ' '.join(text.split())
            if len(normalized) >= 100:
                page_hashes[hashlib.sha256(normalized.encode()).hexdigest()].append([record['file'], i + 1])
    except Exception as exc:
        record['error'] = str(exc)
    records.append(record)

indexes = []
for path in sorted(ROOT.glob('faiss*/**/index.faiss')):
    header = path.read_bytes()[:16]
    record = {'file': str(path.relative_to(ROOT)), 'bytes': path.stat().st_size,
              'header_type': header[:4].decode('ascii', errors='replace')}
    if record['header_type'] in ('IxF2', 'IxFI'):
        record.update(dimension=struct.unpack('<i', header[4:8])[0],
                      vectors=struct.unpack('<q', header[8:16])[0])
    pkl = path.with_suffix('.pkl')
    if pkl.exists():
        # Inspect serialized strings without executing pickle instructions.
        strings = [arg for op, arg, pos in pickletools.genops(pkl.read_bytes())
                   if isinstance(arg, str)]
        record['serialized_source_paths'] = sorted(set(s for s in strings if s.endswith(('.pdf', '.txt'))))
        record['long_text_sample'] = next((s[:1000] for s in strings if len(s) > 200), '')
    indexes.append(record)

syntax = {}
for path in ROOT.glob('*.py'):
    try:
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=path.name)
        syntax[path.name] = 'OK'
    except SyntaxError as exc:
        syntax[path.name] = str(exc)

summary = {'pdf_files': len(records), 'bytes': sum(r['bytes'] for r in records),
           'pages': sum(r.get('pages', 0) for r in records),
           'chars': sum(r.get('chars', 0) for r in records),
           'empty_pages': sum(len(r.get('empty_pages', [])) for r in records),
           'low_text_pages': sum(len(r.get('low_text_pages', [])) for r in records),
           'errors': [r for r in records if 'error' in r],
           'duplicate_files': [v for v in hashes.values() if len(v) > 1],
           'identical_text_pages': [v for v in page_hashes.values() if len(v) > 1]}
result = {'summary': summary, 'documents': records, 'indexes': indexes, 'python_syntax': syntax}
output = ROOT / 'reports' / 'data_audit.json'
output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'summary': summary, 'indexes': indexes, 'python_syntax': syntax}, ensure_ascii=True, indent=2))
