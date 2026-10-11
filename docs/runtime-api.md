# Внутренний API

CLI, Telegram и Streamlit используют `rag.runtime_client.RuntimeClient`.
Только runtime загружает модели/FAISS. API слушает закрытый HTTP endpoint,
по умолчанию loopback:8080; Compose предоставляет private network.

Каждый запрос содержит `Authorization: Bearer <service secret>` и
`X-RAG-Principal`. Секрет определяет namespace адаптера; principal добавляет
доверенный адаптер, не конечный пользователь. Отдельные секреты cli/web/tg/health
создаёт `python -m scripts.init_secrets DIRECTORY`. Компрометация секретного ключа
адаптера позволяет выдавать себя за его пользователей. Это не документный ACL:
авторизованные пользователи имеют доступ к общему корпусу тем.

| Метод/путь | Назначение |
|---|---|
| GET /topics | Доступные каталоги тем |
| POST /query | JSON: topic, session, mode, question |
| POST /history/reset | JSON: topic, session; очищает все режимы этой сессии |
| GET /sources/{token} | Проверенный исходный файл |
| GET /exports/{token} | PPTX сохранённого ответа без нового LLM-вызова |
| POST /charts/{choice} | URL-encoded choice; JSON topic=Банкинг; проверенная SQLite |
| GET /health/live | Жив ли HTTP сервис |
| GET /health/ready | Готовность runtime, очередь и ресурсы без LLM-вызова |

Query возвращает answer, mode, evidence_status, index_version, источники с metadata
и download_token, а также export_token при допустимости PPTX. Текст чанков в
источниках не передаётся. История изолирована по adapter/principal/session/topic/mode.
Готовность runtime не гарантирует наличие индекса каждой темы: запрос отдельно
проходит snapshot/index проверки. Без генерации readiness=false.

Download/export токены случайные, привязаны к principal и исходным session/topic,
живут 15 минут; хранится максимум 128. Смена темы не переносит токен другому
пользователю. Reset отзывает токены соответствующей сессии/темы. In-flight ответы
во время reset консервативно отменяются. При скачивании повторно проверяются путь,
SHA-256 источника и срок токена; изменённый файл не выдаётся.

Ограничения: 16 HTTP потоков, запрос 64 KiB, файл/ответ клиента 32 MiB,
одна операция подготовки/выдачи файлов одновременно. Файлы буферизуются в памяти
с ограничением размера; потоковой выдачи больших файлов пока нет. HTTP timeout
клиента 65 секунд; прекращение ожидания не гарантирует прекращение уже запущенной
генерации. Не повторяйте автоматически дорогие запросы после timeout.

API не журналирует URL с capability tokens, возвращает no-store/nosniff,
санитизирует ошибки. Клиент не следует redirects и не использует HTTP proxy
из окружения. Между хостами нужен VPN/TLS; публичное размещение HTTP не предусмотрено.
При остановке runtime прекращает приём и дожидается worker до освобождения lock.
