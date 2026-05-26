# План реализации: Чат-бот для вакансий

> Версия: 3.0 · Дата: 2026-05-25
> Источник требований: [docs.md](docs.md)

## Легенда статусов

- ✅ **Готово** — этап полностью завершён
- 🟡 **Частично** — что-то сделано, что-то нет (смотри галочки внутри)
- ❌ **Не начато** — нет ни одного шага
- ⏭️ **Пропущено** — решили не делать сейчас

---

## 📊 Общий прогресс

| # | Этап | Статус | Срок |
|---|------|--------|------|
| 0a | GCP-проект + API | ✅ Готово | — |
| 0b | Pub/Sub топик + права | ✅ Готово | — |
| 0c | OAuth Client + refresh token | ✅ Готово | — |
| 0d | Регистрация Chat App | ✅ Готово | — |
| 0e | ЧАТ А + тестовые сообщения | ✅ Готово | — |
| 0f | Локальное окружение + ngrok | 🟡 Частично (~ngrok) | 1 день |
| 1 | База данных + миграции | ✅ Готово | 1 день |
| 2 | FastAPI + OAuth + Pub/Sub | 🟡 Частично | 2 дня |
| 3 | Ingest + embeddings | ✅ Готово | 1.5 дня |
| 4 | LLM pre-filter + extraction | ✅ Готово | 2 дня |
| 5 | Entity resolution + state | ✅ Готово | 2 дня |
| 6 | Bot endpoint + RAG | ❌ Не начато | 2-3 дня |
| 7 | Память диалога + свёртка | ❌ Не начато | 1.5 дня |
| 8 | Идемпотентность + edit/delete | ❌ Не начато | 2 дня |
| 9 | Очереди + rate limits + метрики | ❌ Не начато | 2-3 дня |
| 10 | Тесты + security + доводка | ❌ Не начато | 2-3 дня |

**Контекст:**
- **LLM stack:** все LLM-задачи через **OpenRouter** (`deepseek/deepseek-v4-flash`). Embeddings — `text-embedding-3-small` через **OpenAI API** напрямую (1536 dim).
- **Reasoning:** off для pre-filter и extraction, `high` для entity resolution и сложных RAG-вопросов.
- **Окружение:** локально (Windows) + ngrok для вебхуков → Google.
- **Бюджет:** разработка ~$5-10, прод ~$20-50/мес при 1000 сообщений/день.

---

# Часть I. Подготовка инфраструктуры

## Этап 0a — GCP-проект и включение API ✅

**Проверяемая цель:** в `gcloud projects list` виден проект, в `gcloud services list --enabled` — все нужные API.

- [x] Создан GCP-проект `vacanciesbot-496815`
- [x] Привязан биллинг
- [x] Зафиксирован Project ID
- [x] Включены API: `chat.googleapis.com`, `workspaceevents.googleapis.com`, `pubsub.googleapis.com`, `iam.googleapis.com`

---

## Этап 0b — Pub/Sub топик и права для WE API ✅

**Проверяемая цель:** топик существует, у WE API сервис-аккаунта есть `pubsub.publisher` на нём.

- [x] Создан топик `projects/vacanciesbot-496815/topics/chat-events-topic`
- [x] WE API сервис-аккаунт (`chat-api-push@system.gserviceaccount.com`) имеет `pubsub.publisher` на топик
- [ ] Push-подписка на топик с endpoint `APP_BASE_URL/chat/pubsub-push` ← создаётся в этапе 2

---

## Этап 0c — OAuth Client ID для user-auth ✅

**Проверяемая цель:** есть refresh token, можно получать access token для WE API.

- [x] OAuth consent screen настроен (External, scopes для chat.spaces.readonly + chat.messages.readonly)
- [x] OAuth Client ID создан (Desktop app)
- [x] [oauth-credentials.json](oauth-credentials.json) скачан
- [x] [create_sub.py](create_sub.py) запущен → refresh token получен и сохранён в [token.json](token.json)
- [ ] `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` прописаны в `.env` ← пока пусто, читаем из token.json

---

## Этап 0d — Регистрация бота (Chat App) ✅

**Проверяемая цель:** бот найден в Google Chat, открывается DM.

- [x] App name: `VacancyBot`
- [x] Avatar URL, description
- [x] Functionality: Receive 1:1 messages + Join spaces and group conversations
- [x] HTTP endpoint URL прописан (`/chat/interaction`)
- [x] Authentication audience = Project number
- [x] Visibility настроен

---

## Этап 0e — Создание ЧАТ А и тестовых сообщений ✅

**Проверяемая цель:** пространство существует с тестовыми сообщениями.

- [x] Создано пространство «Вакансии — тест» в threaded-режиме
- [x] Space ID: `spaces/AAQAmmGOtCo`
- [x] Накидано 5-10 тестовых сообщений о вакансиях
- [x] Прописан в `.env` как `CHAT_A_SPACE_ID`

---

## Этап 0f — Локальное окружение и зависимости 🟡

**Проверяемая цель:** `uvicorn app.main:app` отвечает 200 на `/health`; Postgres+Redis healthy; ngrok отдаёт публичный URL.

**Шаги:**

1. [x] Структура проекта `app/` (api, config, logger, main)
2. [x] Venv с пакетами: fastapi, uvicorn, sqlalchemy, asyncpg, openai, google-auth, structlog, pydantic-settings, cryptography, httpx
3. [x] Postgres в Docker запущен (`chatbot-pg`, image `pgvector/pgvector:pg16`, порт 5432)
4. [x] Redis в Docker (`chatbot-redis` healthy через docker-compose)
5. [x] `docker-compose.yml` написан (Postgres + Redis)
6. [x] [.env](.env) заполнен: DATABASE_URL, OPENAI_API_KEY, OPENROUTER_API_KEY, GCP_PROJECT_NUMBER, GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID/SECRET, CHAT_A_SPACE_ID
7. [x] OpenRouter API-ключ получен
8. [ ] Запустить ngrok, прописать URL в Chat App Config и в `.env` как `APP_BASE_URL` ← единственный незакрытый шаг

---

# Часть II. Реализация

## Этап 1 — База данных и миграции ✅

**Проверяемая цель:** `alembic upgrade head` создаёт все 5 таблиц с индексами; smoke-тест pgvector проходит.

**Шаги по порядку:**

1. [x] Установить `alembic`, `pgvector` (Python-привязка)
2. [x] Создать `app/db/` с подмодулями: `base.py` (Declarative base), `session.py` (engine, session)
3. [x] Описать SQLAlchemy-модели по спеке:
   - [x] `ChatMessage` (id, message_id, space_id, thread_id, author_id, author_name, text, created_at, received_at, source)
   - [x] `ChatMessageEmbedding` (message_id FK, embedding `Vector(1536)`, model)
   - [x] `Vacancy` (id, title, status, salary_min/max, currency, owner, team, description, last_message_id, embedding, confidence)
   - [x] `VacancyRevision` (vacancy_id, action, changed_field, old_value, new_value, source_message_id, confidence)
   - [x] `Conversation` (user_id, space_id, running_summary, recent_turns, user_profile, turns_count)
4. [x] Инициализировать alembic (`alembic init -t async alembic`)
5. [x] Написать миграцию `0001_initial.py`:
   - [x] `CREATE EXTENSION IF NOT EXISTS vector;`
   - [x] Создание всех 5 таблиц
   - [x] Индексы (ivfflat для embedding-полей, btree для space_id+created_at, thread_id, author_id, status)
6. [x] Прогнать `alembic upgrade head` — структура проверена через `psql`
7. [x] Написать `scripts/seed.py` — 8 тестовых строк в `chat_messages` (засеяно)
8. [x] Smoke-тест `tests/test_db_smoke.py`: INSERT + ivfflat top-K поиск, score=1.0

---

## Этап 2 — FastAPI + OAuth + Pub/Sub push-подписка ✅

**Проверяемая цель:** запущенная push-подписка получает push на `/chat/pubsub-push`, JWT проходит валидацию, событие в логах.

**Шаги по порядку:**

1. [x] `app/main.py` + роутеры
2. [x] `app/config.py` на pydantic-settings — все env-переменные
3. [x] Логирование через structlog
4. [x] Эндпоинт `/chat/pubsub-push` — декод envelope + лог
5. [x] Эндпоинт `/chat/interaction` — эхо-ответ
6. [x] JWT-валидация написана (с возможностью отключить через `SKIP_JWT_VALIDATION`)
7. [x] `/health` эндпоинт
8. [x] `app/services/google_oauth.py`: загрузить refresh token, получить/обновлять access token
9. [x] `scripts/create_subscription.py` (= рабочий [sub.py](sub.py), переместить в `scripts/`)
10. [x] Pub/Sub push subscription создана вручную в GCP консоли (Subscription ID: `chat-push-sub`, endpoint: `APP_BASE_URL/chat/pubsub-push`) — скрипт не нужен
11. [x] Запустить ngrok → получить публичный URL
12. [x] Прописать `APP_BASE_URL` в `.env` + обновить endpoint в `chat-push-sub` в GCP
13. [x] ~~Включить JWT-валидацию~~ — отложено на прод (требует настройки Authentication на push-подписке в GCP; для локальной разработки `SKIP_JWT_VALIDATION=true` достаточно)
14. [x] End-to-end тест: написать в ЧАТ А → событие появилось в логах сервера

---

## Этап 3 — Ingest endpoint + embeddings ✅

**Проверяемая цель:** пишешь в ЧАТ А → через секунды строка в `chat_messages`, через ещё пару секунд — вектор в `chat_messages_embeddings`.

> ⚠️ Celery заменён на FastAPI `BackgroundTasks` (пп. 1-3, 7 — пропущены как ненужные).

**Шаги по порядку:**

1. [x] ~~Установить Celery + Redis-зависимости~~ → используется `BackgroundTasks`
2. [x] ~~Запустить Redis в Docker~~ → Redis уже есть в `docker-compose.yml`
3. [x] ~~`app/workers/celery_app.py`~~ → не нужен
4. [x] `app/schemas/incoming.py` — Pydantic `IncomingMessage` (нормализованный формат WE event)
5. [x] Маппинг WE API event → `IncomingMessage` (`parse_we_event`)
6. [x] Идемпотентный INSERT в `chat_messages` через `ON CONFLICT (message_id) DO NOTHING`
7. [x] Embedding инлайн в `ingest_message`: `OpenAI().embeddings.create(...)` + INSERT в `chat_messages_embeddings`
8. [x] В `/chat/pubsub-push` → `background_tasks.add_task(ingest_message, incoming)`
9. [x] Возврат `204` синхронно (Pub/Sub требует быстрый ack ≤ 10 сек)
10. [x] `tests/test_ingest.py`: моковый Pub/Sub payload → строка в БД + вектор

---

## Этап 4 — LLM pre-filter + extraction ✅

**Проверяемая цель:** для «открыли вакансию питониста, 300k» получаем JSON с `action=create` и полями.

> ⚠️ Celery-задача заменена на async-функцию с `BackgroundTasks` (п. 4). Tenacity не добавлен (п. 5).

**Шаги по порядку:**

1. [x] `app/llm/client.py` — обёртка над OpenAI SDK с `base_url="https://openrouter.ai/api/v1"`. Функция `chat(messages, model, response_format)`.
2. [x] `app/llm/prefilter.py`:
   - [x] Модель `OPENROUTER_MODEL_PREFILTER`, reasoning OFF
   - [x] Промпт «yes/no: это сообщение про вакансию?»
   - [x] Return bool
3. [x] `app/llm/extractor.py`:
   - [x] Модель `OPENROUTER_MODEL_EXTRACT`, reasoning OFF
   - [x] Системный + user промпт
   - [x] `response_format={"type": "json_object"}`
   - [x] Pydantic `ExtractionResult` для валидации
4. [x] `app/services/extraction.py` (`run_extraction`): prefilter → если yes → extract → лог результата
5. [ ] Retry на 429/500 через `tenacity` ← не реализован
6. [x] `tests/test_extractor.py`: примеры (create/update/close/none) с мок OpenRouter

---

## Этап 5 — Entity resolution + запись состояния ✅

**Проверяемая цель:** «открыли питониста» → «по питонисту подняли до 350k» → одна вакансия + две ревизии.

**Шаги по порядку:**

1. [x] `app/services/entity_resolution.py`:
   - [x] Embed `entity_ref` через OpenAI embeddings
   - [x] SQL: top-5 по `embedding <=> :query` с `1 - distance > 0.75`
   - [x] Если кандидатов нет → новая вакансия
   - [x] Если есть → LLM-вызов с resolution-промптом
2. [x] `app/llm/resolver.py`: модель `OPENROUTER_MODEL_RESOLVE`, **reasoning ON (`high`)**
3. [x] Транзакционная логика if/elif:
   - [x] `match + confidence ≥ 0.6` → UPDATE vacancies + INSERT revision(update/close)
   - [x] `match=null + action=create` → INSERT vacancy + INSERT revision(create)
   - [x] `confidence < 0.6` → INSERT revision(pending), vacancies не трогаем
4. [x] Запись `changed_field/old_value/new_value` по дифу (JSON в Text-полях)
5. [x] `tests/test_resolution.py`: create → update → close + pending (2 теста, 16 pass)

---

## Этап 6 — Bot endpoint + RAG ❌

**Проверяемая цель:** в DM бота «открыта ли вакансия питониста?» → осмысленный ответ из реальных данных.

**Шаги по порядку:**

1. [ ] Парсинг event types (`MESSAGE`, `ADDED_TO_SPACE`, `REMOVED_FROM_SPACE`) в `/chat/interaction`
2. [ ] Валидация Google JWT (audience = `CHAT_APP_AUDIENCE`)
3. [ ] Извлечение `user_id`, `space_id`, `text`; UPSERT в `conversations` при первом обращении
4. [ ] `app/services/rag.py`:
   - [ ] Embed запроса (OpenAI)
   - [ ] Cosine top-K (K=8) по `chat_messages_embeddings`
   - [ ] Cosine top-3 по `vacancies.embedding`
   - [ ] Сборка контекста по шаблону из [docs.md](docs.md)
5. [ ] `app/llm/answerer.py`:
   - [ ] Модель `OPENROUTER_MODEL_ANSWER`
   - [ ] Reasoning: off для простых, `high` для «почему/как/какие»
   - [ ] Промпт «отвечай по контексту, если данных нет — скажи»
6. [ ] Заменить эхо на ответ от RAG в `/chat/interaction`
7. [ ] Возврат `{"text": "..."}` синхронно (Google Chat ждёт ≤30 сек)
8. [ ] Smoke: 5 вопросов разной формы

---

## Этап 7 — Память диалога + свёртка ❌

**Проверяемая цель:** после 12 реплик `recent_turns` = 6 последних, `running_summary` непустой.

**Шаги по порядку:**

1. [ ] После каждого ответа: append в `recent_turns`, `turns_count += 1`
2. [ ] Если `len(recent_turns) > 10` → планировать Celery-задачу `tasks/summarize_conversation.py`
3. [ ] Свёртка: модель `OPENROUTER_MODEL_SUMMARIZE`, на вход `existing_summary + old_turns`
4. [ ] (опционально) `update_user_profile` — извлечь структурные факты в `user_profile`
5. [ ] Тест: симулировать 15 реплик

---

## Этап 8 — Идемпотентность, edit/delete ❌

**Проверяемая цель:** двойная доставка не дублирует; редактирование пересчитывает embedding и state.

**Шаги по порядку:**

1. [ ] Дедупликация в Redis-сете по `message_id` на 24ч
2. [ ] `ON CONFLICT` в БД (двойная защита)
3. [ ] Обработка `MESSAGE_UPDATED`: UPDATE text + флаг `is_edited` + пересчёт пайплайна
4. [ ] Обработка `MESSAGE_DELETED`: soft-delete (`is_deleted=true`)
5. [ ] Миграция: добавить `is_deleted`, `is_edited` если не было
6. [ ] WHERE `is_deleted=false` во всех RAG-запросах
7. [ ] Идемпотентность extraction: дедуп revisions по `(source_message_id, action)`

---

## Этап 9 — Очереди, rate limits, метрики ❌

**Проверяемая цель:** 100 сообщений подряд не теряются; метрики `embed_lag`, `extract_lag` доступны.

**Шаги по порядку:**

1. [ ] Celery: отдельные queues `embeddings`, `extraction`, `summarization`
2. [ ] Throttling: token bucket в Redis для OpenRouter и OpenAI отдельно
3. [ ] Tenacity на 429/500
4. [ ] Батчинг embeddings: если в очереди ≥ 16 — один OpenAI-вызов
5. [ ] Prometheus-метрики через `prometheus-fastapi-instrumentator`
6. [ ] Логи LLM-вызовов: model, prompt hash, tokens_in/out, cost_usd
7. [ ] Дневной лимит на OpenRouter ($1-2/день для разработки)

---

## Этап 10 — Тесты, безопасность, доводка ❌

**Проверяемая цель:** покрытие ≥ 80%, нет хардкод-секретов, security checklist пройден.

**Шаги по порядку:**

1. [ ] Unit-тесты: extractor / resolution / RAG-сборка с мок OpenRouter
2. [ ] Integration тесты эндпоинтов через httpx + тестовая БД (testcontainers-postgres)
3. [ ] E2E сценарии: ingest → extraction → bot question → answer
4. [ ] Pre-commit: ruff + black + mypy + bandit
5. [ ] Row-level security для `conversations` по user_id
6. [ ] Проверка на startup, что все обязательные env заданы
7. [ ] README с инструкцией поднятия

---

# Сводная оценка

| Этап | Срок | Критический? |
|---|---|---|
| **0a-0f.** GCP, бот, OAuth, ngrok, локалка | 1 день | **да** |
| 1. БД | 1 д | да |
| 2. FastAPI + OAuth + Pub/Sub | 2 д | да |
| 3. Ingest + embeddings | 1.5 д | да |
| 4. Pre-filter + extraction | 2 д | да |
| 5. Resolution + state | 2 д | да |
| 6. Bot + RAG | 2-3 д | да |
| 7. Память + свёртка | 1.5 д | да |
| 8. Идемпотентность + edit/delete | 2 д | средне |
| 9. Очереди + rate limits + метрики | 2-3 д | средне |
| 10. Тесты + security | 2-3 д | да |
| **Итого** | **~19-23 рабочих дня (4-4.5 недели)** | |

---

# Вынесено за скобки

- ⏭️ Замена LLM pre-filter на distilbert/fasttext (после первых метрик)
- ⏭️ Cloud Tasks вместо Celery (только при переезде на Cloud Run)
- ⏭️ Шифрование колонок в `conversations` (RLS на старте достаточно)
- ⏭️ Дневные саммари тредов
- ⏭️ Переход с OpenAI embeddings на Voyage `voyage-3-lite`
