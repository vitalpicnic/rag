# Локальные embeddings и подготовка фрагментов

Этап разработки от 2026-10-01. Ollama и ключ Gemini не нужны для этих операций.
Существующие Gemini-индексы и `current.json` не изменяются. Новый общий runtime
и публикация FAISS schema v2 — следующие этапы, пока не подключённые к UI/боту.

## Установка и загрузка модели

В отдельном окружении Python установите зависимости:

```bash
python -m pip install -r requirements-bot.txt -r requirements-embeddings.txt
python -m scripts.bootstrap_model --destination model_cache/e5 --revision 614241f622f53c4eeff9890bdc4f31cfecc418b3
```

Bootstrap — единственная операция, которой требуется сеть. Он загружает
`intfloat/multilingual-e5-small` по неизменяемому commit SHA и создаёт manifest
с SHA256 локальных файлов. Существующий каталог не перезаписывается; если загрузка
прервалась, укажите другой новый destination. Каталог `model_cache/` исключён из Git.
Хеши контролируют целостность, но не удостоверяют происхождение при подмене
самого manifest: загружать модель должен доверенный оператор.

Runtime использует CPU/float32, максимум 512 токенов на фрагмент, mean pooling,
нормализацию и префиксы `query: ` / `passage: `. Настройки включены в embedding ID.
Длинный ввод обрабатывается tokenizer с ограничением 512 токенов; это не замена
проверке общего context budget генерации. [E5](https://huggingface.co/intfloat/multilingual-e5-small),
[локальная загрузка SentenceTransformer](https://sbert.net/docs/package_reference/sentence_transformer/model.html).

## Подготовка и повторное использование

```bash
python -m scripts.prepare_local --root . --topic Банкинг
```

Результат сохраняется в `prepared_corpora/v2/<topic>/<chunks_id>/`:
JSONL фрагментов и manifest. При повторном запуске проверяется хеш JSONL,
повторное извлечение текста не выполняется. Повреждение вызывает отказ;
существующая версия не исправляется молча.

| Идентификатор | Зависит от | Не зависит от |
|---|---|---|
| corpus_id | Относительные пути и SHA256 PDF/TXT | LLM, embeddings, разбиение |
| chunks_id | corpus_id, extraction/chunking/metadata versions | LLM, embeddings |
| embedding_profile_id | Модель/revision, dimensions, tokenizer, pooling, prefixes, normalization, runtime versions | LLM, история, интерфейс |

Политика источников не входит в эти идентификаторы и должна применяться отдельно.
Новый профиль embeddings потребует отдельного индекса, даже если размерность
совпадает. Смена LLM не меняет фрагменты и векторы.

## Проверка без сети

```bash
python -m scripts.smoke_embeddings --cache model_cache/e5
```

Тест запрещает `socket.connect`, загружает локальную модель, получает реальные
векторы, проверяет повторяемость, строит небольшой FAISS IndexFlatL2 и ищет ответ
на русском. Успех — JSON `status=passed`; модель и runtime versions указаны в нём.
Это проверка embeddings/retrieval, а не качества аналитических ответов или VPS.

Настройки фабрики: `RAG_EMBEDDING_CACHE`, `RAG_EMBEDDING_BATCH_SIZE` (8 по умолчанию),
`RAG_EMBEDDING_THREADS` (2). Для LOCAL нужен `RAG_PROFILE=LOCAL`; отсутствие cache
завершает загрузку ошибкой, без скачивания и без обращения к Google. Объект
embedding повторно используется вызывающим runtime; модель не создаётся на запрос.

Зависимости фиксируют прямые версии. Linux/Python 3.12, hash-locked установка,
лимиты 8 ГБ и общий model-owner lock ещё требуют проверки этапа deployment/runtime.
