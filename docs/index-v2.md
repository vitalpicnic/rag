# FAISS schema v2: сборка, активация и откат

Административный этап от 2026-10-01. Старые `current.json`, версии Gemini и
CLI v1 не изменяются. V2 ещё не подключён к serving-интерфейсам: общий runtime,
model-owner lock и кэш проверки будут внедрены отдельно.

## Формат

Версии находятся в `faiss_indexes/<topic>/v2/versions/<version>/`:

- `index.faiss`: только FAISS IndexFlatL2;
- `chunks.jsonl`: текст и metadata; порядок строк соответствует номерам векторов;
- `manifest.json`: schema 2, corpus/chunks/embedding IDs, профиль, размерность,
  количество строк, processing settings и SHA256 артефактов.

`v2/active.json` атомарно выбирает весь bundle, включая embedding profile ID и
manifest hash. Одинаковая размерность разных профилей не делает их совместимыми.
Загрузчик v2 не читает pickle. Manifest/hashes защищают целостность при доверенном
writer, а не удостоверяют файлы, полностью контролируемые атакующим.

## Сборка и переключение

Остановите bot/web/CLI/runtime перед изменениями. Флаг `--maintenance` подтверждает
это действие оператора; он пока **не останавливает процессы автоматически**.
Команды выполняются из рабочего checkout, в Python-окружении с CPU зависимостями
из [инструкции embeddings](local-embeddings.md).

```powershell
$env:RAG_PROFILE = 'LOCAL'
$env:RAG_EMBEDDING_PROVIDER = 'local'
$env:RAG_EMBEDDING_CACHE = 'model_cache/e5'
python -m scripts.index_admin build --topic Банкинг --maintenance --output reports/bundle-a.json
python -m scripts.index_admin verify --topic Банкинг --bundle reports/bundle-a.json
python -m scripts.index_admin activate --topic Банкинг --maintenance --bundle reports/bundle-a.json
```

Сборка публикует immutable directory, но сама не меняет active pointer.
Каждая сборка получает отдельный version ID. Готовые фрагменты переиспользуются.
Сохраните bundle JSON для отката; повторная запись существующего output запрещена.

На Linux данные и каталоги синхронизируются через fsync; staging и final directory
должны находиться на одной файловой системе. После полного verify staging
переименовывается в final version, затем отдельная команда меняет указатель через
`os.replace`. Один writer удерживает build.lock. При аварийном завершении lock
может остаться: удалять его можно только после проверки, что writer точно завершён.
Незавершённые staging directories не активируются и автоматически не удаляются.

```powershell
python -m scripts.index_admin rollback --topic Банкинг --maintenance --bundle reports/bundle-a.json
```

Rollback не выполняет embedding. Корпус должен соответствовать старому bundle.
Если документы изменились, сначала восстановите их проверенную копию в отдельный
root и используйте `--root`. Новый корпус разрешено активировать вместо старого;
восстановленный после ошибки старый pointer с несовпадающим корпусом остаётся
непригодным к загрузке, пока не восстановлены документы.

Python API `activate_bundle(..., warmup=callback)` откатывает pointer при ошибке
прогрева. Caller обязан закрыть admission, завершить запросы и выгрузить старую
модель; только после успешного прогрева снова открыть доступ. CLI сейчас проверяет
артефакты, но не обещает готовность модели/службы. При ошибке восстановления
указателя нельзя открывать serving; требуется ручное восстановление.

## Legacy migration: только доверенный input

`scripts/migrate_index.py` — отдельный административный инструмент, содержащий
pickle loader. Он не импортируется serving-кодом v2. Загружать произвольный
присланный индекс запрещено даже при совпадении SHA256.

Для реальной миграции требуется одноразовый **non-root Linux контейнер**:

1. `--network none`, без API secrets и Docker socket.
2. В `/input` — только проверенная копия `data` и `faiss_indexes`, read-only;
   не монтировать весь домашний каталог или проект с `.env`.
3. В `/output/data` — read-only копия того же корпуса; `/output/faiss_indexes`
   доступен на запись. Остальная файловая система контейнера read-only.
4. Внутри запустить `python -m scripts.migrate_index --input-root /input
   --output-root /output --topic Банкинг --trusted-input --isolated` одной строкой.

`--isolated` — утверждение оператора, не автоматическое доказательство контейнерной
изоляции. CLI дополнительно запрещает Windows/root, secrets в окружении и socket
connect. Эти проверки не превращают враждебный pickle в безопасный.
Сборка tools image и реальный контейнерный прогон относятся к deployment-этапу.

Миграция проверяет все v1 hashes, соответствие vector row→chunk ID, тексты и metadata
docstore; переносит исходные векторы без API/переэмбеддинга и ничего не активирует.
Профиль Google помечается `legacy-unversioned`, поскольку immutable revision
исторического API неизвестна. До подключения Google v2 retriever нужно явно
согласовать именно этот legacy profile, а не приравнивать его к E5.

## Проверки и ограничения

Unit tests покрывают повреждение артефактов, mismatch профилей одной размерности,
сбой embeddings, fsync/replace, ошибки прогрева, concurrent writer lock, смену
корпуса, rollback и доверенную миграцию синтетического legacy индекса.
Проверка реального CPU E5 build/activate/rollback выполняется отдельно.
Windows не проверяет POSIX directory fsync: Linux SIGKILL/power-loss scenarios и
контейнерная миграция пока не пройдены. Готовность production не заявляется.
