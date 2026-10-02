# Проверенное состояние FAISS v2

Этап от 2026-10-02. `SnapshotVerifier` и `VerifiedIndexRetriever` работают с
[индексами schema v2](index-v2.md). Старый Gemini `IndexRetriever` сохранён;
основные CLI/бот/web ещё не переключены на новый общий runtime.

## Контракт

```python
from rag.verification import SnapshotVerifier
from rag.index import VerifiedIndexRetriever

verifier = SnapshotVerifier(data_root)
verifier.full_verify(topic)  # до readiness; всегда полный verify после рестарта
verifier.start()
retriever = VerifiedIndexRetriever(verifier, topic, embeddings)
try:
    documents, lease = retriever.retrieve_with_lease(question)
    verifier.validate_lease(lease)  # перед передачей evidence внешнему provider
    # generation выполняется отдельно, с privacy policy и token budget
    verifier.validate_lease(lease)  # перед выдачей результата пользователю
finally:
    verifier.close()  # в приложении — только при остановке общего runtime
```

Runtime создаёт один verifier на root и переиспользует его для тем/интерфейсов.
Не создавать verifier на каждый запрос. `retrieve_with_lease` возвращает документы
и закреплённый snapshot; `invoke` совместим с прежним retriever API, но финальная
проверка перед генерацией/выдачей остаётся обязанностью вызывающего pipeline.

Warm path читает только небольшой `active.json` (до 16 KiB) и состояние кэша.
Он не обходит корпус и не хеширует PDF/FAISS. Новый указатель требует одного
полного verify; конкурентные запросы используют общий результат. Ошибочный
указатель не запускает повторную полную проверку на каждом вопросе.

Snapshot включает index, immutable version bundle, corpus/chunks/embedding IDs,
метаданные файлов, время проверки и generation отзыва. Он действителен только
в создавшем его verifier. Любой новый процесс обязан проверить индекс заново.
Сам FAISS и внутренние dict доступны только доверенному runtime: caller не должен
изменять загруженный индекс или metadata на месте.

## Расписание и отказ

- Watcher сравнивает состав/size/mtime_ns/inode/mode файлов каждые 30 секунд.
- Полная SHA256-проверка начинается через 3600 секунд после успешного завершения
  предыдущей. Полные проверки последовательны; отдельный watcher продолжает работать.
- Через 7200 секунд с последнего успеха admission и lease validation отказывают,
  даже если фоновая проверка ещё не закончилась. Время измеряется monotonic clock.
- Ошибка/изменение/гонка закрывает тему. Фоновые повторные попытки ограничены
  интервалом 30 секунд; нельзя считать ошибку успехом и продлевать expiry.

`invalidate(topic, reason)` вызывается сразу при штатной публикации, restore,
изменении registry/policy/config. Затем `full_verify(topic)` и отдельная проверка
privacy policy разрешают снова открыть тему. Watcher — страховка от внешних
изменений, а не замена этим событиям. Политика доступа ещё реализуется отдельно:
проверка целостности сама по себе не разрешает передачу документов.

При обычном изменении окно обнаружения примерно 30 секунд плюс длительность
stat scan/планирование. Изменение с сохранением stat attributes обнаруживается
полным SHA scan; максимальная допустимая давность проверки — 7200 секунд.
Это не защита от компрометации владельца VPS. Writer доверенный, runtime mounts
должны быть read-only. Нельзя редактировать опубликованные версии на месте.

Worker выполняет только одну полную проверку за раз, но CPU/I/O приоритеты
процесса и общий предел памяти должен обеспечить deployment/runtime. При refresh
возможна временная память старого pinned и нового индекса: бюджет 8 ГБ и LRU
ещё требуют интеграции и нагрузочного измерения. `close()` выставляет stop и
ожидает потоки ограниченное время; текущий disk scan не прерывается принудительно.

## Проверки

Unit tests используют реальный FAISS v2 и управляемые часы: 100 warm запросов
без full load/stat scan, конкурентный startup/new pointer, expiry, watcher,
stat-preserving tamper, гонка scan, отзыв старого lease и ограничение retries.
Проверяется также retrieval с сохранением source/page/embedding metadata.
Это не заменяет Linux нагрузочную проверку или работу готового serving pipeline.
