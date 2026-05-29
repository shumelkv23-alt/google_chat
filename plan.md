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
| 6 | Bot endpoint + RAG | ✅ Готово | 2-3 дня |
| 6.5 | Intent router + аналитика + графики | ✅ Готово | 3-4 дня |
| 7 | Память диалога + свёртка | ✅ Готово | 1.5 дня |
| 8 | Идемпотентность + edit/delete | ✅ Готово (Redis → этап 9) | 2 дня |
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
   - [x] Принимает `context_messages` (окно чата) и `open_vacancies` (top-K из БД)
         как два независимых источника контекста
4. [x] `app/services/extraction.py` (`run_extraction`):
   - [x] prefilter → если yes → extract → resolver
   - [x] Перед extract эмбеддит текст сообщения и достаёт top-3 открытых вакансий
         из БД → передаёт extractor'у как «источник истины» (закрывает длинные
         обсуждения, когда упоминание вакансии давно ушло из окна)
5. [ ] Retry на 429/500 через `tenacity` ← не реализован
6. [x] `tests/test_extractor.py`: примеры (create/update/close/none) с мок OpenRouter

---

## Этап 5 — Entity resolution + запись состояния ✅

**Проверяемая цель:** «открыли питониста» → «по питонисту подняли до 350k» → одна вакансия + две ревизии.

**Архитектурный принцип:** extractor извлекает данные и выдаёт action как *hint* на основе
двух источников: окна сообщений (тред/space) + top-3 открытых вакансий из БД.
Финальное решение create/update/close принимает **resolver** (он же делает узкий поиск
кандидатов и сверяет через LLM).

**Шаги по порядку:**

1. [x] `app/services/extraction.py`: контекст для extractor:
   - Если есть содержательный тред → берём тред (точный контекст);
   - Иначе → последние 3 сообщения из space (для коротких follow-up в новом треде);
   - + всегда top-3 открытых вакансий из БД (по эмбеддингу текущего сообщения).
   - Для prefilter — окно 7 сообщений из space.
2. [x] `app/services/entity_resolution.py`:
   - [x] Embed `entity_ref` через OpenAI embeddings
   - [x] Узкий поиск: top-5 по `embedding <=> :query` с `1 - distance > 0.75`
   - [x] Fallback для update/close: если узкий пуст — top-5 без threshold (старые
         вакансии с непохожим названием)
   - [x] Если кандидатов нет совсем + action=create → новая вакансия
   - [x] Если кандидатов нет + action=update/close → лог `resolution_no_candidates_for_update`
   - [x] Если есть → LLM-вызов с resolution-промптом
3. [x] `app/llm/resolver.py`: модель `OPENROUTER_MODEL_RESOLVE`, **reasoning ON (`high`)**
4. [x] Транзакционная логика if/elif:
   - [x] `match + confidence ≥ 0.6` → UPDATE vacancies + INSERT revision(action)
   - [x] **action=create, но match найден → переводим в "update"** (это не дубль, а
         дополнение к существующей записи) + лог `resolution_create_to_update`
   - [x] `match=null + action=create` → INSERT vacancy + INSERT revision(create)
   - [x] `match есть, но confidence < 0.6` → INSERT revision(pending), vacancies не трогаем
   - [x] `match=null + action=update/close` → лог `resolution_unmatched_update`,
         без pending к случайному кандидату
5. [x] Запись `changed_field/old_value/new_value` по дифу (JSON в Text-полях).
       При close embedding не обновляем — сохраняем для поиска в close-ревизии.
6. [x] `tests/test_resolution.py`: create → update → close + pending (2 теста, all 26 pass)

---

## Этап 6 — Bot endpoint + RAG ✅

**Проверяемая цель:** в DM бота «открыта ли вакансия питониста?» → осмысленный ответ из реальных данных.

**Шаги по порядку:**

1. [x] Парсинг event types (`MESSAGE`, `ADDED_TO_SPACE`, `REMOVED_FROM_SPACE`) в `/chat/interaction`
2. [x] Валидация Google JWT (audience = `CHAT_APP_AUDIENCE`)
3. [x] Извлечение `user_id`, `space_id`, `text`; UPSERT в `conversations` при первом обращении
4. [x] `app/services/rag.py`:
   - [x] Embed запроса (OpenAI)
   - [x] Cosine top-K (K=8) по `chat_messages_embeddings`
   - [x] Cosine top-3 по `vacancies.embedding` (fix: `SET ivfflat.probes=50`)
   - [x] Сборка контекста по шаблону из [docs.md](docs.md)
5. [x] `app/llm/answerer.py`:
   - [x] Модель `OPENROUTER_MODEL_ANSWER`
   - [x] Reasoning: off для простых, `high` для «почему/как/какие»
   - [x] Промпт «отвечай по контексту, если данных нет — скажи»
6. [x] Заменить эхо на ответ от RAG в `/chat/interaction`
7. [x] Возврат `{"text": "..."}` синхронно (Google Chat ждёт ≤30 сек)
8. [ ] Smoke: 5 вопросов разной формы ← протестировано вручную

---

## Этап 6.5 — Intent router + аналитика + графики ❌

**Проверяемая цель:** бот корректно отвечает на 4 типа запросов:
- «открыта ли вакансия X?» → семантический поиск (текущий RAG)
- «сколько вакансий по python?» → SQL COUNT с фильтром по теме
- «что добавили за последние 3 дня?» → SQL по `created_at`
- «покажи график открытых по командам» → SQL + картинка в Cards V2

**Архитектурный принцип:** на входе в `/chat/interaction` ставим **intent router** —
лёгкий LLM-классификатор, который определяет, какой инструмент применить. RAG остаётся
одним из инструментов, а не единственным путём.

### 6.5.1 — Intent router ✅

1. [x] `app/llm/intent_router.py`:
   - [x] Модель `OPENROUTER_MODEL_PREFILTER`
   - [x] Промпт с 6 примерами (search/count/list_recent/chart)
   - [x] Pydantic `Intent` с валидацией (days 1-365, Literal-ограничения)
   - [x] Fallback на `kind=search` при ValidationError/JSONDecodeError
2. [x] `tests/test_intent_router.py`: 10 тестов, all pass (6 happy path + 4 fallback)

### 6.5.2 — Аналитические функции ✅

3. [x] `app/services/analytics.py`:
   - [x] `count_vacancies(topic, status, days)` — SQL COUNT.
         Тему фильтруем через cosine similarity > 0.5 (через `_embed_topic` + pgvector).
   - [x] `list_recent(days, status, limit=10)` — `make_interval(days => :days)`,
         сортировка по `created_at DESC`.
   - [x] `group_count(group_by, status)` — `GROUP BY` с whitelist'ом колонок
         (`team`, `status`, `owner` → `owner_name`). NULL → 'не указано' через COALESCE.
   - [x] Общий helper `_build_filters` для status/days/topic — одна точка истины.
4. [x] `tests/test_analytics.py`: 6 тестов на живой БД (chatbot-pg), all pass.
       Сидим 5 тестовых вакансий разных команд/дат/статусов, режем по `owner_id`
       чтобы не задеть другие данные.

### 6.5.3 — Графики через QuickChart ✅

5. [x] `app/services/charts.py`:
   - [x] `build_chart_config(data, chart_type, title)` — из tuple-списка в chart.js JSON.
         Pie показывает легенду, bar/line — нет (заголовок достаточно).
   - [x] `create_chart_url(config)` — POST `/chart/create` → возвращает короткий URL.
         При HTTP-ошибке или `success=false` возвращает `None`.
6. [x] `app/services/chat_cards.py`:
   - [x] `build_chart_card(title, image_url, summary)` → структура `{"cardsV2": [...]}`
         с image widget и опциональным summary через `textParagraph`.
7. [x] `tests/test_charts.py`: 9 тестов (конфиг bar/pie/line, успех/HTTP-error/no-success, cards).

### 6.5.4 — Маршрутизация в bot endpoint ✅

8. [x] `app/services/interaction_handler.py` — выделил dispatch в отдельный модуль,
       endpoint остался тонким:
   - [x] `handle_query(query, conversation)` → (payload, turn_text)
   - [x] Switch по `intent.kind`: count / list_recent / chart / search
   - [x] `count` — форматирует `«Нашёл N вакансий по X за Y дн.»` с русской плюрализацией.
   - [x] `list_recent` — список с зарплатой/командой/датой; дефолт days=7.
   - [x] `chart` — `group_count` + `create_chart_url` → Cards V2.
         При `url=None` — **fallback на текстовый ответ** с теми же числами.
   - [x] `search` — старый RAG (`build_rag_context` + `generate_answer`).
   - [x] Логи `interaction_intent` с kind/topic/days/status/group_by.
9. [x] `app/api/interactions.py`:
   - [x] Парсинг event + JWT + conversation как раньше,
         основной путь → `handle_query` → JSONResponse.
   - [x] В `recent_turns` пишем `turn_text` (для chart — короткое описание графика).
10. [x] `tests/test_interaction_handler.py`: 16 тестов с моками analytics/charts/RAG —
    проверяем именно роутинг + форматирование. Все 67 тестов в репо зелёные.

### 6.5.5 — Запасные вопросы ✅ (встроены в 6.5.4)

11. [x] При ошибке `intent_router` (битый JSON / неверный kind) → `kind=search` (fallback на RAG).
12. [x] Пустой результат SQL → дружелюбный текст вида
        «По таким условиям ничего не нашёл / За N дн. ничего нового / Нет данных для графика».
13. [x] При сбое QuickChart (HTTP-error, success=false) → текстовый ответ с теми же числами.

### 6.5.6 — График зарплат по вакансиям ✅

Зарплаты — числовая шкала, а не категория. Сделана отдельная ветка вместо
впихивания в `group_count`.

14. [x] `Intent.kind` расширен значением `"salary_chart"`.
15. [x] `intent_router` промпт: 3 новых примера ("график зарплат", "сколько платят",
        "зарплаты по python вакансиям"). Явно разделяется с `chart` (распределения).
16. [x] `analytics.list_vacancy_salaries(topic, status, days, hours, limit=15)`:
        SQL с WHERE `(salary_min IS NOT NULL OR salary_max IS NOT NULL)`,
        сортировка по `salary_max DESC NULLS LAST`.
17. [x] `charts.build_salary_chart_config(rows, title)` — bar chart с двумя
        датасетами (min и max рядом). Валюта — самая частая из выборки.
18. [x] `_handle_salary_chart` в interaction_handler:
        - hours/days/topic/status фильтры пробрасываются;
        - заголовок графика учитывает scope + topic;
        - в `turn_text` идёт топ-3 зарплат для лога;
        - fallback на текст с `_format_salary_range` при `url=None`.
19. [x] Тесты: 4 на charts (datasets, NULL, валюта, пусто), 2 на analytics
        (всего + фильтр по topic), 4 на handler (success / empty / fallback / topic).
        Все 82 теста зелёные.

**Бюджет:** ~$0.0001 за запрос (intent router ≈ 200 токенов deepseek-flash).
QuickChart — бесплатно для < 100 запросов/мин.

---

## Этап 7 — Память диалога + свёртка ✅

**Проверяемая цель:** после 12 реплик `recent_turns` = 6 последних, `running_summary` непустой,
старые реплики не потеряны (их содержание ушло в summary).

> ⚠️ **Celery не используется** (как в этапах 3-4): свёртка идёт через FastAPI `BackgroundTasks`
> — fire-and-forget после возврата ответа, чтобы не задерживать ответ Google Chat (≤30 сек).
>
> **Что уже готово до старта этапа:**
> - `_append_turns` ([interactions.py](app/api/interactions.py)) уже делает append реплик +
>   `turns_count += 1` (но обрезает тупо до 10 — **теряет** старое без свёртки).
> - `running_summary` + `recent_turns` уже **читаются** в RAG ([rag.py](app/services/rag.py)).
> - `openrouter_model_summarize` уже в [config.py](app/config.py).
>
> **Единица счёта:** `recent_turns` хранит отдельные реплики (`user`/`assistant`),
> один обмен = 2 элемента. Пороги: `MAX_TURNS=12` (триггер свёртки), `KEEP_LAST=6`.

**Шаги по порядку:**

1. [x] [app/llm/summarizer.py](app/llm/summarizer.py) — `summarize_conversation(existing_summary, old_turns)`:
   - Модель `settings.openrouter_model_summarize`, reasoning OFF.
   - Промпт: «обнови сжатую выжимку диалога; сохрани факты, интересы и контекст;
     не дублируй то, что уже в summary; верни только новый текст summary».
   - На вход — старый `running_summary` + список вытесняемых `old_turns`.
   - Возврат: строка нового summary (тримится).

2. [x] [app/services/memory.py](app/services/memory.py) — `compact_conversation(user_id, space_id)`:
   - Читает `Conversation`, делит `recent_turns` на `old = turns[:-KEEP_LAST]` и `keep = turns[-KEEP_LAST:]`.
   - Если `len(turns) <= KEEP_LAST` — выходим (нечего сворачивать).
   - Вызывает `summarize_conversation(running_summary, old)`.
   - Транзакционно пишет `running_summary = новый`, `recent_turns = keep`, `summary_updated_at = NOW()`.
   - Свой `AsyncSessionLocal()` (фоновый таск вне запроса), все ошибки — в лог, не наружу.
   - Константы `MAX_TURNS = 12`, `KEEP_LAST = 6`.

3. [x] Переписать обрезку в `_append_turns` ([interactions.py](app/api/interactions.py)):
   - Убран жёсткий `turns[-10:]` — теперь окно вытесняет свёртка, а не append.
   - После append: если `len(turns) >= MAX_TURNS` → `background_tasks.add_task(compact_conversation, ...)`.
   - `BackgroundTasks` проброшен в `chat_interaction` и `_append_turns`.

4. [ ] ⏭️ (опционально, пропущено) `update_user_profile` + чтение `user_profile` в RAG.
   YAGNI: проверяемая цель этапа закрыта без него, RAG `user_profile` сейчас не читает —
   делать «на будущее» спекулятивно. Вернуться, если профиль реально понадобится.

5. [x] [tests/test_memory.py](tests/test_memory.py) — 6 тестов (все зелёные):
   - `test_summarize_conversation_builds_prompt` — мок OpenRouter, проверка модели + контекста.
   - `test_compact_conversation_folds_old_turns` — живая БД: 14 реплик → summary непуст, окно = 6.
   - `test_compact_conversation_noop_when_short` — короткое окно: свёртка не вызывается.
   - `test_append_turns_triggers_compaction_at_threshold` / `..._no_compaction_below_threshold`
     — триггер фоновой свёртки по порогу.
   - `test_simulate_15_exchanges_keeps_window_bounded` — 15 обменов: окно ≤ 12, summary непуст.

> ✅ Не связанный с этапом 7 падавший тест `test_parse_we_event_skips_non_created` исправлен
> в этапе 8: `parse_we_event` снова разбирает тип события, тест переписан под новый контракт
> (мусорный тип → `None`, deleted/updated парсятся).

---

## Этап 8 — Идемпотентность, edit/delete ✅ (Redis-дедуп отложен на этап 9)

**Проверяемая цель:** двойная доставка не дублирует (ни строку, ни ревизию, ни LLM-вызов);
`MESSAGE_UPDATED` пересчитывает text + embedding + state; `MESSAGE_DELETED` мягко прячет
сообщение из RAG, а если оно было единственным источником вакансии — мягко удаляет и вакансию.

> **Что уже готово до старта этапа:**
> - `chat_messages.message_id` — `UNIQUE`, ingest делает `on_conflict_do_nothing`
>   ([ingest.py](app/services/ingest.py)) → дубль-INSERT строки уже не проходит (пункт «ON CONFLICT» закрыт).
>
> **Главный пробел:** `parse_we_event` ([incoming.py](app/schemas/incoming.py)) **не смотрит на тип события** —
> различает только пустой/непустой текст. Поэтому `MESSAGE_UPDATED` приходит как обычное сообщение
> и `on_conflict_do_nothing` его молча игнорит (правка не применяется), а `MESSAGE_DELETED`
> отсекается по пустому тексту без soft-delete. Этап 8 возвращает разбор типа — **это заодно
> чинит падающий `test_parse_we_event_skips_non_created`** (см. этап 7).
>
> **Скоуп-решение (удаление вакансии при delete сообщения):** мягкое, по единственному источнику.
> - `chat_messages.is_deleted=true` (прячем из RAG).
> - Если удалённое сообщение — **единственный источник** вакансии (все её ревизии ссылаются
>   только на него) → `vacancies.is_deleted=true` (soft-delete всей вакансии — частый случай
>   «одно сообщение = одна вакансия»).
> - Если у вакансии есть живые сообщения-источники → вакансию **не трогаем**, пишем лог.
> - **Не** жёсткий `DELETE` (порвёт FK на `vacancy_revisions`/`last_message_id`, убьёт журнал,
>   необратимо) и **не** `status='closed'` (это бизнес-смысл «закрыли», врёт аналитике).
>   Полный replay-откат ревизий из середины истории — вне скоупа (YAGNI).

**Шаги по порядку:**

1. [x] **Миграция [`0002_edit_delete.py`](alembic/versions/0002_edit_delete.py):**
   - `chat_messages`: добавить `is_deleted BOOLEAN NOT NULL DEFAULT false`,
     `is_edited BOOLEAN NOT NULL DEFAULT false`.
   - `vacancies`: добавить `is_deleted BOOLEAN NOT NULL DEFAULT false` (soft-delete вакансии).
   - `vacancy_revisions`: `UNIQUE (source_message_id, action)` — БД-защита от дубль-ревизий
     (частичный индекс `WHERE source_message_id IS NOT NULL`, т.к. оно nullable).
   - Обновить модели в [models.py](app/db/models.py) (`ChatMessage`, `Vacancy`, индекс при необходимости).
   → проверка: `alembic upgrade head` проходит, колонки и constraint видны в `psql`.

2. [x] **Разбор типа события в `parse_we_event`** ([incoming.py](app/schemas/incoming.py)):
   - Добавить в `IncomingMessage` поле `event_type: Literal["created","updated","deleted"]`.
   - Маппинг WE-типа (`google.workspace.chat.message.v1.{created,updated,deleted}`) → `event_type`.
   - Для `deleted` текст может быть пуст — **не отсекать по пустому тексту**, если тип `deleted`.
   - Неизвестный тип → `None` (как раньше).
   → проверка: переписать/вернуть тест на типы — created/updated/deleted дают нужный `event_type`,
     мусорный тип → `None` (чинит существующий упавший тест).

3. [x] **Роутинг по типу в `/chat/pubsub-push`** ([pubsub.py](app/api/pubsub.py)):
   - `created` → как сейчас (`ingest_message` + `run_extraction`).
   - `updated` → `background_tasks.add_task(handle_edit, incoming)`.
   - `deleted` → `background_tasks.add_task(handle_delete, incoming)`.
   → проверка: мок-события трёх типов вызывают нужные таски (мок `add_task`).

4. [ ] ⏭️ **Дедуп обработки в Redis — отложено на этап 9** (там Redis вводится для throttling/rate
   limits, дедуп идёт прицепом). БД уже защищает от дубль-строк (ON CONFLICT) и дубль-ревизий
   (шаг 1, UNIQUE) — это покрывает корректность. Redis нужен лишь чтобы **не тратить деньги на
   повторный LLM-вызов** при редкой двойной доставке Pub/Sub.
   - План на этап 9: `app/services/dedup.py` (`redis.asyncio`), `seen_recently(message_id)` через
     `SET key NX EX 86400`; в `pubsub_push` перед постановкой тасков — ранний `204` при дубле.

5. [x] **`handle_edit`** в [edits.py](app/services/edits.py):
   - `UPDATE chat_messages SET text=:t, is_edited=true WHERE message_id=:mid`.
   - Пересчёт embedding: `DELETE` старого `chat_messages_embeddings` для этой строки + новый эмбеддинг.
   - Перезапуск `run_extraction(incoming)` на новом тексте (новые ревизии лягут штатно через resolver).
   → проверка: integration на живой БД — правка текста меняет `text`, `is_edited=true`, embedding обновлён.

6. [x] **`handle_delete`** там же (логика по единственному источнику — см. скоуп-решение):
   - `UPDATE chat_messages SET is_deleted=true WHERE message_id=:mid` (soft-delete, строку не трём).
   - Найти `msg_uuid` удалённого сообщения → вакансии, у которых есть ревизии с этим `source_message_id`.
   - Для каждой такой вакансии: посчитать её ревизии с источником **не** из удалённых сообщений.
     Если таких нет (единственный источник — это сообщение) → `vacancies.is_deleted=true`,
     лог `vacancy_soft_deleted`. Иначе → не трогаем, лог `vacancy_source_deleted_kept`.
   → проверка: integration — (а) delete единственного create-источника → у вакансии `is_deleted=true`;
     (б) delete одного из нескольких источников → вакансия живёт, `is_deleted=false`.

7. [x] **Фильтр `is_deleted=false` во всех поисковых запросах** (и сообщения, и вакансии):
   - [rag.py](app/services/rag.py): `_search` — `msg_sql` + `WHERE cm.is_deleted = false`,
     `vac_sql` + `AND is_deleted = false`.
   - [entity_resolution.py](app/services/entity_resolution.py): `_find_candidates` —
     `AND is_deleted = false` (чтобы резолвер не матчил на удалённые вакансии).
   - [analytics.py](app/services/analytics.py): `count_vacancies` / `list_recent` /
     `group_count` / `list_vacancy_salaries` — добавить `is_deleted = false` в `_build_filters`.
   → проверка: удалённое сообщение не в RAG-контексте; soft-deleted вакансия не в поиске,
     не в аналитике, не в кандидатах резолвера (integration).

8. [x] **Идемпотентность ревизий на уровне кода** ([entity_resolution.py](app/services/entity_resolution.py)):
   - Helper `_add_revision` через `pg_insert(...).on_conflict_do_nothing` по партиальному
     индексу `(source_message_id, action)` — используется во всех трёх вставках (create/update/pending).
   → проверка: повторный `_add_revision` для того же сообщения+action не плодит вторую ревизию.

9. [x] **Тесты** (всего по репо 23 зелёных):
   - [test_ingest.py](tests/test_ingest.py) — parse типов событий (created/updated/deleted/мусор),
     deleted допускает пустой текст.
   - [test_pubsub_routing.py](tests/test_pubsub_routing.py) — роутинг 3 типов + мусор (4 теста).
   - [test_edit_delete.py](tests/test_edit_delete.py) — edit (text+embedding+is_edited),
     edit неизвестного сообщения (no-op), delete единственного источника → вакансия `is_deleted`,
     delete одного из нескольких → вакансия живёт, дедуп ревизий, аналитика не видит удалённые (7 тестов).
   → проверка: `pytest -q` → 23 passed, без регрессий. (Redis-дедуп-тест придёт с этапом 9.)

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

---

# Бэклог идей (фичи на будущее)

> Не приоритет, шаги пока не расписаны — фиксация направлений, чтобы не потерять.

## B1 — Больше типов визуализаций в QuickChart

Сейчас [charts.py](app/services/charts.py) умеет bar / pie / line + отдельный salary-chart.
Идея — заготовки под расширение набора:
- новые типы: stacked bar (вакансии по командам × статусам), doughnut, horizontalBar,
  scatter/bubble (зарплата × срок открытия), радар по навыкам;
- вынести общий каркас `build_chart_config` так, чтобы добавление типа было «дописать ветку»,
  а не копипастить конфиг;
- расширить `Intent` ([intent_router.py](app/llm/intent_router.py)) и роутинг в
  [interaction_handler.py](app/services/interaction_handler.py) под новые виды графиков.

## B2 — Более точные методы обновления вакансий из пространства

Сейчас обновление идёт через extraction + entity resolution по эмбеддингам
([entity_resolution.py](app/services/entity_resolution.py)). Идея — повысить точность,
опираясь на структуру самого space:
- использовать thread_id как сильный сигнал принадлежности к одной вакансии
  (реплики в одном треде → почти наверняка та же позиция);
- учитывать автора/овнера и временную близость сообщений при матчинге кандидатов;
- возможно, отдельная привязка «тред ↔ вакансия» в БД вместо чисто семантического поиска;
- цель — меньше ложных «create вместо update» в длинных обсуждениях.

## B3 — Загрузка резюме и подбор подходящих вакансий

Новый сценарий (обратное направление RAG): пользователь кидает резюме боту →
бот ищет подходящие открытые вакансии.
- приём вложения/текста резюме в [interactions.py](app/api/interactions.py)
  (Google Chat attachments или текст);
- извлечение ключевых данных резюме (навыки, грейд, зарплатные ожидания) через LLM;
- эмбеддинг резюме → cosine-поиск по `vacancies.embedding` + фильтры (status=open, зарплата);
- ответ карточкой со списком матчей и кратким «почему подходит».
