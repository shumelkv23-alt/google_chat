# Архитектура и технологический стек

> Версия: 1.0 · Дата: 2026-05-27
> Цель документа: понять, **что это за приложение**, **из каких частей оно состоит**,
> **почему выбраны именно эти технологии** и **как функции взаимодействуют друг с другом**.

---

## 1. Что это вообще

Это **чат-бот для управления вакансиями в Google Chat**. Он живёт в двух местах:

1. **Чат А** — корпоративное пространство, где люди обсуждают вакансии («открыли python
   до 300k», «по тимлиду подняли вилку»). Бот **наблюдает** за этими сообщениями
   и автоматически строит структурированную базу вакансий.
2. **DM с ботом** — личка, куда пользователь может задать вопрос («сколько вакансий
   открыто», «график зарплат», «что добавили за неделю»). Бот отвечает на основе
   собранной базы.

Принципиальная идея: **никто не вводит вакансии руками**. Бот извлекает их из
живого чатового потока через LLM, а потом превращает в источник истины для всей
команды.

---

## 2. Архитектурные решения и почему так

| Решение | Почему |
|---|---|
| **Append-only лог + изменяемое состояние** (chat_messages + vacancies) | Лог даёт voprosposчтобы восстановить историю; состояние — для быстрого ответа. |
| **Pub/Sub push, не polling** | Polling = задержка + нагрузка на Google API. Push — мгновенно, бесплатно. |
| **Эмбеддинги для поиска и группировки** | «питонист» ≈ «Python developer». ILIKE такое не ловит, эмбеддинги ловят. |
| **Два LLM-провайдера** (OpenRouter для генерации + OpenAI для эмбеддингов) | OpenAI text-embedding-3-small — стандарт, дёшево и качественно. OpenRouter — даёт доступ к десяткам моделей (deepseek-v4-flash сильно дешевле OpenAI GPT-4o при сопоставимом качестве для парсинга). |
| **Reasoning ON только для сложных задач** (resolver, complex RAG) | Reasoning умножает токены × 3–10. Для prefilter/extraction он избыточен, для entity resolution — критичен. |
| **Extractor как hint, resolver как арбитр** | Extractor видит только тред — он не знает про БД. Решение create/update/close должно быть основано на состоянии БД, иначе будут дубли. |
| **Intent router перед RAG** | RAG умеет только семантический поиск. Аналитика («сколько», «за период») требует SQL. Router маршрутизирует. |
| **FastAPI BackgroundTasks вместо Celery** | На MVP-этапе нагрузка ничтожна. Celery — отдельный процесс, отдельная очередь, отдельная сложность. BackgroundTasks хватает на тысячи сообщений/день. |
| **pgvector внутри Postgres** | Один процесс, одна БД. Не нужен отдельный Pinecone/Weaviate. Для десятков тысяч векторов pgvector справляется. |

---

## 3. Технологический стек

### 3.1 Бэкенд

| Технология | Версия | Что делает | Почему |
|---|---|---|---|
| **Python** | 3.14 | язык | стандартный для AI-сервисов, хорошая экосистема LLM-клиентов |
| **FastAPI** | latest | HTTP-сервер | async-нативный, autodoc через pydantic, низкая церемония |
| **uvicorn** | latest | ASGI-server | продакшн-готовый под FastAPI |
| **pydantic** + **pydantic-settings** | v2 | валидация и конфиг из .env | type-safe, ошибка на старте если переменная не задана |
| **SQLAlchemy** | 2.x (async) | ORM | async-native в 2.x, типизация через Mapped[T] |
| **asyncpg** | latest | драйвер PostgreSQL | быстрее psycopg, поддерживает async из коробки |
| **alembic** | latest | миграции БД | стандарт для SQLAlchemy |
| **structlog** | latest | логирование | structured logs (JSON) → удобно грепать и парсить |

### 3.2 Базы и инфраструктура

| Технология | Что делает | Почему |
|---|---|---|
| **PostgreSQL 16** | хранилище | проверено временем, JSONB, расширения |
| **pgvector** | vector search внутри PG | ivfflat-индексы, cosine distance, без отдельного сервиса |
| **Redis** | (планируется) кэш, дедупликация, rate-limit | стандарт для in-memory структур |
| **Docker Compose** | локальный запуск БД | один `docker compose up` поднимает всё |

### 3.3 LLM и AI

| Сервис | Использование | Модель |
|---|---|---|
| **OpenAI** | только эмбеддинги | `text-embedding-3-small` (1536 dim, $0.02/1M токенов) |
| **OpenRouter** | весь generation/reasoning | `deepseek/deepseek-v4-flash` — для prefilter, extraction, resolution, answer, intent, summarize |

**Reasoning effort:**
- `off` — для prefilter, extraction, intent_router (быстрые классификаторы)
- `high` — для resolver и сложных RAG-вопросов («почему», «какие», «как»)

### 3.4 Google интеграции

| Сервис | Что делает |
|---|---|
| **Google Workspace Events API (WE)** | подписка на события чата (новые сообщения, edits, deletes) |
| **Google Cloud Pub/Sub** | транспорт между WE и нашим бэкендом (push-подписка с JWT) |
| **Google Chat API** | бот: получение запросов и отправка ответов (включая Cards V2 с картинками) |
| **OAuth 2.0 (user-auth)** | refresh token для подписки на пространство от имени человека |

### 3.5 Внешние сервисы

| Сервис | Использование |
|---|---|
| **QuickChart.io** | рендеринг chart.js конфигов в PNG-URL (для графиков в Cards V2) |
| **ngrok** | публичный URL для локальной разработки (Google требует HTTPS-endpoint) |

---

## 4. Структура проекта

```
google_chat/
├── app/                          # код приложения
│   ├── main.py                   # FastAPI entry point + lifespan
│   ├── config.py                 # pydantic-settings (читает .env)
│   ├── logger.py                 # structlog setup
│   │
│   ├── api/                      # HTTP endpoints
│   │   ├── pubsub.py             # POST /chat/pubsub-push — события WE
│   │   └── interactions.py       # POST /chat/interaction — бот
│   │
│   ├── db/                       # БД-слой
│   │   ├── base.py               # Declarative Base
│   │   ├── models.py             # 5 SQLAlchemy моделей
│   │   └── session.py            # engine + AsyncSessionLocal
│   │
│   ├── llm/                      # обёртки над LLM
│   │   ├── client.py             # async chat() поверх OpenRouter
│   │   ├── prefilter.py          # yes/no: про вакансию?
│   │   ├── extractor.py          # текст → ExtractionResult JSON
│   │   ├── resolver.py           # entity_ref + candidates → match
│   │   ├── answerer.py           # вопрос + RAG-context → ответ
│   │   └── intent_router.py      # запрос → Intent (search/count/chart/...)
│   │
│   ├── services/                 # бизнес-логика
│   │   ├── google_oauth.py       # refresh → access token для WE API
│   │   ├── ingest.py             # WE event → INSERT в БД + embedding
│   │   ├── extraction.py         # prefilter → extractor → resolver
│   │   ├── entity_resolution.py  # create vs update vs close
│   │   ├── rag.py                # семантический поиск + сборка контекста
│   │   ├── analytics.py          # count / list_recent / group_count / salaries
│   │   ├── charts.py             # chart.js конфиг + QuickChart
│   │   ├── chat_cards.py         # сборка Cards V2 для Google Chat
│   │   └── interaction_handler.py# главный диспетчер бота
│   │
│   └── schemas/
│       └── incoming.py           # IncomingMessage + parse_we_event
│
├── alembic/                      # миграции
│   └── versions/0001_initial.py
│
├── tests/                        # 82 теста
│   ├── test_db_smoke.py
│   ├── test_ingest.py
│   ├── test_extractor.py
│   ├── test_resolution.py
│   ├── test_rag.py
│   ├── test_intent_router.py
│   ├── test_analytics.py         # требует chatbot-pg
│   ├── test_charts.py
│   └── test_interaction_handler.py
│
├── scripts/                      # вспомогательное
│   └── seed.py
│
├── plan.md                       # дорожная карта (см. отдельно)
├── docs.md                       # требования
├── docker-compose.yml            # PG + Redis
├── .env                          # секреты (не в git)
└── alembic.ini
```

---

## 5. Структура базы данных

5 таблиц. Условно делятся на **append-only** (история) и **mutable** (текущее состояние).

### 5.1 chat_messages — append-only лог
Все входящие сообщения из чата A. Никогда не апдейтятся (только `is_edited`/`is_deleted` флаги в будущем).

| Колонка | Тип | Зачем |
|---|---|---|
| id | uuid | PK |
| message_id | text UNIQUE | id из Google Chat, для дедупликации |
| space_id | text | где написали |
| thread_id | text | какой тред |
| author_id, author_name | text | кто написал |
| text | text | сам текст |
| created_at, received_at | timestamptz | когда событие произошло / когда мы его получили |
| source | text | 'chat_a' \| 'chat_b' (на будущее) |

Индексы: `(space_id, created_at DESC)`, `thread_id`, `author_id`.

### 5.2 chat_messages_embeddings
Векторы для семантического поиска по сообщениям.

| Колонка | Тип | Зачем |
|---|---|---|
| message_id | uuid → chat_messages.id | FK с CASCADE |
| embedding | vector(1536) | OpenAI text-embedding-3-small |
| model | text | какая модель эмбедила (для миграций) |

Индекс: `ivfflat (embedding vector_cosine_ops)` — для быстрого top-K.

### 5.3 vacancies — mutable состояние
Текущая картина всех вакансий. Апдейтится через resolver.

| Колонка | Тип | Зачем |
|---|---|---|
| id | uuid | PK |
| title | text | название позиции |
| status | text | 'open'/'closed'/'on_hold'/'filled' (CHECK constraint) |
| salary_min/max, currency | int / text | зарплатная вилка |
| owner_id, owner_name | text | кто заводит/курирует |
| team | text | команда |
| description | text | детали |
| last_message_id | uuid | какое сообщение последним меняло |
| embedding | vector(1536) | эмбеддинг title + description, для поиска похожих |
| confidence | float | уверенность LLM в последней операции |
| created_at, updated_at | timestamptz | |

### 5.4 vacancy_revisions — append-only журнал
Каждое изменение состояния — отдельная строка. Восстановим историю даже после удаления вакансии.

| Колонка | Тип | Зачем |
|---|---|---|
| vacancy_id | uuid → vacancies.id | какую трогали |
| action | text | 'create'/'update'/'close'/'pending' (CHECK) |
| changed_field | text | какие поля поменялись (CSV) |
| old_value, new_value | text (JSON) | дифф |
| source_message_id | uuid → chat_messages.id | какое сообщение вызвало изменение |
| confidence | float | уверенность resolver'а |

### 5.5 conversations — память диалога с ботом
Для каждого пользователя × space — своя «нить разговора».

| Колонка | Тип | Зачем |
|---|---|---|
| user_id, space_id | text (UNIQUE) | |
| running_summary | text | свёртка старых реплик (планируется в этапе 7) |
| recent_turns | jsonb | последние 10 (user, assistant) реплик |
| user_profile | jsonb | факты о пользователе (роль, фокус) |
| turns_count | int | счётчик |
| summary_updated_at | timestamptz | когда последний раз свёртывали |

---

## 6. Поток данных

### 6.1 Pipeline A — наблюдатель за чатом

```
человек пишет в чат A
        │
        ▼
Google Workspace Events API
        │ (event published)
        ▼
Google Cloud Pub/Sub topic: chat-events-topic
        │ (push subscription)
        ▼
POST /chat/pubsub-push  ───── pubsub.py
        │
        ├─ verify JWT (audience = APP_BASE_URL/chat/pubsub-push)
        ├─ decode base64 envelope
        ├─ parse_we_event(data) → IncomingMessage
        │
        ├─ background_tasks.add_task(ingest_message)
        └─ background_tasks.add_task(run_extraction)
        │
        ▼
return 204 (≤ 1 сек)   ← Pub/Sub требует быстрый ACK (< 10 сек)


Параллельно в фоне:
─────────────────────

ingest_message(msg)                   run_extraction(msg)
  │                                     │
  ├─ INSERT chat_messages                ├─ build_contexts() — тред/space
  │  ON CONFLICT (message_id) DO NOTHING ├─ is_vacancy_message? (prefilter LLM)
  │                                     │     ↓ нет — skip
  ├─ openai.embeddings.create()         ├─ get_open_vacancies(text)
  │                                     │     эмбеддит сообщение, берёт top-3
  └─ INSERT chat_messages_embeddings     │     открытых вакансий из БД
                                        │
                                        ├─ extract_vacancy(...)  ─── LLM
                                        │     возвращает action/entity_ref/fields
                                        │     с учётом контекста + open_vacancies
                                        │
                                        └─ resolve_and_save(...)  ── см. ниже
```

### 6.2 Pipeline B — entity resolution

Сердцевина системы. Принимает решение **создать новую или обновить существующую**.

```
resolve_and_save(msg, extraction_result)
        │
        ├─ entity_ref = result.entity_ref or result.fields["title"]
        ├─ ref_vector = openai.embed(entity_ref)
        │
        ├─ узкий поиск кандидатов:
        │  similarity > 0.75 в vacancies (status != 'closed')
        │
        ├─ fallback (только для update/close):
        │  если узкий пустой → top-5 без threshold
        │
        ├─ если кандидатов нет совсем:
        │     ├─ action=create → INSERT vacancy + revision('create')
        │     └─ action=update/close → log warning, ничего не пишем
        │
        └─ если кандидаты есть:
              │
              ▼
              resolve_entity(entity_ref, candidates, text) ─── LLM reasoning=high
              │
              ├─ vacancy_id + confidence ≥ 0.6:
              │     если action=create → переписываем на 'update'
              │     UPDATE vacancies + revision(effective_action)
              │
              ├─ vacancy_id=null + action=create:
              │     INSERT vacancy + revision('create')
              │
              ├─ vacancy_id + confidence < 0.6:
              │     revision('pending') — для человека на разбор
              │
              └─ vacancy_id=null + action=update/close:
                    log warning — позиция не нашлась
```

### 6.3 Pipeline C — бот (DM)

```
человек пишет боту в DM
        │
        ▼
POST /chat/interaction  ───── interactions.py
        │
        ├─ verify JWT (audience = CHAT_APP_AUDIENCE)
        ├─ parse event_type
        │
        ├─ ADDED_TO_SPACE → приветствие
        ├─ MESSAGE        → handle_query(query, conversation)
        │
        └─ append_turns(user_text, bot_text)


handle_query()  ─── interaction_handler.py
        │
        ├─ classify_intent(query)  ─── LLM (intent_router)
        │     → Intent { kind, topic, days, hours, status, group_by, chart_type }
        │
        ├─ kind=search       → _handle_search → RAG (см. ниже)
        ├─ kind=count        → analytics.count_vacancies → текст
        ├─ kind=list_recent  → analytics.list_recent → текст-список
        ├─ kind=chart        → analytics.group_count + QuickChart → Cards V2
        └─ kind=salary_chart → analytics.list_vacancy_salaries + QuickChart → Cards V2


_handle_search() — старый RAG путь:
        │
        ├─ build_rag_context(query, conversation) ─── rag.py
        │     ├─ embed query
        │     ├─ top-K сообщений по cosine (chat_messages_embeddings)
        │     ├─ top-K вакансий по cosine (vacancies.embedding)
        │     └─ собирает строку: память + актуальные вакансии + сообщения
        │
        └─ generate_answer(query, context) ─── LLM (answerer)
              reasoning=high для «почему/как/какие», off для остальных
```

---

## 7. LLM-стек: 5 ролей, одна модель

Все LLM-вызовы идут через `app/llm/client.py` — тонкая обёртка над OpenAI SDK,
направленная на OpenRouter. У всех ролей одна и та же дешёвая модель
(`deepseek-v4-flash`), но **разные промпты** и **разные reasoning-настройки**.

| Роль | Reasoning | JSON-mode | Промпт делает |
|---|---|---|---|
| **prefilter** | off | нет | yes/no: «это про вакансию?» |
| **extractor** | off | да | текст + контекст + open_vacancies → JSON с action/fields/entity_ref |
| **resolver** | **high** | да | entity_ref + candidates → vacancy_id или null + confidence |
| **answerer** | high для «почему/как», off иначе | нет | RAG-контекст → текстовый ответ |
| **intent_router** | off | да | пользовательский запрос → JSON Intent |

**Почему один dispatcher на все роли:**
- Единый retry / rate-limit / биллинг
- Одна точка для логирования cost_usd / tokens_in/out
- Легко поменять модель в одном месте (env-переменная на каждую роль)

**Почему именно deepseek-v4-flash:**
- 10× дешевле GPT-4o-mini при сопоставимом качестве для парсинга/классификации
- Поддерживает JSON mode (`response_format`)
- Поддерживает `reasoning.effort` (нативно)

---

## 8. Семантический слой: эмбеддинги

Эмбеддинги — это «координаты смысла». Близкие по смыслу фразы получают близкие векторы.

**Где используются:**

1. **chat_messages_embeddings** — каждое сообщение → 1536-мерный вектор.
   Используется в RAG (поиск похожих сообщений на запрос).
2. **vacancies.embedding** — `title + description` → вектор. Используется:
   - В entity resolution (поиск кандидатов на матч).
   - В extraction (top-3 open vacancies подсовываются в промпт).
   - В RAG (`top-3` вакансий в контексте бота).
   - В analytics (`count`/`list_recent`/`salary_chart` с фильтром по `topic`).
3. **temporary embed запроса** — при каждом обращении к боту запрос эмбедится
   на лету и сравнивается с обоими индексами.

**Cosine similarity** (`1 - distance`):
- `> 0.75` — почти точно та же позиция (узкий поиск кандидатов в resolver)
- `> 0.5` — похожая тема (фильтр `topic` в analytics)
- `< 0.3` — мусор, неважно

**ivfflat-индекс** даёт O(log N) вместо O(N) при поиске top-K. На десятках тысяч
векторов разница в 100×.

---

## 9. Intent Router и аналитика

Когда мы добавили аналитику и графики, оказалось, что **RAG не для всего подходит**.
RAG умеет «найти похожее по смыслу» — но не «посчитай», не «отфильтруй по дате»,
не «сгруппируй по команде».

Решение — **intent_router**: лёгкий LLM-классификатор, который перед основной
обработкой решает, какой инструмент применить.

```
запрос пользователя
        │
        ▼
classify_intent (LLM, JSON output)
        │
        ▼
{ kind, topic, days, hours, status, group_by, chart_type }
        │
        ▼
switch:
  search       → RAG (embed + top-K + answerer)
  count        → SQL COUNT с фильтрами
  list_recent  → SQL ORDER BY created_at DESC
  chart        → SQL GROUP BY + QuickChart → Cards V2
  salary_chart → SQL по salary_min/max + QuickChart → Cards V2
```

**Fallback'ы (3 уровня):**
1. Если intent_router выдал битый JSON / неизвестный kind → `kind=search` (RAG).
2. Если SQL вернул 0 строк → дружелюбное «ничего не нашёл, попробуй переформулировать».
3. Если QuickChart упал → текстовый ответ с теми же числами (не теряем данные).

---

## 10. Графики и Cards V2

Когда нужно отрисовать **график** в Google Chat:

1. Аналитика возвращает данные (`group_count` или `list_vacancy_salaries`).
2. `charts.build_*_chart_config()` собирает **chart.js JSON-конфиг**.
3. `POST https://quickchart.io/chart/create` с этим конфигом → возвращается
   `{"url": "https://quickchart.io/chart/render/..."}` (PNG-URL).
4. `chat_cards.build_chart_card()` оборачивает URL в **Google Chat Cards V2** —
   стандартный формат для интерактивных сообщений с картинками.
5. Возвращаем `{"cardsV2": [...]}` — Google Chat показывает картинку
   прямо в чате.

**Два типа графиков:**
- **group_count** — bar/pie/line по категории (team/status/owner). Используется
  для «распределение открытых по командам».
- **list_vacancy_salaries** — bar с двумя датасетами (min/max). Используется для
  «график зарплат по вакансиям». Это **числовая шкала, не категория**, поэтому
  отдельная ветка.

---

## 11. Безопасность

| Угроза | Защита |
|---|---|
| Случайные звонки на endpoint Pub/Sub | JWT-валидация (audience = APP_BASE_URL/chat/pubsub-push) |
| Случайные звонки на endpoint бота | JWT-валидация (audience = Project Number / CHAT_APP_AUDIENCE) |
| Утечка API-ключей в код | pydantic-settings читает только из `.env`, `.env` в `.gitignore` |
| Двойная доставка сообщений | `ON CONFLICT (message_id) DO NOTHING` (планируется Redis-сет на 24ч) |
| SQL injection через `GROUP BY :col` | Whitelist `_GROUP_BY_COLUMN` в analytics — динамическое имя колонки только из словаря |
| SQL injection через `:vec::vector` | `CAST(:vec AS vector(1536))` — bind через SQLAlchemy text() |
| Утечка чужих диалогов | (план) row-level security на conversations по user_id |

В разработке: `SKIP_JWT_VALIDATION=true` отключает обе проверки JWT.

---

## 12. Тесты

Тестов сейчас **82** (все зелёные). Разделены по уровням:

### Unit (мок LLM и БД)
- `test_extractor.py` — prefilter + extractor с мок-LLM ответами
- `test_intent_router.py` — классификатор с мок-JSON
- `test_charts.py` — конфиги chart.js + мок httpx для QuickChart
- `test_interaction_handler.py` — dispatch routing с моками analytics/charts/RAG
- `test_rag.py` — RAG-сборка контекста, форматирование

### Integration (живой Postgres из docker-compose)
- `test_db_smoke.py` — INSERT + ivfflat top-K
- `test_ingest.py` — pipeline ingest_message
- `test_resolution.py` — entity resolution e2e (create → update → close)
- `test_analytics.py` — SQL count/list/group/salaries с реальными данными

Все integration-тесты:
- Изолируют данные через специальный `owner_id` (например, `users/analytics-test`)
- `_cleanup()` перед и после каждого теста
- `engine.dispose()` в конце каждой `_run_*` — иначе asyncpg пул соединений
  держит ссылку на закрытый event loop, и второй `asyncio.run()` падает с
  `another operation is in progress`

---

## 13. Локальный запуск

```bash
# 1. Поднять Postgres + Redis
docker compose up -d

# 2. Применить миграции
alembic upgrade head

# 3. Запустить FastAPI
uvicorn app.main:app --reload --port 8000

# 4. В другом терминале — ngrok для публичного URL
ngrok http 8000

# 5. Прописать ngrok URL в:
#    - .env как APP_BASE_URL
#    - Google Chat App Config (endpoint /chat/interaction)
#    - Pub/Sub push subscription (endpoint /chat/pubsub-push)

# 6. Запустить тесты
./venv/Scripts/python.exe -m pytest tests/ -v
```

---

## 14. Что важно понимать про взаимодействие модулей

### 14.1 Slim API endpoints

`app/api/*.py` намеренно тонкие. Они только:
- валидируют JWT
- парсят payload
- маршрутизируют в сервис
- возвращают JSON

Вся бизнес-логика — в `app/services/`. Это позволяет:
- легко тестировать сервисы без HTTP-стека
- переключать транспорт (HTTP → Cloud Tasks → Celery) меняя только endpoint

### 14.2 Двунаправленная связь LLM ↔ БД

В extraction.py мы делаем интересное:
```
текст сообщения → embed → top-3 open vacancies из БД → промпт LLM
```

То есть LLM видит **актуальное состояние БД прямо в промпте**. Это закрывает
кейс «обсуждение тянется неделю» — даже если в окне 7 последних сообщений нет
упоминания позиции, она всё равно попадёт в контекст из БД.

Цена — один лишний embed-вызов на каждое сообщение (~$0.00002).

### 14.3 Single source of truth для фильтров

В `app/services/analytics.py:_build_filters()` собраны **все** условия WHERE:
status, days, hours, topic. Любая аналитическая функция дёргает этот хелпер.

Это даёт согласованное поведение: «по python за неделю открыто» → счётчик и
список и график применят одинаковые условия.

### 14.4 Контракт между слоями

```
classify_intent  →  Intent (pydantic)
                      │
                      ▼
              handle_query (dispatcher)
                      │
        ┌─────────────┼──────────────┬──────────────┐
        ▼             ▼              ▼              ▼
   _handle_search  _handle_count  _handle_chart  _handle_salary_chart
        │             │              │              │
        ▼             ▼              ▼              ▼
    RAG          analytics      analytics +    analytics +
                                charts          charts
                      │              │              │
                      ▼              ▼              ▼
                 {"text":...}   {"cardsV2":..}  {"cardsV2":..}
```

Каждый handler возвращает `(payload_dict, turn_text)`. `payload_dict` идёт в
Google Chat, `turn_text` — в `conversations.recent_turns` (память диалога).

### 14.5 Идемпотентность

Базовая идемпотентность — на уровне БД через `UNIQUE(message_id)` и `ON CONFLICT
DO NOTHING`. То есть если Pub/Sub доставит одно и то же сообщение дважды
(а это бывает — at-least-once delivery), вторая попытка просто ничего не сделает.

Полная идемпотентность extraction'а — в планах (этап 8): дедуп `vacancy_revisions`
по `(source_message_id, action)`.

---

## 15. Куда расти

- **Этап 7** — память диалога: свёртка старых реплик в `running_summary`,
  чтобы бот «помнил» предыдущие сеансы.
- **Этап 8** — обработка edits/deletes: `MESSAGE_UPDATED`/`MESSAGE_DELETED`
  с пересчётом state.
- **Этап 9** — Celery + Redis: батчинг embeddings, throttling, метрики.
- **Этап 10** — security + покрытие тестами 80%+, pre-commit, RLS.

Подробности — в [plan.md](plan.md).
