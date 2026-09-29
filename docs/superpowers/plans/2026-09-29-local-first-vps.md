# Local-first Hybrid RAG — план реализации

Статус: подготовлен для согласования вместе со спецификацией; выполнение
production-задач не разрешено до согласования пользователя.

**Цель:** модернизировать существующий `vitalpicnic/rag` для HYBRID на CPU VPS,
сохранив CLOUD, добавив изолированный LOCAL и измеряемое улучшение retrieval.

**Архитектура:** независимые провайдеры generation/embedding, общий оркестратор,
версионированные пространства FAISS, существующие интерфейсы. Один основной
VPS, опциональная Ollama CPU/GPU, явные профили и отсутствие скрытого fallback.

**Стек:** Python 3.12, Ubuntu 24.04, Docker Compose, FAISS, Gemini,
OpenAI-compatible chat API, Sentence Transformers/PyTorch CPU, Ollama, SQLite.

**Спецификация:** [архитектура, аудит, риски и приёмка](../specs/2026-09-29-local-first-vps-design.md).
Исполнение после согласования — последовательно в текущем процессе, без
субагентов; применять executing-plans, TDD и verification-before-completion.
Текст внешних навыков в репозиторий не копируется.

## Общие ограничения

- Основной VPS: 4 vCPU / 8 ГБ, без GPU; ресурсная калибровка отдельно на 16 ГБ.
- Не изменять исходные пользовательские документы, production SQLite и индексы.
  Миграции и эксперименты выполняются в новом root с контролируемой копией корпуса.
- Сохранить все четыре режима, Telegram/CLI/Streamlit, registry, PPTX, таблицы,
  FAISS checksums, provenance, совместимость Gemini и Windows offline-тесты.
- Новая модель embeddings → новый namespace; новая LLM → тот же индекс.
- Никаких cloud fallback; LOCAL не создаёт внешних LLM/embedding клиентов.
- API/model/GPU readiness нельзя заменить результатом mock-теста.
- Каждый этап — тесты, документация, отдельный commit и описанный rollback.
  Коммиты не отправляются в origin автоматически. Не коммитить reports с ответами,
  secrets, corpus, модели или индексы.
- Все названные ниже новые файлы/команды — будущие deliverables, не утверждение,
  что они уже существуют. Этап 0 меняет только архитектурные документы.

## Проверки, требующие особого внимания

1. LOCAL/HYBRID без GOOGLE_API_KEY; некорректная комбинация профиля и провайдера —
   явная ошибка до сети, в том числе если чужие ключи случайно присутствуют.
2. Смена embedding с той же размерностью; повреждённый mapping/manifest и
   прерывание публикации — прежний current.json остаётся рабочим.
3. Пустой Telegram allowlist, второй polling-процесс и запрос чужого source/PPTX —
   отказ; перезапуск освобождает flock, не раскрывает документы.
4. LOCAL с недоступной Ollama, cloud model tag или незагруженной моделью — отказ
   без доступа к внешнему API; offline тест реальной модели обязателен.
5. Backup во время изменения корпуса, несовместимый restore и нехватка диска —
   отказ до переключения; ни один existing volume не удаляется.

## Этап 0 — ревью и сохранение исходного состояния

**Файлы:** актуализировать существующую спецификацию; создать этот план.
Локальные результаты: `reports/stage0-baseline-2026-09-29.{json,log}`.

- [x] Прочитать исходную спецификацию, код, историю Git, tests и evaluator.
- [x] Выполнить baseline с точным учётом passed/failed/errors/skipped:
  78/0/0/0, оба PDF-зависимых теста выполнены, exit 0.
- [x] Проверить 30 evaluation cases и 43 фрагмента; 0 failures. Зафиксировать
  SHA корпуса/набора, отсутствие индекса gold topic и status A=not_run.
- [x] Актуализировать архитектуру, профили, риски P0/P1/P2 и приёмку 0–6.
- [x] Проверить ссылки/согласованность документов.
- [x] Создать docs-only commit
  `docs: review local-first architecture and stage plan`.
- [ ] Получить согласование спецификации и плана до изменений production-кода.

**Рабочий результат:** конкретный проект миграции и подтверждённый offline
baseline. Это не утверждение production readiness.
**Откат:** revert docs-коммита; данные и приложение не затрагиваются.

## Этап 1 — независимые Generation/Embedding Provider

**Создать:** `rag/config.py`, `rag/generation.py`, `rag/embeddings.py`,
`tests/test_config.py`, `tests/test_generation.py`, `tests/test_embeddings.py`.
**Изменить:** `rag/engine.py`, `build_index.py`, `rag/pipeline.py`,
`rag/models.py`, `rag/metrics.py`, `.env.example`, requirements, README.

**Интерфейсы:**

- `load_settings(root: Path, environ: Mapping[str, str] | None = None) -> Settings`.
  Frozen Settings содержит profile, environment, data_root, embedding_provider,
  embedding_model/revision, llm_provider/model/base_url, secret references,
  resource limits. Секреты исключены из repr/metrics.
- `build_embedding_provider(settings: Settings) -> EmbeddingRuntime`:
  `.client` совместим с LangChain Embeddings; `.index_settings: dict` содержит
  только параметры корпуса/кодирования; `.model_id: str` — для telemetry.
- `build_generation_provider(settings: Settings) -> GenerationRuntime`:
  `.runnable` совместим с Runnable.invoke/bind; `.provider`, `.model_id`;
  `.for_mode(mode: str)` преобразует output limits в параметры нужного API.
- `create_embeddings(root=ROOT)` остаётся совместимой оболочкой над новой фабрикой.

- [ ] До смены поведения сохранить baseline A: code/options/gold hashes и
  доступные legacy manifests; live A выполнять на snapshot исходного кода в
  отдельном root. Нет ключа/индекса — status blocked и запрет сравнений B–F,
  но unit-разработка фабрик может продолжаться без ложного результата A.
- [ ] Написать тесты `test_cloud_preserves_legacy_settings`,
  `test_hybrid_compatible_needs_no_google_key`, `test_local_blocks_cloud_before_init`,
  `test_secret_file_conflict_and_redaction`. Mock Google constructor должен иметь
  `assert_not_called()` для LOCAL и HYBRID/openai_compatible.
- [ ] Запустить `python -m unittest discover -s tests -p 'test_config.py' -v`;
  наблюдать отказ новых тестов из-за отсутствующей реализации.
- [ ] Реализовать строгую матрицу HYBRID/local+external, LOCAL/local+ollama,
  CLOUD/google+gemini, отдельный RAG_ENV, env/*_FILE и immutable Settings.
- [ ] Написать adapter contract tests: Gemini параметры сохранены; compatible
  API получает только поддерживаемые chat/messages/model/max output настройки;
  нет Gemini thinking fields, у usage отсутствующее поле остаётся None.
- [ ] Реализовать фабрики с ленивыми импортами, ограниченным retry/timeout.
  Ollama transport подключается в этапе 4; неизвестный/ещё не доступный адаптер
  явно отказывается, а не использует Gemini. Локальный encoder — этап 2.
- [ ] Подключить CLOUD и HYBRID/compatible wiring; передавать реальный embedding
  model_id в metrics, различать local/API стоимость. Добавить тест переключения
  LLM: `embed_documents` не вызывается, index settings равны до/после.
- [ ] Прогнать новые тесты и весь offline suite; описать конфигурацию и
  временно недоступные режимы. Commit `refactor: separate generation and embedding providers`.

**Приёмка:** CLOUD работает по прежнему контракту; новые generation adapters
контрактно проверены; нет обязательного Google-ключа у неподходящего провайдера.
**Откат:** revert коммита/старый image; формат данных на этом этапе не меняется.

## Этап 2 — локальные embeddings, пространства FAISS и миграция

**Создать:** `rag/documents.py`, `scripts/download_models.py`,
`scripts/manage_index.py`, `scripts/smoke_local.py`, `tests/test_index_migration.py`.
**Изменить:** `rag/embeddings.py`, `rag/index.py`, `build_index.py`,
`tests/test_index.py`, `tests/test_index_config.py`, dependency locks, README.

**Интерфейсы:**

- `embedding_fingerprint(index_settings: dict) -> str`: provider/model/revision,
  dimension, normalization, query/document prefix, tokenizer/max length и версии
  encoder входят; generation provider/model/role не входят.
- `extract_chunks(root, snapshot)` сохраняет сигнатуру и provenance; реализация
  выносится в documents, старый импорт остаётся доступен.
- `list_versions(root: Path, topic: str, settings: dict) -> list[dict]` и
  `activate_version(root, topic, version_id, settings) -> dict`: verify до replace.
- CLI `manage_index.py list|verify|activate|migrate-legacy` с обязательными topic,
  root и профилем; `migrate-legacy` требует отдельный destination root.

- [ ] RED: тесты same-dimension wrong-model, prefix/revision/normalization mismatch,
  отсутствующей локальной модели, сохранения legacy fingerprint и chunk IDs.
- [ ] Реализовать `multilingual-e5-small` на CPU, revision pin, normalized vectors,
  query/passsage prefixes, bounded batch/threads, local_files_only runtime,
  singleton на конфигурацию процесса. Download только отдельной командой.
- [ ] GREEN: без GOOGLE_API_KEY и с запретом sockets mock tests проверяют пути;
  реальный model smoke после preload выполняется отдельно, не подменяет mock.
- [ ] RED: schema v2 mapping mismatch, path traversal, corrupted artifacts,
  build interruption и ошибка embedding API оставляют current bytes прежними.
- [ ] Реализовать namespaces по fingerprint и schema v2 JSONL + mapping;
  не менять legacy current.json. Legacy loader изолирован и не принимает
  пользовательские загруженные pickle; hashes и права проверяются до чтения.
- [ ] RED/GREEN: миграция trusted fixture v1 в новый root сохраняет ID, векторы,
  retrieval и hashes исходника; переключение/откат не вызывает embedding.
- [ ] Реальный offline smoke: загрузить модель заранее, затем в изолированной
  среде запретить сеть, build небольшого проверенного RU корпуса, найти gold page.
  Для полноценного Docker `--network none` повторить в этапе 3.
- [ ] Проверить `python -m unittest discover -s tests -p 'test_index*.py' -v`,
  embedding/document tests и общий suite; документировать offline bootstrap,
  совместимость/rollback. Commit `feat: add local embeddings and versioned index spaces`.

**Приёмка:** настоящий local build/retrieval, isolation между пространствами,
legacy Gemini нетронут, corrupt/partial версии не публикуются.
**Откат:** выбрать прежний CLOUD/root/image; новые namespaces оставить на диске.

## Этап 3 — рабочий HYBRID на CPU VPS

**Создать:** Dockerfile, .dockerignore, compose.yaml, requirements/locks,
`deploy/hybrid.env.example`, `scripts/bootstrap-vps.sh`, `rag/health.py`,
`rag/polling_lock.py`, `tests/test_deployment.py`, `tests/test_numeric_evidence.py`.
**Изменить:** bot.py, app.py, main.py, build_tables.py, rag/tables.py,
rag/evidence.py, rag/slides.py, .github/workflows/tests.yml, README.

**Интерфейсы:**

- `check_readiness(settings: Settings, topic: str) -> dict`: ready/status/reasons,
  без paid calls; CLI возвращает ненулевой код при not_ready.
- `polling_lock(path: Path)` — контекстный Linux flock; один shared volume.
- Проверенные таблицы остаются authoritative через `load_rows`; новый
  `validate_numeric_claims(claims: list[dict], verified_rows: list) -> list[dict]`
  возвращает нарушения сопоставимых entity/metric/period/unit/value. Свободный
  текст не становится verified из-за присутствия такого же числа в документе.

- [ ] RED: production empty allowlist, second polling, чужие source/PPTX,
  read-only volume, отсутствующий secret/model/index, повреждённая SQLite.
- [ ] Реализовать root mapping во всех интерфейсах, fail-closed authorization,
  lock, strict readiness и heartbeat event loop. Проверить старые сценарии
  CLI/Streamlit/TG и четыре роли тестовыми адаптерами без сети.
- [ ] RED/GREEN: значение из reviewed SQLite не заменяется conflicting generated
  number; такой ответ отклоняется/исправляется из проверенной строки. Tests для
  одинакового числа на другом периоде, единиц/масштаба и отрицательных значений.
  PPTX должен сохранять тот же подтверждённый ответ, без новой генерации.
- [ ] Создать CPU Dockerfile с Python 3.12 и non-root; зафиксировать wheels/hash
  locks, image digest и model revision после проверки доступности. В образе нет
  .env/data/indexes/.git/.runtime_deps, CUDA wheels и Docker socket.
- [ ] Создать Compose services app/tools, bot, web, indexer, model-init:
  persistent mounts, readonly runtime, secrets, limits, logging, health/restart.
  Bot один, web только 127.0.0.1, indexer одноразовый restart=no.
- [ ] Bootstrap Ubuntu повторяем: проверить ОС/CPU/RAM/disk, official Docker apt,
  UID/каталоги/права; существующие данные и secrets не перезаписывать.
- [ ] `docker compose --env-file deploy/hybrid.env.example config --quiet`;
  Docker build; container unit tests; offline `smoke_local.py` с network=none.
  Проверить non-root/cgroups и restart неизменность FAISS/SQLite.
- [ ] На выбранном VPS: bootstrap, model-init, копия corpus, отдельный build,
  HYBRID smoke выбранного API, query→sources→PPTX, сервисный restart.
  До наличия VPS/API этот подпункт not_run, этап целиком не принимается.
- [ ] Обновить deployment runbook, записать image ID/config hash и результаты.
  Commit `deploy: run hybrid rag on a cpu vps`.

**Приёмка:** реальный HYBRID на 4 vCPU/8 ГБ, документы закрыты, storage persistent,
все прежние функции проверены, нет обхода numeric/source guards.
**Откат:** прежний image digest и профиль; не использовать `down -v`.

## Этап 4 — LOCAL и Ollama CPU/GPU

**Создать:** compose.local.yaml, deploy/ollama/compose.yaml,
deploy/ollama/gpu.env.example, tests/test_ollama.py, tests/test_local_isolation.py.
**Изменить:** rag/generation.py, rag/config.py, scripts/download_models.py,
health, README/deployment runbook.

**Контракт:** тот же GenerationRuntime; retrieval и embedding settings неизменны.
LOCAL accepts только ollama с allowlisted endpoint и local model digest.

- [ ] RED: Google/OpenAI external clients никогда не создаются в LOCAL даже при
  заданных чужих ключах; cloud model tag, URL вне allowlist, redirects и timeout
  не вызывают внешний fallback. Проверить requests mock и сетевую изоляцию.
- [ ] Реализовать Ollama transport/usage/output limits и health без generate;
  запрет cloud функций и управление только предварительно загруженными моделями.
- [ ] Создать однохостовый LOCAL Compose с internal network, без публикации
  11434; отдельный GPU Compose с NVIDIA capabilities и private endpoint.
  Документировать container/host адреса и отсутствие multi-host Compose network.
- [ ] Preload Qwen3, затем блокировать WAN и выполнить настоящий CLI ответ со
  ссылками. Не использовать Telegram для доказательства полного offline.
- [ ] Проверить смену Gemini→Ollama: FAISS hashes и embedding count неизменны.
  CPU и GPU результаты раздельно; недоступная GPU = not_run, не PASS.
- [ ] Unit/contracts + LOCAL smoke; описать ограничения RAM/CPU и восстановление.
  Commit `feat: support isolated local generation with ollama`.

**Приёмка:** полный LOCAL без внешнего API после preload; отсутствие ключей
Google не мешает запуску. GPU deploy конфигурация проверена отдельно.
**Откат:** явный выбор HYBRID/CLOUD и предыдущего image, без миграции индекса.

## Этап 5 — hybrid retrieval, reranking и качество

**Создать:** rag/retrieval.py, rag/reranking.py,
tests/test_retrieval.py, tests/test_reranking.py, scripts/run_experiments.py.
**Изменить:** rag/index.py, rag/evaluation.py, scripts/evaluate_rag.py,
rag/metrics.py, gold fixtures только для проверенных примеров.

**Интерфейсы:**

- `HybridRetriever.invoke(query: str, config=None) -> list[Document]`; совместим
  с прежним IndexRetriever.invoke, параметры из Settings.
- `fuse_rankings(dense: list[Document], sparse: list[Document], k=60) -> list[Document]`.
- `rerank(query: str, docs: list[Document], limit=20) -> list[Document]`;
  сохраняет metadata/IDs, не создаёт новых фактов.
- `run_cases(..., mode: str | None = None)` расширяет существующий контракт;
  mode=None сохраняет 28 text + 2 overview. Отдельные поля candidate_recall@k,
  context_recall, numeric_review, entailment_review, stage latencies/statuses.

- [ ] RED: BM25 и FAISS с разным corpus fingerprint не объединяются; одинаковые
  chunk IDs дедуплицируются, RRF детерминирован, raw L2/BM25 не суммируются.
- [ ] Реализовать BM25 sidecar над теми же chunks, Unicode/number tokenizer,
  JSON артефакты и corpus/processing fingerprint; RRF k=60, rerank top20.
- [ ] RED/GREEN: reranker меняет только порядок/score, offline cache обязателен,
  отключение не перестраивает индекс; source alternatives/evidence budgets сохранены.
- [ ] Расширить evaluator четырьмя режимами и фиксированными k=3/5/10;
  unreviewed/unknown/not_applicable/failed не считать нулём или успехом.
  Проверять numeric tuples Decimal и ручную поддержку атомарных утверждений.
- [ ] Заморозить prompts/chunks/options и провести A→B→C→D→E→F из раздела 8
  спецификации, изменяя только указанный компонент. E/F — Qwen3 8B/14B с теми
  же retrieval results, context/quantization policy; model digests фиксируются.
- [ ] Перед B проверить control A на новых адаптерах: prompt/options/chunk order
  совпадают с исходным A; numeric guards применены одинаково post hoc ко всем
  raw answers. При различиях не приписывать эффект одной embedding-модели.
- [ ] Без измеренного A не публиковать относительные улучшения. Corpus/API/model
  unavailable отражать blocked и продолжать только независимые offline проверки.
- [ ] Документировать качество/latency/memory/cost по ролям и решение о default.
  Commit `feat: add evaluated hybrid retrieval and reranking`.

**Приёмка:** воспроизводимые отчёты с честными статусами, исходный A сохранён;
retrieval default меняется только после прохождения quality/resource gates.
**Откат:** dense + reranking disabled, прежний image; FAISS не перестраивать.

## Этап 6 — production hardening, измерения и recovery

**Создать:** scripts/benchmark_rag.py, scripts/backup.sh, scripts/restore.sh,
tests/test_backup_restore.py, tests/test_resource_metrics.py, deployment runbook.
**Изменить:** rag/metrics.py, rag/health.py, Compose limits/logging, CI workflow.

**Интерфейсы:** benchmark CLI принимает profile/experiment/mode/concurrency,
число запросов, output path; пишет metadata + samples + summary + статус.
Backup CLI принимает explicit root/destination; restore требует новый пустой
destination и проверяет hashes до активации; secrets сохраняются отдельно.

- [ ] RED: metrics без /proc или cgroup возвращают unavailable; rerank disabled
  возвращает not_applicable; неизвестный provider pricing возвращает unknown.
- [ ] Реализовать RSS/model delta/index bytes/index-build peak, CPU utilization,
  steal/throttling, queue/query-embedding/dense/sparse/rerank/generation/total timing.
  Не ставить Docker socket в контейнер для измерений; host collector отдельно.
- [ ] На 16 ГБ выполнить cold/warm и 1/2/4 клиента, минимум 30 запросов на режим;
  отдельно повторить HYBRID на 8 ГБ. Отличать очередь SerialWorker от реальных
  parallel inference; при OOM прекратить рост нагрузки, записать failed samples.
- [ ] RED: interrupted backup, изменившийся corpus, symlink/path traversal в
  archive, недостаточный диск и wrong-profile restore не меняют active root.
- [ ] Реализовать согласованный backup в maintenance window, контрольные суммы,
  защиту архива/метаданных и restore в новый root. Проверить SQLite integrity,
  FAISS/source hashes, retrieval gold, PPTX; source secrets не попадают в Git/log.
- [ ] Выполнить restart/restore drill в контейнере и на целевом VPS; экспортировать
  отчёт с versions, timings, status и recovery steps. Ошибки healthchecks,
  заполнения диска и лимитов проходят fault injection.
- [ ] Прогнать полный suite + Linux container smoke + доступные API/GPU проверки;
  сверить 13 критериев спецификации с конкретными reports. Тесты внешней среды
  без ресурсов остаются not_run, финальная production-ready отметка запрещена.
- [ ] Commit `ops: verify resource limits backup and recovery`.

**Приёмка:** restore реально выполнен, измерены RAM и latency, соблюдены лимиты
и доступ, закрыты P0/P1 production blockers. Нет обучения моделей на VPS.
**Откат:** предыдущий image/config + проверенный согласованный snapshot в новом
root; исходный volume не удаляется до отдельного решения владельца.

## Согласование и отчётность

Одобрению подлежат эта последовательность, матрица профилей, границы миграции,
условия LOCAL isolation и критерии качества. Вопросы о CPU/RAM/ОС уже закрыты
текущим ТЗ; их не задавать повторно. Параметры доступа к реальному VPS и API
потребуются только для живой приёмки соответствующего этапа и не собираются
через репозиторий или публичные сообщения.

После каждого этапа отчёт содержит commit, что работает, команды проверок,
passed/failed/skipped/not_run, материальные ограничения и точный rollback.
Наличие новых файлов или успешный mock-тест не заменяет приёмку рабочего результата.
