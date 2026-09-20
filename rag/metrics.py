"""Local stage metrics. Never stores prompts, answers, keys or session IDs."""
import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)
_DEFAULT_PATH = Path(__file__).resolve().parents[1] / 'logs' / 'rag.metrics.jsonl'


def _field(obj, name):
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def extract_usage(response):
    """Output includes thinking; thinking/cache are subsets, not extra totals.

    Missing provider fields remain None. Do not infer zero usage for embeddings,
    images, errors, or providers that do not return usage.
    """
    usage = _field(response, 'usage_metadata')
    if _field(usage, 'input_tokens') is not None:
        return {
            'input_tokens': _field(usage, 'input_tokens'),
            'output_tokens': _field(usage, 'output_tokens'),
            'thinking_tokens': _field(_field(usage, 'output_token_details'), 'reasoning'),
            'cache_read_tokens': _field(_field(usage, 'input_token_details'), 'cache_read'),
            'total_tokens': _field(usage, 'total_tokens'),
        }
    candidates = _field(usage, 'candidates_token_count')
    thinking = _field(usage, 'thoughts_token_count')
    return {
        'input_tokens': _field(usage, 'prompt_token_count'),
        'output_tokens': None if candidates is None or thinking is None else candidates + thinking,
        'thinking_tokens': thinking,
        'cache_read_tokens': _field(usage, 'cached_content_token_count'),
        'total_tokens': _field(usage, 'total_token_count'),
    }


def write_event(event, metrics_path=None):
    """A metrics disk failure must not turn a successful paid call into an error."""
    try:
        path = Path(metrics_path or os.getenv('RAG_METRICS_PATH') or _DEFAULT_PATH)
        line = json.dumps(event, ensure_ascii=False, allow_nan=False)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as handle:
                handle.write(line + '\n')
    except (OSError, TypeError, ValueError):
        _LOG.warning('Unable to write RAG metrics; response processing continues.')


def measure_call(function, *args, stage, model, request_id, metrics_path=None, **kwargs):
    """Measure one application call; retries hidden inside SDKs are unknown."""
    start = time.perf_counter()
    response = None
    status, error_type = 'ok', None
    try:
        response = function(*args, **kwargs)
        return response
    except BaseException as exc:
        status, error_type = 'error', type(exc).__name__
        raise
    finally:
        write_event({
            'schema_version': 1,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'request_id': request_id,
            'stage': stage,
            'model': model,
            'duration_ms': round((time.perf_counter() - start) * 1000, 3),
            'status': status,
            'error_type': error_type,
            'application_calls': 1,
            'internal_retry_count': None,
            **extract_usage(response),
        }, metrics_path)
