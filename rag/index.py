"""Versioned FAISS indexes. Loading never builds or updates an index."""
from contextlib import contextmanager
import hashlib
import json
import logging
import os
import pickle
from pathlib import Path
import re
from datetime import datetime, timezone
from uuid import uuid4

DEFAULT_SETTINGS = {
    'pipeline_version': 3,
    'metadata_version': 1,
    'embedding': {'model': 'gemini-embedding-001', 'dimensions': 3072,
                  'client': 'langchain-google-genai', 'client_version': '4.2.1',
                  'document_task': 'RETRIEVAL_DOCUMENT', 'query_task': 'RETRIEVAL_QUERY',
                  'normalization': 'none', 'prefix': ''},
    'chunking': {'splitter': 'RecursiveCharacterTextSplitter', 'version': '1.1.1',
                 'size': 1000, 'overlap': 100, 'length': 'characters',
                 'separators': ['\n\n', '\n', ' ', '']},
    'extraction': {'pdf_loader': 'pypdf', 'version': '6.7.1', 'mode': 'plain', 'txt_encoding': 'utf-8'},
    'index': {'type': 'FAISS IndexFlatL2', 'version': '1.13.2'},
}


class IndexNotReady(RuntimeError):
    pass


class IndexChanged(IndexNotReady):
    pass


def _paths(root, topic):
    root = Path(root).resolve()
    if not topic or topic in ('.', '..') or any(c in topic for c in '/\\:'):
        raise IndexNotReady('Некорректное имя темы.')
    data = (root / 'data' / topic).resolve()
    index = (root / 'faiss_indexes' / topic).resolve()
    if not data.is_relative_to(root / 'data') or not index.is_relative_to(root / 'faiss_indexes'):
        raise IndexNotReady('Путь темы выходит за пределы проекта.')
    return root, data, index


def _hash(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def inspect_corpus(root, topic, settings=None):
    root, data, _ = _paths(root, topic)
    if not data.is_dir():
        raise IndexNotReady(f'Нет папки документов для темы «{topic}».')
    files = []
    for path in sorted(data.rglob('*'), key=lambda value: value.relative_to(root).as_posix()):
        if not path.is_file() or path.suffix.lower() not in ('.pdf', '.txt'):
            continue
        if not path.resolve().is_relative_to(data):
            raise IndexNotReady('Документ ссылается за пределы папки темы.')
        relative = path.relative_to(root).as_posix()
        files.append({'source': relative, 'bytes': path.stat().st_size,
                      'sha256': _hash(path),
                      'source_id': hashlib.sha256(relative.encode('utf-8')).hexdigest()})
    if not files:
        raise IndexNotReady('В теме нет PDF или TXT для индексации.')
    snapshot = {'topic': topic, 'settings': settings or DEFAULT_SETTINGS, 'files': files}
    snapshot['fingerprint'] = hashlib.sha256(_json_bytes(snapshot)).hexdigest()
    return snapshot


def extract_chunks(root, snapshot):
    from importlib.metadata import version
    for package, expected in (('pypdf', snapshot['settings']['extraction']['version']),
                              ('langchain-text-splitters', snapshot['settings']['chunking']['version'])):
        if version(package) != expected:
            raise IndexNotReady(f'Версия {package} отличается от настроек manifest.')
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from pypdf import PdfReader
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    config = snapshot['settings']['chunking']
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config['size'], chunk_overlap=config['overlap'],
        separators=config['separators'], add_start_index=True,
    )
    chunks, stats = [], []
    for item in snapshot['files']:
        path = Path(root) / item['source']
        if path.suffix.lower() == '.pdf':
            reader = PdfReader(path)
            pages = [page.extract_text(extraction_mode=snapshot['settings']['extraction']['mode']) or ''
                     for page in reader.pages]
        else:
            pages = [path.read_text(encoding=snapshot['settings']['extraction']['txt_encoding'])]
        from rag.evidence import source_metadata
        provenance = source_metadata(item['source'], pages)
        documents = [Document(page_content=text, metadata={
            **provenance,
            'source': item['source'], 'source_id': item['source_id'],
            'source_sha256': item['sha256'], 'page': page,
            'page_label': str(page + 1),
        }) for page, text in enumerate(pages)]
        parts = splitter.split_documents(documents)
        for chunk in parts:
            identity = [item['source_id'], item['sha256'], chunk.metadata['page'],
                        chunk.metadata['start_index'], chunk.page_content]
            chunk.metadata['chunk_id'] = hashlib.sha256(_json_bytes(identity)).hexdigest()
        chunks.extend(parts)
        stats.append({'source': item['source'], 'pages': len(pages),
                      'empty_pages': sum(not text.strip() for text in pages),
                      'characters': sum(len(text) for text in pages), 'chunks': len(parts)})
    if not chunks:
        raise IndexNotReady('Из документов не извлечён текст; индекс не опубликован.')
    return chunks, stats


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Expected object')
        return value
    except (OSError, ValueError) as exc:
        raise IndexNotReady('Индекс отсутствует или повреждён; выполните build_index.py.') from exc


def current_index(root, topic, settings=None):
    root, _, base = _paths(root, topic)
    pointer = _read_json(base / 'current.json')
    if not isinstance(pointer, dict) or not re.fullmatch('[0-9a-f]{32}', str(pointer.get('version', ''))):
        raise IndexNotReady('Некорректный указатель версии индекса.')
    version = (base / 'versions' / pointer['version']).resolve()
    if not version.is_relative_to(base):
        raise IndexNotReady('Путь версии выходит за пределы индекса.')
    manifest = _read_json(version / 'manifest.json')
    if _hash(version / 'manifest.json') != pointer.get('manifest_sha256'):
        raise IndexNotReady('Контрольная сумма manifest не совпадает.')
    if manifest.get('schema_version') != 1 or manifest.get('version') != pointer['version']:
        raise IndexNotReady('Неподдерживаемый manifest индекса.')
    snapshot = inspect_corpus(root, topic, settings)
    if manifest.get('snapshot', {}).get('fingerprint') != snapshot['fingerprint']:
        raise IndexChanged('Документы или настройки изменились. Пересоберите индекс командой build_index.py.')
    for name in ('index.faiss', 'index.pkl', 'chunks.jsonl'):
        path = version / name
        if not path.is_file() or _hash(path) != manifest.get('artifacts', {}).get(name):
            raise IndexNotReady(f'Повреждён файл индекса: {name}. Пересоберите индекс.')
    return version, manifest


def _validate_store(store, manifest):
    import faiss
    if faiss.__version__ != manifest['snapshot']['settings']['index']['version']:
        raise IndexNotReady('Версия FAISS отличается от настроек manifest.')
    if (store.index.ntotal != manifest['chunk_count']
            or store.index.d != manifest['snapshot']['settings']['embedding']['dimensions']
            or len(store.index_to_docstore_id) != manifest['chunk_count']):
        raise IndexNotReady('Число векторов или размерность индекса не соответствуют manifest.')
    allowed = {item['source']: item for item in manifest['snapshot']['files']}
    for doc_id in store.index_to_docstore_id.values():
        doc = store.docstore.search(doc_id)
        if not hasattr(doc, 'metadata') or doc.metadata.get('chunk_id') != doc_id:
            raise IndexNotReady('Некорректное соответствие вектора и фрагмента.')
        source = doc.metadata.get('source')
        if (source not in allowed or doc.metadata.get('source_id') != allowed[source]['source_id']
                or doc.metadata.get('source_sha256') != allowed[source]['sha256']):
            raise IndexNotReady('Метаданные источников не соответствуют manifest.')


def _load_version(root, version, manifest, embeddings):
    import faiss
    import numpy as np
    from langchain_community.vectorstores import FAISS
    # Only our local published artifacts, checked before any pickle deserialization.
    # Python handles Unicode paths; FAISS C++ file APIs do not on Windows.
    index = faiss.deserialize_index(np.frombuffer((version / 'index.faiss').read_bytes(), dtype=np.uint8))
    with (version / 'index.pkl').open('rb') as handle:
        docstore, mapping = pickle.load(handle)
    store = FAISS(embeddings, index, docstore, mapping)
    _validate_store(store, manifest)
    for doc_id in store.index_to_docstore_id.values():
        doc = store.docstore.search(doc_id)
        doc.metadata['source_relative'] = doc.metadata['source']
        doc.metadata['source'] = str(Path(root).resolve() / doc.metadata['source'])
    return store


def load_index(root, topic, embeddings, settings=None):
    version, manifest = current_index(root, topic, settings)
    return _load_version(root, version, manifest, embeddings)


@contextmanager
def _build_lock(base):
    lock = base / 'build.lock'
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise IndexNotReady('Другая сборка держит build.lock. Проверьте процесс перед удалением блокировки.') from exc
    try:
        with handle:
            handle.write(json.dumps({'pid': os.getpid(), 'started': datetime.now(timezone.utc).isoformat()}))
        yield
    finally:
        lock.unlink()


def publish_current(base, version):
    pointer = {'version': version.name, 'manifest_sha256': _hash(version / 'manifest.json')}
    temporary = base / ('current-' + uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(_json_bytes(pointer))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, base / 'current.json')
    finally:
        temporary.unlink(missing_ok=True)


def build_index(root, topic, embeddings, settings=None):
    from langchain_community.vectorstores import FAISS
    settings = settings or DEFAULT_SETTINGS
    root, _, base = _paths(root, topic)
    snapshot = inspect_corpus(root, topic, settings)
    base.mkdir(parents=True, exist_ok=True)
    with _build_lock(base):
        try:
            version, manifest = current_index(root, topic, settings)
            return {'status': 'unchanged', 'version': version.name,
                    'document_count': manifest['document_count'], 'chunk_count': manifest['chunk_count']}
        except IndexNotReady:
            pass
        prepared = _prepared_path(root, snapshot)
        if (prepared / 'manifest.json').is_file():
            chunks, stats = _load_prepared(prepared, snapshot)
        else:
            chunks, stats = extract_chunks(root, snapshot)
        if inspect_corpus(root, topic, settings)['fingerprint'] != snapshot['fingerprint']:
            raise IndexChanged('Корпус изменился во время извлечения текста.')
        version = base / 'versions' / uuid4().hex
        version.mkdir(parents=True)
        with (version / 'chunks.jsonl').open('x', encoding='utf-8') as handle:
            for chunk in chunks:
                handle.write(json.dumps({'text': chunk.page_content, 'metadata': chunk.metadata},
                                        ensure_ascii=False) + '\n')
        store = FAISS.from_documents(chunks, embeddings, ids=[doc.metadata['chunk_id'] for doc in chunks])
        manifest = {'schema_version': 1, 'version': version.name,
                    'created_at': datetime.now(timezone.utc).isoformat(), 'snapshot': snapshot,
                    'document_count': len(snapshot['files']), 'chunk_count': len(chunks),
                    'documents': stats}
        _validate_store(store, manifest)
        import faiss
        (version / 'index.faiss').write_bytes(faiss.serialize_index(store.index).tobytes())
        with (version / 'index.pkl').open('wb') as handle:
            pickle.dump((store.docstore, store.index_to_docstore_id), handle)
        manifest['artifacts'] = {name: _hash(version / name)
                                 for name in ('index.faiss', 'index.pkl', 'chunks.jsonl')}
        (version / 'manifest.json').write_bytes(_json_bytes(manifest))
        # Read the saved version before publishing it, not just the in-memory index.
        _load_version(root, version, manifest, embeddings)
        if inspect_corpus(root, topic, settings)['fingerprint'] != snapshot['fingerprint']:
            raise IndexChanged('Корпус изменился во время embedding; версия не опубликована.')
        publish_current(base, version)
        return {'status': 'built', 'version': version.name,
                'document_count': len(snapshot['files']), 'chunk_count': len(chunks)}


def _prepared_path(root, snapshot):
    root = Path(root).resolve()
    path = (root / 'prepared_corpora' / snapshot['topic'] / snapshot['fingerprint']).resolve()
    if not path.is_relative_to(root / 'prepared_corpora'):
        raise IndexNotReady('Путь подготовленного корпуса выходит за пределы проекта.')
    return path


def _load_prepared(path, snapshot):
    from langchain_core.documents import Document
    manifest = _read_json(path / 'manifest.json')
    chunks_path = path / 'chunks.jsonl'
    if (manifest.get('status') != 'prepared_without_vectors' or manifest.get('snapshot') != snapshot
            or not chunks_path.is_file() or _hash(chunks_path) != manifest.get('chunks_sha256')):
        raise IndexNotReady('Подготовленные фрагменты повреждены или несовместимы.')
    try:
        rows = [json.loads(line) for line in chunks_path.read_text(encoding='utf-8').splitlines()]
        chunks = [Document(page_content=row['text'], metadata=row['metadata']) for row in rows]
        if len(chunks) != manifest['chunk_count']:
            raise ValueError('Wrong chunk count')
    except (ValueError, KeyError) as exc:
        raise IndexNotReady('Некорректный формат подготовленных фрагментов.') from exc
    return chunks, manifest['documents']


def prepare_corpus(root, topic, settings=None):
    """Persist extracted text without any embedding client or active index pointer."""
    root, _, _ = _paths(root, topic)
    snapshot = inspect_corpus(root, topic, settings)
    target = _prepared_path(root, snapshot)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _build_lock(target.parent):
        if (target / 'manifest.json').is_file():
            chunks, stats = _load_prepared(target, snapshot)
        else:
            chunks, stats = extract_chunks(root, snapshot)
            if inspect_corpus(root, topic, settings)['fingerprint'] != snapshot['fingerprint']:
                raise IndexChanged('Корпус изменился во время подготовки.')
            target.mkdir(exist_ok=True)
            with (target / 'chunks.jsonl').open('w', encoding='utf-8') as handle:
                for chunk in chunks:
                    handle.write(json.dumps({'text': chunk.page_content, 'metadata': chunk.metadata},
                                            ensure_ascii=False) + '\n')
            manifest = {'status': 'prepared_without_vectors', 'snapshot': snapshot,
                        'document_count': len(stats), 'chunk_count': len(chunks),
                        'documents': stats, 'chunks_sha256': _hash(target / 'chunks.jsonl')}
            temporary = target / 'manifest.tmp'
            temporary.write_bytes(_json_bytes(manifest))
            os.replace(temporary, target / 'manifest.json')
        return {'status': 'prepared_without_vectors', 'path': str(target),
                'document_count': len(stats), 'chunk_count': len(chunks), 'index_published': False}


class IndexRetriever:
    """Recheck freshness per query and reload only when the published version changes."""
    def __init__(self, root, topic, embeddings, settings=None):
        import threading
        self.root, self.topic, self.embeddings = root, topic, embeddings
        self.settings = settings or DEFAULT_SETTINGS
        self._version, self._store = None, None
        self._lock = threading.Lock()

    def invoke(self, query, config=None):
        options = (config or {}).get('metadata', {})
        mode = options.get('retrieval_mode', 'text')
        with self._lock:
            version, manifest = current_index(self.root, self.topic, self.settings)
            if version != self._version:
                self._store = _load_version(self.root, version, manifest, self.embeddings)
                self._version = version
            store = self._store
        matches = store.similarity_search_with_score(query, k=24 if mode == 'overview' else 12)
        return [doc.model_copy(update={'metadata': {
            **doc.metadata, 'retrieval_distance': float(distance),
            'retrieval_metric': 'squared_l2', 'index_fingerprint': manifest['snapshot']['fingerprint'],
            'embedding_model': self.settings['embedding']['model'],
        }}) for doc, distance in matches]
