# Local-first Hybrid RAG — Implementation Plan

**Цель:** перенести существующий RAG на Ubuntu 24.04 x86_64, 4 vCPU / 8 ГБ RAM
с локальными embeddings/FAISS и внешней генерацией, сохранив интерфейсы и источники.

**Архитектура:** один внутренний RAG runtime владеет моделью, индексами, историей
и очередью. CLI, Telegram и Streamlit — лёгкие адаптеры. Индексация выполняется
в отдельном окне обслуживания; Ollama и улучшения retrieval подключаются позднее.

**Стек:** Python 3.12, Docker Compose, FAISS CPU, multilingual-e5-small,
Gemini/OpenAI-compatible API; опционально Ollama, BM25 и CPU cross-encoder.

**Спецификация:** [утверждённая ревизия 3](../specs/2026-09-29-local-first-vps-design.md),
архитектурное содержимое зафиксировано коммитом `82eb42e`.
Пользователь подтвердил спецификацию и затем отдельно разрешил реализацию
2026-09-30. План от 2026-09-29 исторический; его задачи не выполняются.

Текущий статус: задача 0 выполняется в ветке `feat/local-first-vps`.
Baseline wrapper и 10 его тестов готовы; полный локальный suite — 88 passed,
0 failures/errors/skipped. Живой A заблокирован отсутствием ключа Gemini,
разрешения на передачу корпуса и бюджета API. Retrieval/generation не изменены.

**Выполнение:** последовательно в текущей сессии, без субагентов, с применением
Superpowers executing-plans. Здесь только проектные решения; инструкции навыков
не копируются. Production-код в ходе подготовки плана не изменяется.

## Общие ограничения

- Основной VPS: 4 vCPU, 8 ГБ RAM, Ubuntu 24.04 LTS x86_64, без GPU, начально 80 ГБ SSD.
- Один runtime worker, один активный запрос, очередь 8; таймаут ожидания очереди 60 с.
- Runtime: 3,5 GiB / 2,5 CPU; bot: 384 MiB / 0,25 CPU; web: 768 MiB / 0,5 CPU;
  collector: 128 MiB / 0,25 CPU. Indexer вместо runtime: 4,5 GiB / 3 CPU,
  batch 8, threads 2. Запас хоста ≥1,5 GiB по фактическому MemTotal.
- FAISS+docstore cache ≤512 MiB; 100 000 chunks — защитный предел, не обещание ёмкости.
- Watcher 30 с; полный SHA scan через 3600 с после успешного scan; срок проверки
  7200 с, затем fail-closed. Startup/publish/restore/activation требуют полного verify.
- Input cap 16 384; история ≤800 реальных токенов; outputs text/overview/executive/expert
  600/1800/1800/3000; margin max(256, 5% эффективного лимита); thinking по контракту модели.
- Сохраняются text/overview/executive/expert, официальный приоритет с альтернативами,
  источники/страницы, SQLite-графики и PPTX; несвязанный UI-рефакторинг исключён.
- SHA не аутентифицирует pickle. Новый runtime не читает pickle. Старый Gemini
  индекс и образ остаются доступными для отката; миграция только доверенного входа.
- Production TG без allowlist не стартует. Web только 127.0.0.1:8501/SSH tunnel;
  внутренний runtime API без host port. Runtime не имеет Docker socket.
- LOCAL не обращается в cloud и не делает fallback. HYBRID external_generation=deny
  по умолчанию, source deny сильнее topic allow; countTokens тоже требует разрешения.
- Секреты через env либо *_FILE, конфликт — ошибка; тексты/ключи не логируются.
- Корпус, модели, отчёты и backups не коммитятся; отчёты с текстом доступны оператору,
  retention 30 дней. Корпус и pointers не удаляются автоматически при обновлении.
- Сначала живой A, затем изменения поведения. Mock/API-free tests не заменяют A.
  Windows tests не заменяют Linux/8 ГБ приёмку. Отсутствующий ресурс = blocked/not_run.
- Docker Skills не обнаружены в текущем наборе. При реализации проверяются официальные
  документы Docker и источники §6 спецификации; зависимости и digests фиксируются
  после реальной сборки, не выдумываются в плане. Kubernetes/LoRA/SFT вне работ.

## Файлы и границы компонентов

Существующие точки входа: `main.py`, `bot.py`, `app.py`, `build_index.py`,
`build_tables.py`. `rag/engine.py` собирает chain, `rag/pipeline.py` выполняет
retrieval/generation, `rag/index.py` управляет FAISS; `rag/history.py`,
`rag/evidence.py`, `rag/sources.py`, `rag/tables.py`, `rag/slides.py` сохраняют роли.
Существующие тесты используют unittest; команды ниже запускаются из корня проекта.

| Новые файлы | Ответственность |
|---|---|
| `rag/config.py`, `rag/embeddings.py`, `rag/generation.py` | Настройки и независимые адаптеры providers |
| `rag/index_schema.py`, `rag/index_store.py`, `rag/verification.py` | IDs/schema, v2 build/activation, VerifiedSnapshot |
| `rag/privacy.py`, `rag/token_budget.py` | Политика передачи и подсчёт полного payload |
| `rag/runtime.py`, `rag/runtime_api.py`, `rag/runtime_client.py` | Один владелец модели, внутренний HTTP JSON API и adapters |
| `rag/health.py`, `rag/resources.py` | Readiness/liveness, лимиты и мониторинг |
| `rag/sparse.py`, `rag/reranking.py` | Опциональный BM25/RRF и reranking |
| `scripts/capture_baseline.py`, `scripts/migrate_index.py`, `scripts/index_admin.py` | Baseline wrapper, изолированная миграция, административные операции |
| `scripts/bootstrap_model.py`, `scripts/smoke_local.py`, `scripts/benchmark_rag.py` | Загрузка модели, offline smoke, измерения |
| `Dockerfile`, `.dockerignore`, `compose.yaml`, `compose.local.yaml` | CPU сборка и основной/локальный deployment |
| `requirements-linux.in`, `requirements-linux.lock`, `requirements-tools.lock` | Linux CPU runtime и воспроизводимый набор инструментов проверки |
| `deploy/vps.env.example`, `deploy/ollama/compose.yaml`, `deploy/ollama/gpu.env.example` | Несекретные конфигурации VPS/отдельной GPU-ноды |
| `scripts/bootstrap-vps.sh`, `scripts/backup.sh`, `scripts/restore.sh` | Подготовка хоста, согласованный backup и restore |
| `.github/workflows/local-first.yml`, `docs/deployment.md` | Проверки CI и операторский runbook |

Новые модули имеют узкие обязанности; существующий `rag/index.py` остаётся
совместимым фасадом. Не переносить всё приложение между каталогами.

Контракты создаются в модуле владельца, не дублируются между задачами:

| Тип | Владелец | Обязательные поля |
|---|---|---|
| `Settings` | `rag/config.py`, задача 2 | Валидированные параметры §4.1 spec и лимиты этого плана; секреты исключены из repr/serialization |
| `ModelCapabilities` | `rag/generation.py`, задача 2 | input_limit, output_limit, context_limit (optional), tokenizer/count contract, framing и thinking policy |
| `Bundle` | `rag/index_schema.py`, задача 3 | schema_version, index_version, corpus_id, chunks_id, embedding_profile_id, manifest_sha256, relative_path |
| `VerifiedSnapshot` | `rag/verification.py`, задача 5 | bundle, loaded store, signatures, registry_id/policy_id, generation, verified_at, expires_at |
| `PolicyLease` | `rag/privacy.py`, задача 6 | topic, source_ids, provider, endpoint, policy_id, generation |
| `PackedRequest` | `rag/token_budget.py`, задача 7 | messages, evidence, input_tokens, output_reserve, thinking_reserve, margin |

Служебные интервалы и lease expiry используют monotonic clock; wall-clock UTC
сохраняется отдельно в отчётах. В API передаются JSON-данные, не Python objects/pickle.
Runtime API реализуется на FastAPI/Uvicorn с одним worker; synchronous pipeline
исполняется через собственную единственную очередь runtime, не через независимые
web workers. Health handlers остаются отзывчивыми во время inference. Версии
FastAPI/Uvicorn и provider SDK фиксируются Linux lock-файлом после проверки совместимости.

## Особое внимание при проверке

| Условие | Ожидаемое поведение | Тест в задаче |
|---|---|---|
| Кириллица, Unicode, длинный вопрос, число с единицей | Не обрезать смысл и citation header; отказ при непомещающемся вопросе | 7 |
| Symlink/path traversal в topic/source/архиве | Не читать/писать за разрешённым root | 3, 9, 11 |
| Клиент отменил запрос, но inference ещё работает | Слот не освобождать до реального завершения; не загрузить вторую модель | 8 |
| Отзыв прав/порча индекса во время запроса | Запретить внешний вызов и выдачу результата | 5, 6, 9 |
| Диск закончился или процесс убит при публикации | Старый bundle остаётся валиден; после рестарта нет смешанного профиля | 4 |

## Последовательность и контрольные точки

Задачи 0–1 — baseline gate; 2–7 — providers/индексы/защита; 8–10 — рабочий HYBRID;
11 — production эксплуатация; 12–14 — опциональные эксперименты; 15 — итоговый отчёт.
Операционная подготовка задачи 11 не зависит от опциональных 12–14. Каждая задача
завершается отдельным проверяемым коммитом только после своего green gate.
При изменении библиотек сохраняются исходные requirements для воспроизведения A.

### Задача 0. Зафиксировать исходный Gemini без изменения поведения

**Вход:** утверждён этот план; доступны разрешённый corpus, API credentials и бюджет
вызовов. До их появления retrieval/generation задачи не начинаются.
**Файлы:** создать `scripts/capture_baseline.py`, `tests/test_baseline.py`;
читать `scripts/evaluate_rag.py`, `tests/fixtures/rag_eval_cases.json`, исходный `b2c8022`.
**Интерфейс:** `capture_baseline(checkout: Path, corpus: Path, output: Path) -> Path`;
output — новый закрытый каталог, не перезаписываемый отчёт и raw results.

- [x] Написать tests `test_refuses_existing_output`, `test_never_modifies_source_root`,
  `test_missing_credentials_is_blocked`: исходные hashes неизменны, нет API при missing prerequisites.
- [x] Выполнить `python -m unittest discover -s tests -p test_baseline.py -v`;
  увидеть FAIL из-за отсутствующего wrapper, а не неверного окружения.
- [x] Реализовать wrapper: отдельный checkout `b2c8022`, копия corpus, исходные options/prompts;
  если index отсутствует, явно build только внутри этого checkout. Записать commit,
  package versions, requested/returned model IDs, corpus/dataset/artifact hashes,
  время, raw ответы, usage, ошибки; запускать исходный evaluator без изменения prompts.
- [ ] Повторить тесты — PASS; прогнать исходный suite в baseline checkout, сохранить
  JSON/log с passed/failed/errors/skipped. Запустить evaluator `--run --output` там же.
  Отчёт A с реальными ответами обязателен; отсутствие immutable model version отметить.
- [ ] Сохранить коммит `test: capture immutable Gemini baseline`; output вне Git.

**Откат:** остановить wrapper; исходный root остаётся нетронутым. Отчёт не удалять.

### Задача 1. Измеримый evaluator и контроль сопоставимости

**Вход:** raw A сохранён; до задачи 2 завершить измеренный baseline gate.
**Файлы:** изменить `rag/evaluation.py`, `scripts/evaluate_rag.py`, `scripts/compare_models.py`,
`tests/test_evaluation.py`; добавить `tests/fixtures/rag_eval_roles.json`.
**Интерфейс:** `score_case(case: dict, observation: dict, review: dict | None) -> dict`;
`compare_runs(reference: dict, candidate: dict) -> dict` с raw/guarded метриками.

- [ ] Добавить `test_recall_at_k_source_page`, `test_wrong_period_numeric_is_wrong`,
  `test_unreviewed_not_verified`, `test_four_explicit_modes`: Recall@3/5/10 имеет
  явный denominator, пустой gold = N/A; Decimal совпадение без периода не засчитывается.
- [ ] Запустить `python -m unittest discover -s tests -p test_evaluation.py -v` — новые tests FAIL.
- [ ] Добавить явный mode, retrieved candidates до selection и окончательный context,
  numeric tuples/entity/period/unit/scope, manual supporting-evidence annotations,
  refusal/role/latency/resource/usage. Legacy 30 cases показывать отдельным slice.
  Дополнительный adapter вокруг frozen A только наблюдает retrieval, не меняет его.
- [ ] Повторить tests — PASS; проверить новые gold facts по исходным PDF;
  провести живой A на дополненном наборе и review атомарных утверждений. Сохранить
  измеренный отчёт и baseline hashes до любого следующего изменения поведения.
- [ ] Коммит `test: establish measurable RAG quality gates`.

**Приёмка:** результаты A воспроизводимо привязаны к модели/коду/корпусу;
структурные citation checks не называются factual verification.
**Откат:** прежний evaluator и неизменный raw A; новый dataset остаётся отдельным.

### Задача 2. Конфигурация и независимые generation providers

**Вход:** baseline gate выполнен. **Файлы:** создать `rag/config.py`, `rag/generation.py`,
`tests/test_config.py`, `tests/test_generation.py`; изменить `rag/models.py`,
`rag/engine.py`, `rag/metrics.py`, `build_index.py` и тесты моделей/метрик.
**Интерфейсы:** `load_settings(env: Mapping[str, str]) -> Settings`;
`create_generation(settings: Settings) -> GenerationProvider`;
provider: `invoke(messages: list, mode: str) -> AIMessage`,
`count_tokens(messages: list) -> int`, `capabilities: ModelCapabilities`.
Capabilities фиксирует input/output/context limits, framing и thinking contract.

- [ ] Написать `test_env_file_conflict`, `test_hybrid_without_google_key`,
  `test_local_never_falls_back`, `test_compatible_no_gemini_options`,
  `test_usage_provider_label`: неизвестный provider/contract в production отклоняется.
- [ ] Запустить отдельно discover для `test_config.py` и `test_generation.py` — FAIL.
- [ ] Реализовать настройки §4.1 spec, секреты и Gemini/OpenAI-compatible adapters;
  Ollama контракт зарезервировать до задачи 12. Google embedding ключ требовать
  только при google embedding; не отправлять thinking options чужому API.
- [ ] Повторить tests — PASS, затем `python -m unittest discover -s tests -v`.
  Проверить frozen payload/options для Gemini; новые budget/privacy policy пока
  не выдавать за неизменность A. Ошибки провайдера не пишут index.
- [ ] Коммит `feat: decouple generation providers and settings`.

**Откат:** прежний commit/image и конфигурация; текущие индексы неизменны.

### Задача 3. CPU embeddings и независимые IDs

**Вход:** задача 2. **Файлы:** создать `rag/embeddings.py`, `rag/index_schema.py`,
`scripts/bootstrap_model.py`, `tests/test_embeddings.py`, `tests/test_index_schema.py`;
изменить `rag/index.py`, `build_index.py`, `tests/test_index_config.py`.
**Интерфейсы:** `create_embeddings(settings: Settings) -> Embeddings`;
`corpus_id(files: list[dict]) -> str`, `chunks_id(corpus: str, processing: dict) -> str`,
`embedding_profile_id(profile: dict) -> str`. Canonical JSON/SHA сериализация версионируется.

- [ ] Добавить tests: LLM смена не меняет ни один ID; extraction settings меняют chunks;
  revision/prefix/normalization/tokenizer меняют embedding ID даже при одинаковой dimension;
  path traversal/symlink outside corpus отвергаются; официальный registry ID отдельный.
- [ ] Выполнить discover `test_embeddings.py`, `test_index_schema.py`, `test_index_config.py` — новые FAIL.
- [ ] Реализовать E5-small 384 CPU, query:/passage: prefixes, pinned revision,
  tokenizer/max length/pooling/normalization в profile, локальный cache, batch 8/threads 2.
  Bootstrap загружает конкретную revision и manifest; runtime offline, без auto download.
  Prepared chunks повторно используются по chunks_id, legacy fingerprint не переписывается.
- [ ] Повторить tests — PASS; реальным encode подтвердить dimension/детерминированность
  и offline failure при отсутствующем cache; сетевой bootstrap отмечать отдельно.
- [ ] Коммит `feat: add local embeddings and independent index identities`.

**Откат:** выбрать google profile/старый root; новые prepared/model artifacts сохраняются отдельно.

### Задача 4. Schema v2, миграция, публикация и rollback

**Вход:** задача 3. **Файлы:** создать `rag/index_store.py`, `scripts/migrate_index.py`,
`scripts/index_admin.py`, `tests/test_index_store.py`; изменить `rag/index.py`, `tests/test_index.py`.
**Интерфейсы:** `build_version(root: Path, topic: str, settings: Settings) -> Bundle`;
`activate_bundle(root: Path, topic: str, bundle: Bundle) -> None`;
`rollback_bundle(root: Path, topic: str, previous: Bundle) -> None`.
Bundle schema включает index_version, corpus_id, chunks_id, embedding_profile_id,
manifest SHA и относительный путь. JSONL docstore и mapping проверяются до FAISS load.

- [ ] Добавить tests same-dimension mismatch, corrupt mapping, interrupted build,
  disk-full/fsync/rename failures, concurrent writers и warmup failure.
  Assertions: active pointer содержит целиком старый или новый bundle; legacy bytes неизменны.
- [ ] Выполнить `python -m unittest discover -s tests -p test_index_store.py -v` — FAIL.
- [ ] Реализовать staging→verify→fsync→rename version→fsync directory→atomic active.json;
  publish lock, same filesystem, immutable namespaces. Admin активация закрывает admission,
  drain/unload старого model owner перед warmup; ошибка возвращает previous verified bundle.
- [ ] Реализовать trusted migration копии pickle в отдельном non-root tools контейнере
  без сети/секретов, read-only input. Сверять vectors/order/IDs/source hashes; новый
  runtime никогда не вызывает pickle.load. Недоверенный pickle отклонять, не «санитизировать».
- [ ] Повторить tests — PASS; fault injection на Linux во всех границах публикации,
  успешный forward/back switch без embed_documents и без перезаписи Gemini namespace.
- [ ] Коммит `feat: publish and roll back verified v2 index bundles`.

**Откат:** admin выбирает предыдущий bundle с matching corpus; иначе отдельная
проверенная копия corpus/root. Старый image читает свой неизменный legacy namespace.

### Задача 5. VerifiedSnapshot вне горячего пути

**Вход:** задача 4. **Файлы:** создать `rag/verification.py`, `tests/test_verification.py`;
изменить `rag/index.py` и `rag/index_store.py`.
**Интерфейсы:** `SnapshotVerifier.acquire(topic: str) -> VerifiedSnapshot`;
`invalidate(topic: str, reason: str) -> None`, `validate_lease(snapshot: VerifiedSnapshot) -> None`;
`full_verify(topic: str) -> VerifiedSnapshot`. Clock и event source инъецируемы в tests.

- [ ] Написать `test_warm_queries_do_not_hash`, `test_coalesced_new_pointer_verify`,
  `test_stat_preserving_tamper_periodic`, `test_7200_expiry`, `test_inflight_revocation`;
  проверить startup/restore/full verify и гонку pointer во время scan.
- [ ] Выполнить discover `test_verification.py` — FAIL.
- [ ] Реализовать cache key root/topic/bundle/manifest/schema; watcher 30 с,
  periodic 3600 с, expiry 7200 с; один scan, низкий CPU/I/O priority. Hot path
  читает только маленький pointer/stat/cache, без обхода corpus и full SHA.
  Mismatch/read error закрывает readiness; до/после scan сверяется generation/signature.
- [ ] Повторить tests — PASS; instrumented 100 warm queries дают 0 full-hash calls,
  смена pointer ровно один verify; незаконченный scan не продлевает expiry.
- [ ] Коммит `perf: cache verified snapshots with bounded integrity freshness`.

**Откат:** прежний verifier/образ с полной проверкой; integrity никогда не выключается.

### Задача 6. Privacy policy и отзыв разрешений

**Вход:** задачи 2, 5. **Файлы:** создать `rag/privacy.py`, `tests/test_privacy.py`;
изменить `rag/sources.py`, `rag/pipeline.py`, тесты источников.
**Интерфейс:** `authorize_external(topic: str, source_ids: list[str], provider: str,
endpoint: str, policy: dict) -> PolicyLease`; `validate_policy_lease(lease: PolicyLease) -> None`.

- [ ] Tests production default deny, source deny overrides topic allow, forbidden endpoint,
  policy revoked in-flight: ни countTokens, ни generation не вызваны после отказа.
- [ ] Выполнить discover `test_privacy.py` — FAIL.
- [ ] Добавить versioned topic/source policy отдельно от embeddings; allow только
  оператором заданных endpoints; закрыть arbitrary request URL. Проверять lease до
  каждого внешнего вызова и выдачи результата. LOCAL блокирует внешние providers.
- [ ] Повторить tests — PASS; редактирование policy не меняет FAISS/embedding IDs;
  журнал не содержит snippets, prompts, абсолютных путей или ключей.
- [ ] Коммит `feat: enforce explicit external data policies`.

**Откат:** остановить внешний сервис/закрыть admission. Нельзя откатывать к открытому
production поведению; старый образ допустим только в изолированном закрытом режиме.

### Задача 7. Token-aware packing полного запроса

**Вход:** задачи 2, 6. **Файлы:** создать `rag/token_budget.py`, `tests/test_token_budget.py`;
изменить `rag/history.py`, `rag/evidence.py`, `rag/pipeline.py` и их тесты.
**Интерфейс:** `pack_request(system: str, question: str, history: list, evidence: list,
mode: str, provider: GenerationProvider, lease: PolicyLease | None) -> PackedRequest`.
PackedRequest содержит окончательные messages, selected evidence и token accounting.

- [ ] Добавить tests RU/Unicode/таблиц, всех 4 ролей, длинной истории и вопроса;
  assert system+question+history+evidence+framing+margin <= input limit;
  для общего окна добавить output/thinking reserve. Whole evidence/header/number intact.
- [ ] Выполнить discover `test_token_budget.py` — FAIL.
- [ ] Реализовать §4.1.2: input cap 16384, history 800, output 600/1800/1800/3000,
  margin max(256, ceil(0.05*effective_limit)); сначала убрать старые полные turns,
  затем whole evidence, сохранить официальный приоритет/альтернативы при вместимости.
  Пересчитать финальный payload точным provider counter после privacy gate;
  неизвестный contract/count error/непомещающийся вопрос → отказ до generation.
- [ ] Повторить tests — PASS; проверить provider usage discrepancy logging без текста.
  Изменения packing оценить отдельным hardening control arm относительно frozen A,
  затем использовать одинаковый packing при A/B, не приписывать эффект embeddings.
- [ ] Коммит `feat: bound complete generation requests by model tokens`.

**Откат:** предыдущий согласованный packing только в закрытом control arm;
production без точного бюджета не продвигается.

### Задача 8. Общий runtime, очередь и model ownership

**Вход:** задачи 3–7. **Файлы:** создать `rag/runtime.py`, `rag/resources.py`,
`tests/test_runtime.py`; изменить `rag/engine.py`, `rag/bot_support.py`.
**Интерфейсы:** `RagRuntime.query(request: dict) -> dict`, `drain() -> None`,
`readiness(topic: str | None) -> dict`; request содержит principal/session/topic/mode/question.
Runtime использует GenerationProvider, SnapshotVerifier и PackedRequest из предыдущих задач.

- [ ] Написать tests 1 model initialization across topics, active_count<=1,
  queue<=8/timeout 60 с, cancellation holds slot, model-owner lock excludes indexer,
  512 MiB LRU eviction/pinned protection и allocation refusal для oversized index.
- [ ] Выполнить discover `test_runtime.py` — FAIL.
- [ ] Реализовать один worker, общую bounded очередь, session keys principal/topic/mode,
  bounded index cache и conservative size estimate перед загрузкой; memory limit
  дополнительно обеспечивается cgroup. Нет per-request/adapter model creation.
  Maintenance закрывает admission, drain, выгружает и освобождает owner lock для indexer.
- [ ] Повторить tests — PASS; убедиться, что реальное выполнение после client timeout
  удерживает слот; hung worker завершает процесс, не открывает второе выполнение.
- [ ] Коммит `feat: centralize RAG model ownership and admission`.

**Откат:** остановить runtime, выбрать прежний image; индекс и данные не менять.

### Задача 9. Внутренний API и сохранение интерфейсов

**Вход:** задача 8. **Файлы:** создать `rag/runtime_api.py`, `rag/runtime_client.py`,
`rag/health.py`, `tests/test_runtime_api.py`; изменить `main.py`, `bot.py`, `app.py`,
`rag/tables.py`, `rag/slides.py`, tests бота/таблиц/слайдов.
**Интерфейс:** HTTP JSON `/query`, `/topics`, `/history/reset`, `/sources/{token}`,
`/exports/{token}`, `/charts/{choice}`, `/health/live`, `/health/ready`;
query response сохраняет answer/sources/режим/статус verification и export token.
`RuntimeClient.query(request: dict) -> dict` соответствует RagRuntime.query.

- [ ] Написать tests bad/missing service secret, principal/session isolation,
  source/export token bound to user/topic, path traversal, allowlist absent/invalid,
  heartbeat stale, повторный polling process; чужой пользователь не вызывает backend.
- [ ] Выполнить discover `test_runtime_api.py` и `test_bot.py` — новые tests FAIL.
- [ ] Реализовать API с единственным worker, service secret и trusted adapter principals;
  запрос не выбирает endpoint/model path. Стрим файлов через API с повторными
  source hash/правами; exports без нового LLM. CLI сохраняет аргументы, фронтенды
  не импортируют model initialization. Development direct CLI допускается только
  с тем же model-owner lock, без параллельного runtime.
- [ ] Подключить fail-closed TG allowlist и persistent polling flock до Telegram init;
  heartbeat от event loop. Readiness проверяет локальную готовность без платных вызовов.
- [ ] Повторить tests — PASS; regression всех режимов, истории, источников,
  SQLite guards/charts и PPTX. Две сессии не получают чужие download/export tokens.
- [ ] Коммит `feat: connect existing interfaces to the shared runtime`.

**Откат:** прежний image за закрытым доступом; production allowlist и privacy сохраняются.

### Задача 10. Воспроизводимый Compose и приёмка HYBRID

**Вход:** задача 9; работающий Linux Docker Engine, model download и внешний API.
**Файлы:** создать Docker/Compose/lock/env файлы из карты выше, `scripts/bootstrap-vps.sh`,
`scripts/smoke_local.py`, `tests/test_deployment.py`, `.github/workflows/local-first.yml`;
изменить существующие requirements только с сохранением совместимых entry points.
**Интерфейс:** `docker compose --profile web config --quiet`; сервисы `runtime`,
`bot`, `web`, `collector`, one-shot `indexer`, `model-bootstrap`, `tools`.
Профили `web`, `maintenance`, `tools`; bot объявлен один раз.

- [ ] Написать tests/rendered-config assertions лимитов, non-root, read-only roots,
  отсутствия Docker socket/публичных ports, restart policies и secrets exclusions;
  отсутствие model/corpus/.env/.git в image context. Запустить discover `test_deployment.py` — FAIL.
- [ ] Создать CPU Python 3.12 multi-stage image с pinned base digest и Linux hash locks,
  CPU torch wheels; `pip check`, hash-locked rebuild на чистом builder. Secrets читает
  приложение, не bake ARG. Mount directories pointers целиком; данные/model RO в runtime,
  RW только нужному admin job; security_opt no-new-privileges, cap_drop ALL, tmpfs tmp.
- [ ] Задать лимиты из общих ограничений, rotation 3×10 MiB, restart unless-stopped
  для runtime/bot/web, no для indexer; readiness/healthcheck без платных API.
  Bootstrap проверяет ОС/MemTotal/диск, ставит Docker из официального apt repository,
  создаёт каталоги/UID permissions; повторный запуск не стирает данные.
- [ ] Выполнить tests — PASS; `docker compose --profile web config --quiet`,
  `docker compose build`, `docker compose run --rm tools python -m unittest discover -s tests -v`.
  В CI без secrets выполнить image build, config и offline tests; live API вручную.
- [ ] Preload модели; `docker run --rm --network none` с RO model cache и отдельным
  writable test root запускает `python -m scripts.smoke_local`: реальный RU embedding,
  FAISS retrieval, source/page; полный pipeline дополнительно с fake LLM.
- [ ] На 4 vCPU/8 ГБ проверить CLI/TG/web с Gemini и OpenAI-compatible; container restart
  сохраняет FAISS/SQLite hashes. Сохранить rendered config без secrets, digests,
  logs/status и A-adapter control → B quality report. B проходит gate §8 spec:
  Recall@5/supporting precision не ниже A, reviewed numeric 100%, guards без обхода.
- [ ] Коммит `deploy: add reproducible CPU Hybrid stack`; HYBRID принят только после
  живых quality/resource проверок, не после одного валидного YAML.

**Откат:** предыдущий pinned image/config/bundle, volumes сохранить. Если B хуже A,
CLOUD остаётся доступным, HYBRID не объявляется готовым по качеству.

### Задача 11. Backup, restore, мониторинг и эксплуатационная приёмка

**Вход:** принятый HYBRID; задан закрытый off-host backup destination.
**Файлы:** создать `scripts/backup.sh`, `scripts/restore.sh`, `scripts/benchmark_rag.py`,
`tests/test_operations.py`, `docs/deployment.md`; изменить `rag/resources.py`,
`rag/health.py`, workflow и README.
**Интерфейсы:** `backup.sh ROOT DEST`, `restore.sh ARCHIVE NEW_ROOT`;
`python -m scripts.benchmark_rag --output PATH` (новый JSON, не overwrite).

- [ ] Tests: backup holds maintenance lock; restore rejects existing root, absolute/traversal
  archive members and unsafe symlinks; damaged checksum/SQLite blocks activation;
  queue metrics contain no question; missing CPU steal = unavailable. Discover `test_operations.py` — FAIL.
- [ ] Реализовать согласованный backup corpus/registry/versions/pointers/SQLite/config
  без secrets, immutable manifest hashes, закрытые права, документировать защищённый
  перенос off-host. Restore только новый root→verify→SQLite integrity_check→retrieval→activation.
- [ ] Реализовать RSS/cgroup peak, CPU throttling/steal, queue/stage p50/p95, disk/free,
  index bytes, actual provider usage. Log rotation, reserve disk 20%, signal on errors,
  разные liveness/readiness. Retention 30 дней для evaluator reports по явному
  operator job, без автоматического удаления indexes/backups.
- [ ] Повторить tests — PASS; `bash -n scripts/bootstrap-vps.sh scripts/backup.sh scripts/restore.sh`;
  выполнить backup/restore на чистом root и повторный запуск bootstrap без потери данных.
- [ ] Измерить основной 8 ГБ стенд: cold/warm, ≥30 завершённых запросов на каждую
  нагрузку 1/2/4 клиента/режим с inference=1; отдельно maintenance indexing peak.
  Нет OOM, лимиты фактически соблюдаются; 16 ГБ результаты не заменяют 8 ГБ.
  Регрессия p95 >20% требует явного разбора, отсутствующий SLO не подменять выдуманным.
- [ ] Записать runbook: setup, secrets, policy, tunnel, index build/switch/rollback,
  update/recovery, logs, single Telegram token host. Коммит `ops: verify recovery and VPS resource limits`.

**Откат:** старый image и согласованный backup в отдельном root; исходный root не удалять.

### Задача 12. Опциональный Ollama backend

**Вход:** принят HYBRID; доступен отдельный ресурс/экспериментальный стенд.
**Файлы:** изменить `rag/generation.py`, `tests/test_generation.py`, runbook;
создать `compose.local.yaml`, `deploy/ollama/compose.yaml`, `deploy/ollama/gpu.env.example`.
**Интерфейс:** тот же GenerationProvider; LOCAL использует tokenizer/chat template
по Ollama model digest и не допускает cloud-backed model IDs.

- [ ] Tests timeout/no-fallback, offline missing model, unchanged index on LLM switch,
  private endpoint validation; discover `test_generation.py` — новые FAIL.
- [ ] Реализовать Ollama adapter, pinned image/model digest, explicit context/thinking;
  LOCAL network policy закрывает WAN после preload. CPU experimental compose отдельный;
  GPU compose отдельного хоста с NVIDIA toolkit и persistent model volume, порт закрыт.
- [ ] Повторить tests — PASS; после preload реальный ответ CLI при блокировке WAN;
  остановка Ollama не вызывает Gemini; смена модели даёт 0 embed_documents calls.
  Проверить маршрут контейнер→private GPU endpoint, не путать localhost разных хостов.
- [ ] Коммит `feat: add strict optional Ollama generation`.

**Откат:** выключить опциональный compose; оператор явно выбирает прежний provider.
Нехватка GPU/VRAM = blocked для этого эксперимента, не отказ основного HYBRID.

### Задача 13. BM25 и RRF отдельным экспериментом C

**Вход:** измерены A/B, принят HYBRID. **Файлы:** создать `rag/sparse.py`,
`tests/test_sparse.py`; изменить `rag/index.py`, schema и evaluator.
**Интерфейсы:** `build_sparse(chunks: list, chunks_id: str) -> dict`;
`fuse(dense: list, sparse: list, k: int = 60) -> list`.

- [ ] Tests numeric token preservation, Unicode normalization, duplicate chunk IDs,
  deterministic ties, stale sparse fingerprint rejection; discover `test_sparse.py` — FAIL.
- [ ] Реализовать NFKC/casefold + Unicode tokenizer version, JSON sparse store,
  те же chunk IDs; равные веса RRF k=60, dedup, tie-break chunk ID. Raw L2/BM25 не складывать.
- [ ] Повторить tests — PASS; сравнить C против B при одинаковых prompts/LLM/gold,
  dense toggle не меняет FAISS hashes. Нет улучшения → dense остаётся default.
- [ ] Коммит `feat: evaluate optional lexical retrieval fusion`.

**Откат:** выключить sparse retrieval, без FAISS rebuild.

### Задача 14. Reranking и сравнение локальных LLM

**Вход:** C измерен; для E/F принята задача 12 и доступен ресурс.
**Файлы:** создать `rag/reranking.py`, `tests/test_reranking.py`;
изменить pipeline, bootstrap модели, evaluator/compare_models.
**Интерфейс:** `rerank(question: str, candidates: list, limit: int = 20) -> list`.

- [ ] Tests disabled loads no model, top20 cap, stable metadata/IDs, limit/memory refusal;
  discover `test_reranking.py` — FAIL.
- [ ] Реализовать pinned `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, optional CPU;
  официальный приоритет и альтернативы сохранять после reranking/evidence selection.
  На 8 ГБ включать только после замеров в общем runtime budget, иначе оставить off.
- [ ] Повторить tests — PASS; выполнить D против C; E Qwen3 8B против D,
  F Qwen3 14B против E с одинаковыми retrieval/prompt и quantization class/context policy.
  Tokenizer differences/VRAM/OOM/latency записать; отсутствие ресурса = not_run.
- [ ] Коммит `feat: evaluate optional reranking and local generation quality`.

**Откат:** rerank=false и прежний generation provider, FAISS неизменен; результаты
экспериментов сохраняются. LoRA/SFT не начинать.

### Задача 15. Итоговая проверка и отчёт о готовности

**Вход:** завершены обязательные 0–11; опциональные задачи имеют честный статус.
**Файлы:** актуализировать `docs/deployment.md`, README; локальные evidence reports вне Git.
**Интерфейс:** таблица requirement→commit→команда→отчёт→статус.

- [ ] Провести review финального diff на сохранение поведения, доступ к данным,
  bounded resources, atomicity и provenance. Исправления проверять целевыми regression tests.
- [ ] На финальном image выполнить полный unittest suite, config validation,
  offline real-model smoke, Linux HYBRID live smoke, restart и restore drill.
  Повторять нагрузку/качество при изменениях, влияющих на результаты; иначе использовать
  уже сохранённые результаты с идентичными code/image/model/corpus hashes.
- [ ] Свести все 19 критериев §10 spec; not_run/blocked не превращать в PASS.
  Отдельно указать статус основного HYBRID и optional LOCAL/BM25/reranker/GPU.
- [ ] Коммит `docs: record verified Hybrid deployment and remaining optional checks`.

**Откат:** процедура задачи 11; отчёт всегда сохраняет фактические ограничения.

## Матрица покрытия критериев спецификации

| Критерий §10 | Задачи |
|---|---|
| 1 HYBRID без GPU; 2 local embeddings/FAISS | 3, 8–10 |
| 3 сохранённый Gemini baseline | 0–1, 10 |
| 4 смена LLM без rebuild; 5 совместимость пространств | 2–4, 12 |
| 6 источники; 7 проверенные числа | 1, 6–7, 9–10 |
| 8 API errors/index integrity | 2, 4–5 |
| 9 secrets; 10 restart; 11 restore | 6, 9–11 |
| 12 RAM/latency; 13 документированные tests | 10–11, 15 |
| 14 SHA cache; 15 token budget; 16 один model owner | 5, 7–8 |
| 17 atomic activation/rollback | 4, 8, 11 |
| 18 LOCAL/privacy; 19 Telegram fail-closed | 6, 9, 12 |

## Точка согласования

План проверен на соответствие ревизии 3 и утверждён. Шаги подготовки baseline
отмечены выше; задача 0 целиком не завершена без живого A. Исторические 78 unit
tests от 2026-09-29 не доказывают готовность будущих изменений.

Выполнение начато с baseline gate. Без корпуса/разрешения на
передачу/API доступа baseline остаётся blocked; запрос недостающих ресурсов
делается тогда с конкретным перечнем, без передачи секретов через Git/документы.
