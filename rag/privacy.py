"""Explicit external-data authorization, checked again at every external call."""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit


class PrivacyDenied(RuntimeError):
    pass


def _endpoint(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        url = urlsplit(value)
        if (url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None
                or url.query or url.fragment or any(c.isspace() for c in value)):
            raise ValueError()
        port = url.port
        host = url.hostname.lower()
        authority = (f'[{host}]' if ':' in host else host) + (f':{port}' if port is not None and port != 443 else '')
        return 'https://' + authority + url.path.rstrip('/')
    except (ValueError, TypeError):
        raise PrivacyDenied('Invalid external endpoint') from None


class PolicyStore:
    """Small operator-controlled policy file. Never reuse stale allows on read errors."""
    def __init__(self, path):
        self.path = Path(path)
        self._lock = RLock()
        self._last = None
        self._generation = 0

    def read(self):
        with self._lock:
            try:
                if self.path.is_symlink() or self.path.is_junction():
                    raise ValueError()
                with self.path.open('rb') as handle:
                    raw = handle.read(1024*1024+1)
                if len(raw) > 1024*1024:
                    raise ValueError()
                policy = json.loads(raw)
                if policy.get('schema_version') != 1 or not isinstance(policy.get('topics'), dict):
                    raise ValueError()
                for topic, record in policy['topics'].items():
                    if not isinstance(topic, str) or not isinstance(record, dict): raise ValueError()
                    if record.get('external_generation', 'deny') not in ('allow', 'deny'): raise ValueError()
                    if not isinstance(record.get('providers', {}), dict): raise ValueError()
                    if not isinstance(record.get('sources', {}), dict): raise ValueError()
                    for provider, endpoints in record.get('providers', {}).items():
                        if provider not in ('gemini', 'openai_compatible') or not isinstance(endpoints, list): raise ValueError()
                        for endpoint in endpoints: _endpoint(endpoint)
                    for source, rule in record.get('sources', {}).items():
                        if not isinstance(source, str) or not isinstance(rule, dict): raise ValueError()
                        if rule.get('external_generation') not in ('allow', 'deny'): raise ValueError()
                identity = hashlib.sha256(raw).hexdigest()
                if identity != self._last:
                    self._generation += 1
                    self._last = identity
                return policy, identity, self._generation
            except (OSError, ValueError, TypeError, AttributeError, PrivacyDenied):
                self._generation += 1
                self._last = None
                raise PrivacyDenied('External data policy missing or invalid') from None


@dataclass(frozen=True)
class PolicyLease:
    topic: str
    source_ids: tuple
    provider: str
    endpoint: str
    policy_id: str
    generation: int
    owner: object = field(repr=False)


class PrivacyGate:
    def __init__(self, store, topic, provider, endpoint, *, profile='HYBRID'):
        if profile not in ('LOCAL', 'HYBRID', 'CLOUD'):
            raise PrivacyDenied('Unknown profile')
        self.store, self.topic, self.provider = store, topic, provider
        self.endpoint, self.profile = _endpoint(endpoint), profile
        self._owner = object()

    def authorize(self, source_ids):
        if self.profile == 'LOCAL':
            raise PrivacyDenied('LOCAL forbids external calls')
        if not source_ids or not all(isinstance(item, str) and item for item in source_ids):
            raise PrivacyDenied('Source identities are required')
        policy, identity, generation = self.store.read()
        topic = policy['topics'].get(self.topic, {})
        if topic.get('external_generation', 'deny') != 'allow':
            raise PrivacyDenied('External generation is denied for this topic')
        approved = topic.get('providers', {}).get(self.provider, [])
        if self.endpoint not in [_endpoint(value) for value in approved]:
            raise PrivacyDenied('Provider endpoint is not approved')
        if any(topic.get('sources', {}).get(source, {}).get('external_generation') == 'deny' for source in source_ids):
            raise PrivacyDenied('External generation is denied for a source')
        return PolicyLease(self.topic, tuple(sorted(set(source_ids))), self.provider,
                           self.endpoint, identity, generation, self._owner)

    def authorize_documents(self, documents, history=()):
        # Old chat history lacks source provenance. Do not leak revoked evidence through it.
        if history:
            raise PrivacyDenied('External history requires source-provenance tracking')
        return self.authorize([doc.metadata.get('source_id') for doc in documents])

    def validate(self, lease):
        if (lease.owner is not self._owner or lease.topic != self.topic
                or lease.provider != self.provider or lease.endpoint != self.endpoint):
            raise PrivacyDenied('Policy lease belongs to another gate')
        current = self.authorize(lease.source_ids)
        if current.policy_id != lease.policy_id or current.generation != lease.generation:
            raise PrivacyDenied('External permission changed during request')

    def call(self, lease, function, *args, **kwargs):
        self.validate(lease)  # Applies equally to countTokens and generation.
        result = function(*args, **kwargs)
        self.validate(lease)
        return result
