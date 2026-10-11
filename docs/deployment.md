# Развёртывание Local-first RAG

Статус 2026-10-11: на коммите `7a5c883` прошёл полный
[Linux CI](https://github.com/vitalpicnic/rag/actions/runs/38159985671):
сборка image, hash-locked установка с `pip check`, офлайн-тесты в image,
проверка Nginx, Compose startup, доступ к web через loopback, отказ API без
авторизации, restart runtime и backup/restore с проверкой хешей FAISS/SQLite.
[Windows/Ubuntu CI](https://github.com/vitalpicnic/rag/actions/runs/38159985795)
также прошёл. В каждом удалённом suite: 223 теста, 221 PASS и 2 ожидаемых пропуска
из-за банковского PDF, не распространяемого с репозиторием; failures/errors — 0.
Повторный bootstrap на VPS, реальная генерация и нагрузочная приёмка ещё не выполнены.
На машине разработки Docker Engine недоступен, Ubuntu WSL
не запускается из-за отсутствующего VHDX. Валидный Compose не означает принятую систему.

Локальная проверка: **223 unittest tests, 0 failures/errors/skips** (Windows,
Python 3.14), Compose all-profiles config и `bash -n` — PASS. Целевой Python 3.12
зафиксирован для Linux image; локальная версия не подменяет проверку этого image.
Отдельный HTTP-процесс без генерации и полный `scripts.smoke_local` с настоящей
CPU E5, запрещённым `socket.connect` и тестовой LLM — PASS. Реальная генерация
не проверялась; в smoke явно записано `real_generation=false`.

## Минимальный сервер

Ubuntu 24.04, amd64, **4 vCPU, 8 ГБ RAM, SSD от 80 ГБ**, не менее 20% свободного диска.
Это стартовая конфигурация для локального E5/FAISS и отдельной LLM. Большой корпус
потребует измерений и увеличения диска/RAM. Ollama с большой моделью на этот же
8 ГБ сервер не включена; используйте отдельную машину по частной сети.

| Контейнер | RAM | CPU |
|---|---:|---:|
| runtime | 3584 MiB | 2.5 |
| bot, опционально | 384 MiB | 0.25 |
| web, опционально | 768 MiB | 0.5 |
| collector | 128 MiB | 0.25 |
| gateway, Nginx | 64 MiB | 0.1 |
| indexer, вместо runtime | 4608 MiB | 3 |

Остаток предназначен ОС и Docker. Эти лимиты заданы в Compose; достаточность
для реального корпуса пока не доказана. Один worker выполняет inference,
очередь содержит до восьми запросов, ожидание в очереди ограничено 60 секундами.

## Подготовка хоста

Из checkout нужного commit на **целевом сервере**:

```bash
sudo bash scripts/bootstrap-vps.sh /srv/rag
cp deploy/vps.env.example deploy/vps.env
sudo docker compose --env-file deploy/vps.env --profile web config --quiet
sudo docker compose --env-file deploy/vps.env build runtime web
sudo docker compose --env-file deploy/vps.env --profile web up -d
```

Bootstrap проверяет ОС/CPU/RAM/диск, устанавливает зафиксированные версии Docker
из официального apt-репозитория, создаёт каталоги и секреты для UID 10001.
Повторный запуск сохраняет данные и ключи; несовпадающая существующая установка
Docker требует явного решения оператора. Скрипт не удаляет конфликтующие пакеты.
Не запускайте одновременно bootstrap и изменение секретов/каталогов.
Если импортируете корпус от другого пользователя, назначьте UID 10001 права
чтения отдельно; bootstrap не меняет рекурсивно владельца существующих данных.

До запуска можно проверить состояние без API и загрузки моделей:

```bash
sudo python3 -m scripts.check_deployment --root /srv/rag --secrets /srv/rag/secrets
```

Проверяются обязательные каталоги, обычный lock-файл, согласованность и уникальность
секретов адаптеров, закрытые POSIX-права и синтаксис privacy-политики. Команда ничего
не исправляет и не выводит секреты; возвращает ненулевой код при ошибке. PASS не
подтверждает доступ UID контейнера к каждому документу, готовность модели или VPS
по ресурсам (`generation_verified=false`).

По умолчанию `RAG_GENERATION_ENABLED=false`: работают topics, health и проверенные
табличные графики при наличии их данных. `/query` отказывает; readiness=false.
Это режим проверки инфраструктуры без LLM, а не имитация ответов.
Docker healthcheck проверяет liveness; для готовности генерации отдельно выполните:

```bash
sudo docker compose --env-file deploy/vps.env exec runtime python -m scripts.healthcheck --ready
```

Состояние `/srv/rag` и секреты не входят в image. Корневые FS контейнеров read-only,
процессы работают без root/capabilities; Docker socket не монтируется.
Файл `.model-owner.lock` должен быть обычным файлом, созданным bootstrap.
Не удаляйте его во время работы процессов.

## Доступ, секреты и интерфейсы

8080 и 8501 опубликованы только на loopback через отдельный gateway. Runtime и web
остаются только во внутренней сети без прямого выхода в интернет. Gateway связан
с внутренней и входной bridge-сетями, проксирует только фиксированные runtime/web,
не получает volumes корпуса или secrets. Его образ закреплён digest; URI с
download/export токенами не журналируются. При выключенном профиле web порт 8501
возвращает ошибку upstream, не открывает другой сервис. Для веб-интерфейса:

```bash
ssh -N -L 8501:127.0.0.1:8501 operator@vps
```

Откройте `http://127.0.0.1:8501`. Отдельная пользовательская авторизация web
не реализована: SSH/VPN является границей доступа. Не публикуйте порт напрямую.
CLI использует `RAG_RUNTIME_URL` и `RAG_SERVICE_SECRET_FILE` (секрет cli.txt).
API и доверенная идентификация адаптеров описаны в [runtime-api.md](runtime-api.md).

Секреты лежат в `/srv/rag/secrets`, файлы 0600, каталог 0700. Compose монтирует
их как файлы. Не передавайте значения через аргументы команд, Git или отчёты.
Для Telegram создайте `telegram.txt` с токеном бота, владельцем 10001 и правами 0600;
заполните `TELEGRAM_ALLOWED_USER_IDS` в `deploy/vps.env`, затем включите профиль
`--profile telegram`. Пустой/неправильный allowlist запрещает доступ.
Один bot token должен опрашиваться только на одном хосте. Межпроцессный lock
защищает одну state-директорию, не несколько серверов. Техническая учётная запись
Telegram для платных подписок не реализуется.

## Модели, индексы и включение генерации

Подготовка Ollama вынесена в [ollama-setup.md](ollama-setup.md). Production требует проверенного
счётчика токенов, соответствующего конкретной модели и её chat template.
`RAG_COUNTER_FACTORY=module:factory` — точка подключения доверенного серверного
кода; готового production backend для неизвестной Ollama-модели нет.
Не обходите этот отказ приближённым подсчётом или development-режимом.

Compose по умолчанию изолирует runtime от внешней сети. Для удалённой Ollama
или Gemini потребуется явный override сети, адреса и настроек провайдера,
проверка приватного маршрута и счётчика. Gemini-адаптер сохранён, но живое
подключение не проверено. Разрешения на внешнюю передачу задаются отдельно
в `policies/external.json`; начальная политика запрещает передачу.

Загрузка E5 — отдельная сетевая операция с закреплённой revision:

```bash
sudo docker compose --env-file deploy/vps.env --profile maintenance run --rm model-bootstrap
sudo docker compose --env-file deploy/vps.env stop runtime bot web
sudo docker compose --env-file deploy/vps.env --profile maintenance run --rm indexer \
  python -m scripts.index_admin build --root /srv/rag --topic bank --maintenance --output /srv/rag/bundle-bank.json
sudo docker compose --env-file deploy/vps.env --profile maintenance run --rm indexer \
  python -m scripts.index_admin activate --root /srv/rag --topic bank --maintenance --bundle /srv/rag/bundle-bank.json
sudo docker compose --env-file deploy/vps.env --profile web up -d
```

Перед сборкой поместите PDF/TXT в `/srv/rag/data/bank`. Сборка и activation разделены;
`verify` проверяет bundle, `rollback --bundle <старый JSON> --maintenance` возвращает
старую версию. FAISS не пересобирается при смене LLM. Подробнее: [index-v2.md](index-v2.md).

После загрузки модели проверьте весь локальный путь с тестовой LLM и отдельным
временным корпусом; этот smoke не использует реальные документы и не доказывает
качество генерации:

```bash
sudo docker run --rm --network none --read-only --tmpfs /tmp:size=512m,mode=1777 \
  --mount type=bind,src=/srv/rag/model_cache/e5,dst=/model,readonly \
  -e RAG_METRICS_PATH=/tmp/smoke-metrics.jsonl \
  rag-runtime:local python -m scripts.smoke_local --cache /model
```

Для исходного проверенного банковского PDF таблицы собираются отдельно, без LLM:
`docker compose --env-file deploy/vps.env --profile maintenance run --rm indexer
python build_tables.py --root /srv/rag`. Неизвестный исходный файл отклоняется по SHA-256.

## Резервное копирование и восстановление

Остановите runtime и все записи в корпус/SQLite/policies. Backup дополнительно
берёт model-owner lock. Используйте Python окружение с runtime dependencies:

```bash
bash scripts/backup.sh /srv/rag /private-backups/rag-20261010.tar.gz
bash scripts/restore.sh /private-backups/rag-20261010.tar.gz /srv/rag-restored
```

В контейнере те же команды: `python -m scripts.backup ROOT ARCHIVE` и
`python -m scripts.restore ARCHIVE NEW_ROOT`; смонтируйте приватный каталог архивов
RW отдельно. Архив содержит data, faiss_indexes (версии/registry/pointers),
prepared_tables и policies, manifest с SHA-256. Секреты, модели, `.env`, логи,
кэш prepared_corpora исключены; настройки развёртывания восстановите из commit
и отдельно сохранённой операторской конфигурации. Архив содержит частные документы:
передавайте его в зашифрованное off-host хранилище по SSH/TLS, доступ только оператору.
Автоматическая внешняя отправка не настроена.

Restore принимает только **новый** root, отклоняет ссылки, traversal, дубликаты,
неверные размеры/хеши; проверяет SQLite и поиск в активном FAISS без модели.
После проверки staging переименовывается в новый root. Существующее развёртывание
не переключается автоматически. Восстановите отдельно secrets/model_cache,
назначьте права UID 10001, подготовьте lock/служебные каталоги bootstrap с новым root,
укажите новый `RAG_STATE_ROOT`/`RAG_SECRETS_ROOT`, запустите и проверьте readiness.
Старый root сохраняйте для отката. Офлайн-тест проверяет настоящий FAISS и SQLite;
полный recovery drill на Linux с реальной моделью ещё требуется.

## Наблюдаемость и приёмка

Collector сохраняет очередь, readiness, RSS/peak, cgroup memory/CPU, CPU steal и
диск без текста вопросов/ответов; отсутствующие показатели имеют null.
`logs/operations.jsonl` и `logs/metrics.jsonl` ограничены тремя файлами по 10 MiB
каждый. Метрики стадий и actual provider usage записывает runtime; collector
считает p50/p95 по последним максимум 1000 событиям каждой стадии в текущем файле.
Суммы usage относятся только к наблюдаемым полям, рядом указано количество
записей с доступными значениями; отсутствие usage не заменяется нулями.
Отказ collector виден через status/logs контейнера. Контролируйте резерв диска 20%.

После подключения LLM выполните `python -m scripts.benchmark_rag --topic bank
--question 'Контрольный вопрос' --output /private-reports/load.json` в настроенном
CLI окружении. Команда делает по 30 попыток для 1/2/4 клиентов, выводит количество
ошибок и p50/p95 только успешных вызовов. Это не оценка качества. Если успешных
меньше 30, нагрузочная приёмка не пройдена. Повторите для всех режимов, cold/warm;
замерьте отдельно индексатор и сравните качество по gold-набору. Реальные замеры
в текущем окружении не проводились. Храните evaluator reports 30 дней; удаление
старых отчётов выполняется отдельно оператором, не затрагивает indexes/backups.

Перед production обязательны: Linux build и `pip check`, офлайн тесты в image,
реальные E5/retrieval, проверенный tokenizer и LLM, restart/restore, проверка RAM,
latency и качества на выбранном VPS. Сборка, офлайн-тесты и utility restart/restore
подтверждены указанным выше CI; приёмка с настоящей моделью на целевом VPS остаётся открытой.

CI дополнительно проверяет Compose runtime/web/collector/gateway, повторный запуск runtime
с неизменными хешами файлов, отказ генерации в utility-режиме, backup и restore
в новый root. `scripts.deployment_fixture` предназначен только для этой проверки:
создаёт новый каталог с синтетическими векторами и настоящими FAISS/SQLite,
отказывается использовать существующий root. Он не проверяет качество embeddings
или генерации. `.ci-run` с временными секретами не публикуется как artifact.

## Воспроизводимость

Python 3.12 amd64 image закреплён digest; apt использует Debian snapshot.
Linux lockfiles содержат транзитивные версии и hashes, CPU torch. Они разрешены
для Python 3.12/x86_64-manylinux_2_28 с uv 0.11.7; установка на Linux и `pip check`
прошли при сборке image в GitHub Actions.
Для обновления пересоздайте lock из `.in` через `uv pip compile --python-version 3.12
--python-platform x86_64-manylinux_2_28 --generate-hashes`, runtime использует
CPU extra-index из `.in` и `--index-strategy unsafe-best-match` только при resolution;
установка требует hashes и binary wheels. Изменения pins проходят review и CI.

Основание конфигураций: [Docker Ubuntu installation](https://docs.docker.com/engine/install/ubuntu/),
[Compose services](https://docs.docker.com/reference/compose-file/services/),
[Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/),
[pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/),
[uv resolution](https://docs.astral.sh/uv/concepts/resolution/).
Конфигурация входа: [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/)
и [Nginx WebSocket proxying](https://nginx.org/en/docs/http/websocket.html).
