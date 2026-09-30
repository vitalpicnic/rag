"""Validated provider selection; secrets never appear in settings repr."""
from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit


def validate_ollama_url(value):
    url = urlsplit(value)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('Invalid private Ollama endpoint')
    if url.hostname not in ('localhost', 'ollama'):
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            raise ValueError('Ollama requires localhost, ollama, or a private IP') from None
        if not (address.is_loopback or address.is_private) or address.is_unspecified or address.is_multicast:
            raise ValueError('Ollama endpoint must be private')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('Invalid port')
    return value.rstrip('/')


@dataclass(frozen=True)
class Settings:
    profile: str
    environment: str
    llm_provider: str
    embedding_provider: str
    model: str
    ollama_url: str
    context_window: int
    timeout: int
    google_key: str = field(default='', repr=False)


def load_settings(env):
    profile = env.get('RAG_PROFILE', 'CLOUD').upper()
    environment = env.get('RAG_ENV', 'development')
    if profile not in ('CLOUD', 'LOCAL', 'HYBRID') or environment not in ('development', 'production'):
        raise ValueError('Unknown profile/environment')
    provider = env.get('RAG_LLM_PROVIDER', 'ollama' if profile == 'LOCAL' else 'gemini')
    embedding = env.get('RAG_EMBEDDING_PROVIDER', 'google' if profile == 'CLOUD' else 'local')
    if provider not in ('gemini', 'ollama', 'openai_compatible') or embedding not in ('google', 'local'):
        raise ValueError('Unknown provider')
    if profile == 'LOCAL' and (provider != 'ollama' or embedding != 'local'):
        raise ValueError('LOCAL requires local embeddings and Ollama; no cloud fallback')
    if profile == 'HYBRID' and embedding != 'local':
        raise ValueError('HYBRID requires local embeddings')
    model = env.get('RAG_MODEL', 'qwen3:4b' if provider == 'ollama' else 'gemini-2.5-flash').strip()
    if not model or (provider == 'ollama' and ('cloud' in model.lower() or '://' in model)):
        raise ValueError('A local model name is required')
    key, filename = env.get('GOOGLE_API_KEY', ''), env.get('GOOGLE_API_KEY_FILE', '')
    if key and filename:
        raise ValueError('Conflicting GOOGLE_API_KEY and GOOGLE_API_KEY_FILE')
    if filename:
        key = Path(filename).read_text(encoding='utf-8').strip()
    context = int(env.get('OLLAMA_CONTEXT_LENGTH', '8192'))
    timeout = int(env.get('OLLAMA_TIMEOUT', '120'))
    if not 512 <= context <= 131072 or not 1 <= timeout <= 600:
        raise ValueError('Invalid context/timeout limits')
    return Settings(profile, environment, provider, embedding, model,
                    validate_ollama_url(env.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')),
                    context, timeout, key)
