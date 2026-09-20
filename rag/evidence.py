"""Local evidence policy: provenance, bounded context and structural citations."""
from collections import defaultdict
from datetime import datetime
from pathlib import PurePosixPath
import re

REFUSAL = 'В найденных материалах недостаточно подтверждений для ответа. Уточните документ, организацию или период.'
MAX_CONTEXT_CHARS = 14000


def source_metadata(source, pages):
    name = PurePosixPath(source.replace('\\', '/')).stem
    metadata = {'title': name.replace('_', ' '), 'title_origin': 'filename'}
    for prefix in ('Эксперт_РА', 'РИА_Рейтинг', 'ЦБ'):
        if name.startswith(prefix + '_'):
            metadata['publisher'] = prefix.replace('_', ' ')
            metadata['publisher_origin'] = 'filename'
            break
    # Do not infer publication or reporting periods from arbitrary dates/headers.
    for field, label in [('as_of', r'по данным на'),
                         ('updated_at', r'последнее обновление страницы\s*:')]:
        found = []
        for page, text in enumerate(pages, 1):
            for match in re.finditer(label + r'\s*(\d{2}\.\d{2}\.\d{4})', text, re.I):
                try:
                    date = datetime.strptime(match[1], '%d.%m.%Y').date().isoformat()
                except ValueError:
                    continue
                found.append((date, page))
        if found and len({date for date, _ in found}) == 1:
            metadata[field], metadata[field + '_page'] = found[0]
    return metadata


def resolve_query(question, history):
    """Carry recent user context only for explicit elliptical follow-ups."""
    if not re.search(r'^\s*(?:а\s|и\s|а?\s*что насч[её]т)|\b(?:того же|этом же|предыдущ|тот же|тогда|у него|у неё)', question, re.I):
        return question
    previous = [str(m.content) for m in history if m.type == 'human'][-3:]
    if not previous:
        return question
    # The new question has priority, including a changed entity or month. Context
    # is labelled instead of merging dates into a potentially false assertion.
    return 'Предыдущие вопросы пользователя (контекст):\n' + '\n'.join(previous)[-2400:] + '\nТекущий вопрос (приоритет):\n' + question


def select_evidence(documents, mode='text'):
    limit = 6 if mode == 'overview' else 3
    unique, seen = [], set()
    for doc in documents:
        key = (doc.metadata.get('source'), doc.metadata.get('page'), doc.page_content)
        if key not in seen and doc.page_content.strip():
            seen.add(key)
            unique.append(doc)
    if mode == 'overview':
        groups = defaultdict(list)
        for doc in unique:
            groups[doc.metadata.get('source')].append(doc)
        # Round-robin preserves similarity order within a document; at most two
        # chunks per source. Diversity cannot recover sources absent in candidates.
        unique = [group[i] for i in range(2) for group in groups.values() if len(group) > i]
    selected, remaining = [], MAX_CONTEXT_CHARS
    for doc in unique[:limit]:
        header_size = len(_header(doc, len(selected) + 1)) + 4
        available = min(2000, remaining - header_size)
        if available <= 0:
            break
        copy = doc.model_copy(update={'page_content': doc.page_content[:available],
                                      'metadata': dict(doc.metadata)})
        selected.append(copy)
        remaining -= header_size + len(copy.page_content)
    return selected


def _header(doc, number):
    meta = doc.metadata
    source = str(meta.get('source_relative', meta.get('source', 'неизвестен')))
    page = meta.get('page')
    fields = [f'[S{number}]', 'источник: ' + source,
              'название: ' + str(meta.get('title', PurePosixPath(source).name)),
              'страница: ' + (str(page + 1) if isinstance(page, int) else 'неизвестна')]
    for key, label in [('publisher', 'издатель (по имени файла)'),
                       ('as_of', 'по данным на'), ('updated_at', 'обновление страницы')]:
        if meta.get(key) and (key + '_page' not in meta or
                             isinstance(page, int) and meta[key + '_page'] == page + 1):
            provenance = f" (стр. {meta[key + '_page']})" if key + '_page' in meta else ''
            fields.append(label + ': ' + str(meta[key]) + provenance)
    return '\n'.join(fields)


def format_context(documents):
    return '\n\n'.join(_header(doc, i) + '\n' + doc.page_content
                       for i, doc in enumerate(documents, 1))


def finalize_answer(answer, documents):
    """Validate reference syntax, not factual entailment. No corrective LLM call."""
    answer = answer.strip()
    references = {int(n) for n in re.findall(r'\[S(\d+)\]', answer)}
    if (not documents or 'INSUFFICIENT_EVIDENCE' in answer or not references
            or any(n < 1 or n > len(documents) for n in references)):
        return REFUSAL
    legend = []
    for number in sorted(references):
        doc = documents[number - 1]
        source = str(doc.metadata.get('source_relative', doc.metadata.get('source', 'неизвестен')))
        page = doc.metadata.get('page')
        legend.append(f'[S{number}] {PurePosixPath(source.replace(chr(92), "/")).name}, '
                      + (f'стр. {page + 1}' if isinstance(page, int) else 'страница неизвестна'))
    return answer + '\n\nИсточники:\n' + '\n'.join(legend)
