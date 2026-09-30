# Развёртывание локальной Ollama для RAG

Дата: 2026-10-01. Для запуска Ollama ключ Gemini не нужен. Инструкция подготавливает
локальный backend генерации; это ещё не готовый production RAG.

В ветке `feat/local-first-vps` доступны настройки, Ollama chat adapter и отдельный
smoke test. Основные `main.py`, Telegram и Streamlit пока используют прежний Gemini
pipeline. Не запускайте их с ожиданием полностью локального поиска: CPU embeddings,
новый индекс и общий runtime ещё предстоит подключить. Production Ollama adapter
пока намеренно отклоняет запуск до интеграции точного tokenizer/context budget.

Пользователь разрешил продолжить разработку без Gemini baseline. Он остаётся
`not_run`; результаты Ollama не доказывают превосходство над Gemini.

## Выбор машины

Для первого теста подойдёт существующий Windows-компьютер либо отдельная Ubuntu
24.04 x86_64 машина. Планировочный ориентир: 4 CPU, 8–16 ГБ RAM и минимум 15 ГБ
свободного диска; GPU необязателен. Это оценка, а не измеренная гарантия скорости.
На CPU ответы могут быть медленными. Рекомендуется отдельная машина: основной
RAG VPS 4 vCPU / 8 ГБ уже имеет собственный бюджет embeddings/retrieval.

Начальная модель — `qwen3:4b`: примерно 2,5 ГБ загружаемых весов; рабочая память
дополнительно расходуется на контекст и runtime. Для проверки на слабой машине
можно явно выбрать `qwen3:0.6b`; качество банковской аналитики этой проверкой не
подтверждается. Для первых запросов используйте один параллельный запрос,
контекст 8192 и отключённый thinking. [Официальные модели Qwen3](https://ollama.com/library/qwen3/tags).

## Вариант A: Windows, быстрее для текущего компьютера

1. Установите Ollama из [официального дистрибутива Windows](https://ollama.com/download/windows).
   Запишите версию установщика. Для воспроизводимого повторения сохраните его
   вместе с SHA256: `Get-FileHash .\OllamaSetup.exe -Algorithm SHA256`.
2. Полностью закройте Ollama через значок в системном трее. Откройте **новый** PowerShell
   после установки, чтобы обновился PATH. Выполните:

```powershell
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_NO_CLOUD = '1'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_MAX_LOADED_MODELS = '1'
$env:OLLAMA_MAX_QUEUE = '8'
$env:OLLAMA_CONTEXT_LENGTH = '8192'
ollama --version
ollama serve
```

Оставьте это окно открытым. Если порт занят, завершите уже запущенное приложение
Ollama; не запускайте два сервера. Параметры выше действуют для этого процесса.
Для автозапуска задайте те же переменные в настройках пользователя Windows и
перезапустите приложение. Не задавайте `OLLAMA_HOST=0.0.0.0` для доступа из интернета.

В другом PowerShell:

```powershell
ollama pull qwen3:4b
ollama list
Invoke-RestMethod http://127.0.0.1:11434/api/version
$body = @{
  model = 'qwen3:4b'
  messages = @(@{role = 'user'; content = 'Сколько будет 2 + 2? Ответь числом.'})
  stream = $false
  think = $false
  options = @{num_ctx = 8192; num_predict = 32; temperature = 0}
} | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:11434/api/chat -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod http://127.0.0.1:11434/api/tags | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 ollama-models.json
```

Ожидается `done=true`, непустой `message.content`, ответ по смыслу «4».
Сохраните `ollama-models.json`: в нём полные model digests. Tag может обновиться;
не выполняйте повторный pull между сравниваемыми измерениями.
После загрузки отключите интернет и повторите запрос: локальная модель должна
ответить. `OLLAMA_NO_CLOUD=1` отключает cloud-функции, но не является сетевым firewall.
См. [официальную установку Windows](https://docs.ollama.com/windows)
и [настройки cloud/сети](https://docs.ollama.com/faq).

## Вариант B: отдельный Linux-сервер, Docker Compose

Установите Docker Engine и Compose plugin по
[инструкции для Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
Проверьте `docker version` и `docker compose version`: должны отвечать client и server.
Следующие команды выполняйте из корня checkout с этой инструкцией.

Сначала загрузите конкретный релиз, например `0.34.1` из
[официальных релизов Ollama](https://github.com/ollama/ollama/releases/tag/v0.34.1),
затем используйте его **реальный digest**. Пример не требует `latest`:

```bash
docker pull ollama/ollama:0.34.1
export OLLAMA_IMAGE="$(docker image inspect ollama/ollama:0.34.1 --format '{{index .RepoDigests 0}}')"
printf 'OLLAMA_IMAGE=%s\n' "$OLLAMA_IMAGE" > deploy/ollama/.env
docker compose --env-file deploy/ollama/.env -f deploy/ollama/compose.yaml -f deploy/ollama/compose.bootstrap.yaml config --quiet
docker compose --env-file deploy/ollama/.env -f deploy/ollama/compose.yaml -f deploy/ollama/compose.bootstrap.yaml up -d
docker compose --env-file deploy/ollama/.env -f deploy/ollama/compose.yaml exec ollama ollama pull qwen3:4b
docker compose --env-file deploy/ollama/.env -f deploy/ollama/compose.yaml exec ollama ollama list
```

Файл `.env` локальный и игнорируется Git. Сохраните его для повторного развёртывания.
Временный bootstrap override разрешает интернет только для загрузки модели.
После загрузки удалите контейнер/сеть **без удаления volume** и запустите основной
файл с внутренней сетью:

```bash
docker compose --env-file deploy/ollama/.env -f deploy/ollama/compose.yaml -f deploy/ollama/compose.bootstrap.yaml down
docker compose --env-file deploy/ollama/.env -f deploy/ollama/compose.yaml up -d
docker compose --env-file deploy/ollama/.env -f deploy/ollama/compose.yaml ps
curl --fail http://127.0.0.1:11434/api/version
curl --fail http://127.0.0.1:11434/api/tags > ollama-models.json
curl --fail http://127.0.0.1:11434/api/chat -H 'Content-Type: application/json' -d '{"model":"qwen3:4b","messages":[{"role":"user","content":"2 + 2 = ?"}],"stream":false,"think":false,"options":{"num_ctx":8192,"num_predict":32,"temperature":0}}'
```

Не добавляйте `down -v`: это удалит модельный volume. Compose ограничивает Ollama
6 ГБ RAM / 4 CPU на **отдельной** ноде; эти лимиты не суммируются незаметно с
основным RAG VPS. Проверяйте `docker stats`, OOM и качество на своём оборудовании.
В текущей рабочей среде Docker Engine недоступен: конфигурация проверена статически,
но эти контейнерные команды и реальный inference пока не выполнены.

Официальный образ использует `/root/.ollama`; отдельный named volume сохраняет
модели и локальные настройки после пересоздания. Не передавайте в контейнер
Google/API secrets и Docker socket. Порт опубликован только на loopback.
Файл соответствует [официальному CPU Docker запуску](https://docs.ollama.com/docker)
с дополнительными ограничениями ресурсов/сети. GPU в этой конфигурации не включён;
NVIDIA требует отдельной настройки драйвера и Container Toolkit.

## Доступ к удалённой Ollama

На машине, с которой выполняется проверка, откройте туннель:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 11434:127.0.0.1:11434 user@ollama-server
```

Тогда endpoint для локального Python остаётся `http://127.0.0.1:11434`.
Не открывайте 11434 в публичном firewall. Если порт занят, используйте слева
11435 и укажите `OLLAMA_BASE_URL=http://127.0.0.1:11435`.
Когда RAG будет в контейнере, его `127.0.0.1` означает сам контейнер: потребуется
отдельный маршрут/туннель или закрытый VPN endpoint; host SSH tunnel автоматически
в контейнер не переносится. Сейчас smoke запускается Python-процессом на хосте.

## Проверка адаптера этого репозитория

В Python-окружении проекта с установленными `requirements-bot.txt`, из корня
ветки `feat/local-first-vps`:

```powershell
$env:RAG_ENV = 'development'
$env:RAG_PROFILE = 'LOCAL'
$env:RAG_LLM_PROVIDER = 'ollama'
$env:RAG_EMBEDDING_PROVIDER = 'local'
$env:RAG_MODEL = 'qwen3:4b'
$env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
python -m scripts.smoke_ollama
```

Linux эквивалент: `RAG_ENV=development RAG_PROFILE=LOCAL RAG_MODEL=qwen3:4b python -m scripts.smoke_ollama`.
Проверка передаёт только нейтральный арифметический вопрос, без ваших PDF.
Успех: `status=response_received`, ответ и usage; ошибка — ненулевой exit code.
Автоматического скачивания модели и перехода в Gemini нет. Протокол:
[Ollama chat API](https://docs.ollama.com/api/chat).

После успешного запуска для продолжения интеграции нужны только URL, имя модели,
её digest и параметры машины (RAM/CPU/GPU). Ключей API для локальной Ollama нет.

## Диагностика и обновление

- `connection refused`: Ollama не запущена либо неверен endpoint/туннель.
- `model not found`: выполнить pull выбранного локального tag в bootstrap-режиме.
- OOM/сильная задержка: остановить другие модели, проверить RAM и `ollama ps`;
  уменьшить модель/контекст. Качество после смены модели оценить заново.
- `Production requires verified tokenizer...`: это текущий статус интеграции,
  development smoke не является разрешением production-развёртывания.
- Обновление: записать прежний image digest/model digest, получить новый image,
  проверить smoke отдельно, затем обновить `.env`. Для отката вернуть прежний
  image digest и сохранённый модельный volume/backup. Один model digest без весов
  не гарантирует возможность восстановления после удаления модели.
