"""Operator-maintained provenance. Authority is never inferred by the LLM."""
import json
from pathlib import PurePosixPath, Path
import re
from urllib.parse import urlsplit


def load_source_registry(folder):
    path = Path(folder) / 'sources.json'
    if not path.exists():
        return {}
    try:
        records = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(records, dict):
            raise ValueError('Ожидается объект JSON.')
        for name, record in records.items():
            relative = PurePosixPath(name)
            if (not name or relative.is_absolute() or '..' in relative.parts or
                    '\\' in name or ':' in name or name != relative.as_posix()):
                raise ValueError('Неверный относительный путь документа.')
            if not isinstance(record, dict) or record.get('kind') not in ('official_report', 'analysis', 'unknown'):
                raise ValueError('Неверный kind источника.')
            if not re.fullmatch(r'[0-9a-f]{64}', str(record.get('sha256', ''))):
                raise ValueError('Требуется SHA-256 документа, строчными буквами.')
            url = record.get('url', '')
            if not isinstance(url, str):
                raise ValueError('URL должен быть строкой.')
            if url:
                parts = urlsplit(url)
                if (parts.scheme not in ('http', 'https') or not parts.hostname or
                        parts.username or parts.password or any(c.isspace() for c in url)):
                    raise ValueError('Разрешены только публичные HTTP(S) ссылки без учётных данных.')
            for field in ('title', 'publisher', 'published_at'):
                if field in record and (not isinstance(record[field], str) or len(record[field]) > 300):
                    raise ValueError('Некорректное поле: ' + field)
        return records
    except (ValueError, OSError) as exc:
        raise ValueError('Проверьте sources.json: ' + str(exc)) from exc


def apply_source_registry(metadata, registry, topic):
    result = {k: v for k, v in metadata.items() if k not in ('source_kind', 'source_url', 'published_at')}
    result['source_kind'] = 'unknown'
    source = metadata.get('source_relative', metadata.get('source', '')).replace('\\', '/')
    prefix = f'data/{topic}/'
    record = registry.get(source[len(prefix):]) if source.startswith(prefix) else None
    if not record or record['sha256'] != metadata.get('source_sha256'):
        return result
    result.update(source_kind=record['kind'], source_url=record.get('url', ''))
    for field in ('title', 'publisher', 'published_at'):
        if record.get(field):
            result[field] = record[field]
            result[field + '_origin'] = 'registry'
    return result
