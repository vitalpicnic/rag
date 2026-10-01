"""CPU-only E5 adapter. Runtime loads an explicitly bootstrapped local snapshot."""
import json
import math
from pathlib import Path
import re
from threading import Lock

from langchain_core.embeddings import Embeddings
from rag.index import _hash

MODEL = 'intfloat/multilingual-e5-small'


def create_embeddings(settings):
    if settings.embedding_provider == 'local':
        return load_local_embeddings(settings.embedding_cache, batch_size=settings.embedding_batch_size,
                                     threads=settings.embedding_threads)
    if settings.embedding_provider != 'google' or not settings.google_key:
        raise ValueError('GOOGLE_API_KEY is required for Google embeddings')
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    from google.genai import Client
    from google.genai.types import HttpOptions, HttpRetryOptions
    embedding = GoogleGenerativeAIEmbeddings(model='gemini-embedding-001', output_dimensionality=3072,
                                            google_api_key=settings.google_key, vertexai=False)
    embedding.client.close()
    embedding.client = Client(api_key=settings.google_key, http_options=HttpOptions(
        timeout=15000, retry_options=HttpRetryOptions(attempts=2)))
    return embedding


def local_profile(revision, runtime):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('An immutable 40-character model revision is required')
    return {'provider': 'local', 'model': MODEL, 'revision': revision, 'dimensions': 384,
            'normalization': True, 'query_prefix': 'query: ', 'document_prefix': 'passage: ',
            'tokenizer': {'model': MODEL, 'revision': revision}, 'max_length': 512,
            'pooling': 'mean', 'runtime': runtime, 'device': 'cpu', 'precision': 'float32'}


class LocalEmbeddings(Embeddings):
    def __init__(self, encoder, profile, batch_size=8):
        if not 1 <= batch_size <= 64:
            raise ValueError('Invalid embedding batch size')
        self.encoder, self.profile, self.batch_size = encoder, profile, batch_size
        self._lock = Lock()

    def _encode(self, texts, prefix):
        if not texts:
            return []
        if not all(isinstance(text, str) and text.strip() for text in texts):
            raise ValueError('Embedding inputs must be nonempty text')
        with self._lock:
            vectors = self.encoder.encode([prefix + text for text in texts], batch_size=self.batch_size,
                                          normalize_embeddings=True, convert_to_numpy=True,
                                          show_progress_bar=False, precision='float32')
        rows = vectors.tolist() if hasattr(vectors, 'tolist') else vectors
        if len(rows) != len(texts) or any(len(row) != 384 or not all(math.isfinite(x) for x in row) for row in rows):
            raise ValueError('Invalid local embedding vectors')
        return rows

    def embed_documents(self, texts):
        return self._encode(texts, self.profile['document_prefix'])

    def embed_query(self, text):
        return self._encode([text], self.profile['query_prefix'])[0]


def load_local_embeddings(cache, *, batch_size=8, threads=2):
    cache = Path(cache).resolve()
    manifest_path = cache/'model-manifest.json'
    if not manifest_path.is_file():
        raise RuntimeError('Local embedding cache missing; run scripts.bootstrap_model first')
    if not 1 <= threads <= 4:
        raise ValueError('Invalid CPU threads')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    revision = manifest['revision']
    local_profile(revision, {})
    if manifest.get('model') != MODEL or not manifest.get('files'):
        raise RuntimeError('Invalid embedding model manifest')
    for name, expected in manifest['files'].items():
        path = (cache/name).resolve()
        if not path.is_relative_to(cache) or not path.is_file() or _hash(path) != expected:
            raise RuntimeError('Local model integrity check failed')
    from importlib.metadata import version
    import torch
    from sentence_transformers import SentenceTransformer
    torch.set_num_threads(threads)
    encoder = SentenceTransformer(str(cache), device='cpu', local_files_only=True,
                                  trust_remote_code=False, model_kwargs={'torch_dtype': torch.float32})
    encoder.max_seq_length = 512
    if encoder.get_sentence_embedding_dimension() != 384:
        raise RuntimeError('Unexpected model dimensions')
    runtime = {name: version(name) for name in ('torch', 'sentence-transformers', 'transformers', 'tokenizers')}
    return LocalEmbeddings(encoder, local_profile(revision, runtime), batch_size)
