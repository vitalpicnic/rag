# Офлайн-эксперимент BM25/RRF

`rag.sparse` использует те же chunk_id, NFKC/casefold и числовые токены с
нормализацией десятичной запятой. Индекс — JSON-compatible объект с chunks_id
и версией tokenizer. Несовпадение fingerprint отклоняется. BM25: k1=1.2, b=0.75.
RRF: равные веса, k=60, удаление дубликатов, tie-break по chunk_id.
Расстояния FAISS и оценки BM25 не складываются.

При остановленном runtime:

```bash
python -m scripts.sparse_index --root /srv/rag --topic bank --question 'активы 42,15' --output /private-reports/sparse.json
```

Команда проверяет активный v2 bundle, берёт maintenance lock, формирует кандидатов
без LLM/embeddings, не изменяет FAISS и не перезаписывает существующий отчёт.
Основной retrieval остаётся dense. Сравнение C против B на одинаковых gold,
LLM и prompts не выполнено; улучшение качества не заявляется. Production toggle
не добавлен до сравнительной оценки. API `fuse(dense, sparse)` принимает два
упорядоченных списка `(chunk_id, score)`; исходные scores игнорируются.

`--save-index /private-reports/sparse-index.json` сохраняет JSON store; он содержит
лексические признаки документов и требует тех же прав доступа, что корпус.
`--dense-results dense.json` включает RRF для заранее полученного dense ranking
формата `{"chunks_id":"...","results":[["chunk_id",0.1]]}`. Другой fingerprint
или неизвестный chunk_id отклоняется. Подайте ranking того же вопроса; сам текст
вопроса в отчёте не сохраняется. FAISS и production retrieval не изменяются.
