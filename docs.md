# Документация: Чат-бот для вакансий

> Версия: 1.1 · Дата: май 2026  
> Охватывает архитектуру, схему БД, LLM-пайплайн, два ключевых архитектурных решения (контекст сущностей и память диалога)

---

## Содержание

1. [Обзор проекта](#1-обзор-проекта)
2. [Архитектура — семь слоёв](#2-архитектура--семь-слоёв)
3. [Слой 01 — Источники событий](#3-слой-01--источники-событий)
4. [Слой 02 — Доставка событий](#4-слой-02--доставка-событий)
5. [Слой 03 — FastAPI сервер](#5-слой-03--fastapi-сервер)
6. [Слой 04 — База данных](#6-слой-04--база-данных)
7. [Слой 04+ — Извлечение состояния (LLM)](#7-слой-04--извлечение-состояния-llm)
8. [Слой 05 — RAG](#8-слой-05--rag)
9. [Слой 06 — Возврат ответа](#9-слой-06--возврат-ответа)
10. [Ключевые архитектурные решения](#10-ключевые-архитектурные-решения)
11. [Потоки данных](#11-потоки-данных)
12. [Нерешённые вопросы и альтернативы](#12-нерешённые-вопросы-и-альтернативы)

---

## 1. Обзор проекта

Система состоит из двух чатов:

**ЧАТ А — Пространство (Google Chat / Workspace).** Рабочее пространство, которое система пассивно мониторит. Туда приходят сообщения о вакансиях: кто-то объявляет об открытии позиции, обсуждает зарплату, меняет условия, закрывает роль. Люди пишут в свободном стиле — без форм, без структуры.

**ЧАТ Б — Бот.** Личный диалог пользователя с ботом. Пользователь спрашивает: «Открыта ли ещё вакансия Python-разработчика?», «Кто нанимает в команду X?», «Что обсуждалось по позиции Y на прошлой неделе?» — бот отвечает на основе данных из ЧАТ А.

Главная техническая задача — извлекать структурированное знание о вакансиях из неструктурированного потока сообщений и отвечать на вопросы про **текущее состояние**, а не только про историю.

---

## 2. Архитектура — семь слоёв

```
┌─────────────────────────────────────────────────────────┐
│  01  Источники событий       ЧАТ А        ЧАТ Б / бот   │
├─────────────────────────────────────────────────────────┤
│  02  Доставка событий    WE API + Pub/Sub   Chat API     │
├─────────────────────────────────────────────────────────┤
│  03  FastAPI server      /pub-sub-push   /interactions   │
├─────────────────────────────────────────────────────────┤
│  04  PostgreSQL + pgvector   (5 таблиц)                  │
├─────────────────────────────────────────────────────────┤
│  04+ Извлечение состояния LLM (новое, Вопрос 1)          │
├─────────────────────────────────────────────────────────┤
│  05  RAG   embeddings → search → answer generation       │
├─────────────────────────────────────────────────────────┤
│  06  Возврат ответа → ЧАТ Б                              │
└─────────────────────────────────────────────────────────┘
```

Два независимых пути данных:

- **Путь записи (Ingest):** ЧАТ А → WE API → Pub/Sub → `/pub-sub-push` → `chat_messages` → фоновые задачи (embeddings + state extraction).
- **Путь чтения (Interaction):** ЧАТ Б → Chat API → `/interactions` → RAG → ответ в ЧАТ Б.

---

## 3. Слой 01 — Источники событий

### ЧАТ А

Google Chat / Workspace-пространство. Мониторим как внешний наблюдатель — от имени реального пользователя (OAuth user auth), у которого есть доступ к пространству.

**Важно при создании пространства:** включить режим **threaded** — иначе ветвление обсуждений не сохраняется. Однако архитектура специально спроектирована так, чтобы не требовать от пользователей дисциплины с тредами (см. [Слой 04+](#7-слой-04--извлечение-состояния-llm)).

### ЧАТ Б

Бот, зарегистрированный как Google Chat App. Принимает прямые обращения от пользователей через личный DM-чат.

---

## 4. Слой 02 — Доставка событий

### Workspace Events API (ЧАТ А)

Позволяет создавать подписки на события пространства. Работает от имени OAuth-пользователя, у которого есть доступ к пространству (не сервисного аккаунта).

Поток:

```
ЧАТ А — новое сообщение
  → WE API публикует событие в Cloud Pub/Sub топик
  → push-подписка отправляет POST на /chat/pub-sub-push
```

**Преимущества Pub/Sub:** буферизация, retry из коробки, защита от пиков нагрузки.

### Google Chat API (ЧАТ Б)

Для бота настраивается конфигурация Chat App: все сообщения, адресованные боту, доставляются на HTTP-эндпоинт через **interactions webhook**.

```
ЧАТ Б — пользователь пишет боту
  → Google Chat API отправляет POST на /chat/interactions
```

---

## 5. Слой 03 — FastAPI сервер

Два эндпоинта с разной логикой обработки.

### POST `/chat/pub-sub-push`

Принимает push-сообщения от Pub/Sub (путь записи).

**Шаги обработки:**

1. Распаковать Pub/Sub envelope (base64-декодирование data).
2. Валидировать JWT от Google (Bearer-токен в заголовке).
3. Распознать тип события WE API (сообщение создано / изменено / удалено).
4. Нормализовать: извлечь `message_id`, `sender`, `space_id`, `thread_id`, `text`, `created_time`.
5. Записать в `chat_messages`.
6. Поставить в очередь две фоновые задачи:
   - **embeddings** — создать вектор и записать в `chat_messages_embeddings`.
   - **state extraction** — прогнать через LLM-пайплайн (пре-фильтр → extraction → resolution).
7. Ответить `200 OK` немедленно (до завершения фоновых задач).

> Pub/Sub ждёт `200 OK` в течение `ackDeadline`. Если не пришёл — повторная доставка. Поэтому сохраняем в БД синхронно, фоновые задачи запускаем async.

### POST `/chat/interactions`

Принимает обращения к боту (путь чтения).

**Шаги обработки:**

1. Разобрать тип события (`MESSAGE`, `CARD_CLICKED`, `REMOVED_FROM_SPACE` и т.д.).
2. Для `MESSAGE`: извлечь `sender.name` (user_id), `space.name` (space_id), `text`.
3. Поднять `conversations` по ключу `(user_id, space_id)`.
4. Запустить RAG: embedding запроса → cosine search → сборка контекста → LLM.
5. Вернуть ответ напрямую в теле ответа на тот же HTTP-запрос (Google Chat ждёт синхронного ответа).

---

## 6. Слой 04 — База данных

PostgreSQL с расширением `pgvector`. Пять таблиц с чёткими ролями.

### Таблица `chat_messages`

Append-only лог событий. **Никогда не изменяется** — источник истины о том, что кто и когда написал.

```sql
CREATE TABLE chat_messages (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id    TEXT NOT NULL UNIQUE,   -- Google message name
    space_id      TEXT NOT NULL,          -- space.name
    thread_id     TEXT,                   -- threadKey или thread.name
    author_id     TEXT NOT NULL,          -- sender.name (users/…)
    author_name   TEXT,
    text          TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL,
    received_at   TIMESTAMPTZ DEFAULT NOW(),
    source        TEXT NOT NULL CHECK (source IN ('chat_a', 'chat_b'))
);

CREATE INDEX ON chat_messages (space_id, created_at DESC);
CREATE INDEX ON chat_messages (thread_id);
CREATE INDEX ON chat_messages (author_id);
```

### Таблица `chat_messages_embeddings`

Векторные представления для cosine-поиска.

```sql
CREATE TABLE chat_messages_embeddings (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    embedding  VECTOR(1536) NOT NULL,   -- размер зависит от модели
    model      TEXT NOT NULL,           -- e.g. text-embedding-3-small
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON chat_messages_embeddings
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
```

### Таблица `vacancies` *(новая, Вопрос 1)*

**Изменяемая** таблица текущего состояния сущностей. Отвечает на вопросы «как есть сейчас».

```sql
CREATE TABLE vacancies (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed', 'on_hold', 'filled')),
    salary_min      INTEGER,
    salary_max      INTEGER,
    currency        TEXT DEFAULT 'RUB',
    owner_id        TEXT,               -- author_id нанимающего менеджера
    owner_name      TEXT,
    team            TEXT,
    description     TEXT,
    last_message_id UUID REFERENCES chat_messages(id),
    embedding       VECTOR(1536),       -- для entity resolution
    confidence      FLOAT,              -- уверенность последнего extraction
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON vacancies (status);
CREATE INDEX ON vacancies USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 50);
```

### Таблица `vacancy_revisions` *(новая, Вопрос 1)*

Append-only журнал всех изменений по вакансиям. Никогда не удаляется.

```sql
CREATE TABLE vacancy_revisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vacancy_id      UUID NOT NULL REFERENCES vacancies(id),
    action          TEXT NOT NULL CHECK (action IN ('create','update','close','pending')),
    changed_field   TEXT,               -- поле, которое изменилось
    old_value       TEXT,
    new_value       TEXT,
    source_message_id UUID REFERENCES chat_messages(id),
    confidence      FLOAT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON vacancy_revisions (vacancy_id, created_at DESC);
CREATE INDEX ON vacancy_revisions (source_message_id);
```

> **Поле `pending`:** при низком `confidence` (< 0.6) запись пишется как `action = 'pending'`, в `vacancies` ничего не меняется. Это позволяет вручную просмотреть сомнительные апдейты, не засоряя текущее состояние.

### Таблица `conversations` *(обновлена, Вопрос 2)*

Память диалога пользователя с ботом. Хранит только выжимку, не полную переписку.

```sql
CREATE TABLE conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,      -- sender.name → users/123456789
    space_id        TEXT NOT NULL,      -- space.name (стабильный 1:1 DM)
    running_summary TEXT,               -- сжатая долговременная память
    recent_turns    JSONB DEFAULT '[]', -- последние 6–10 реплик (capped)
    user_profile    JSONB DEFAULT '{}', -- структурный профиль (опционально)
    turns_count     INTEGER DEFAULT 0,  -- всего реплик в жизни диалога
    summary_updated_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, space_id)
);
```

**Структура `recent_turns`:**

```json
[
  { "role": "user",      "text": "Открыта ли ещё позиция Python?", "ts": "2026-05-22T09:00:00Z" },
  { "role": "assistant", "text": "Да, вакансия открыта, зарплата 340k.",  "ts": "2026-05-22T09:00:02Z" }
]
```

**Структура `user_profile`:**

```json
{
  "roles_of_interest": ["Python backend", "Data Engineer"],
  "team":              "Platform",
  "is_hiring_manager": false
}
```

---

## 7. Слой 04+ — Извлечение состояния (LLM)

> **Архитектурное решение для Вопроса 1.** Заменяет необходимость опираться на треды для понимания «это апдейт той же вакансии».

Запускается фоном после записи каждого сообщения в `chat_messages`. Состоит из пре-фильтра и двух LLM-шагов.

### Пре-фильтр

Перед LLM-вызовами — быстрая и дешёвая проверка: «это вообще про вакансию?». Варианты реализации:

- Лёгкая классификационная модель (distilbert, fasttext).
- Маленькая LLM с коротким промптом (`gpt-4o-mini` / `claude-haiku`): «Это сообщение содержит информацию о вакансии? Ответь yes/no».
- Набор ключевых слов + regex как baseline.

Только при `yes` запускаем дорогие шаги ниже.

### Шаг 1: Extraction

Извлекаем структурированные данные из текста сообщения.

**Промпт (system):**

```
Ты — парсер сообщений о вакансиях в корпоративном чате.
Твоя задача — извлечь структурированную информацию.
Отвечай ТОЛЬКО валидным JSON без markdown-блоков.

Поля action: create | update | close | none
Поля fields: title, salary_min, salary_max, currency, status, owner, team, description
Если поле неизвестно — не включай его в fields.
```

**Промпт (user):**

```
Сообщение от {author_name} ({created_at}):
"{text}"
```

**Ожидаемый JSON-ответ:**

```json
{
  "action": "update",
  "entity_ref": "сеньор питонист в команду инфраструктуры",
  "fields": {
    "salary_min": 320000,
    "salary_max": 360000,
    "currency": "RUB"
  },
  "confidence": 0.91
}
```

| Поле | Описание |
|---|---|
| `action` | `create` — новая вакансия, `update` — изменение, `close` — закрытие, `none` — не про вакансию |
| `entity_ref` | Свободный текстовый идентификатор вакансии (нужен для resolution) |
| `fields` | Только изменяемые поля, без пустых значений |
| `confidence` | 0–1, уверенность модели. При < 0.6 → `pending` в ревизии |

### Шаг 2: Entity Resolution

Ключевой шаг — заменяет треды. Определяет, к **какой именно** вакансии относится сообщение.

**Алгоритм:**

1. Embed `entity_ref` из шага 1.
2. Найти top-5 кандидатов из `vacancies` через cosine similarity (threshold > 0.75).
3. Если кандидаты найдены — спросить LLM: «Это сообщение относится к одной из этих вакансий или это новая?»

**Промпт для resolution:**

```
Тебе дано новое сообщение о вакансии и список существующих вакансий.
Определи: это сообщение обновляет одну из существующих вакансий, или описывает новую?

Новое сообщение: "{entity_ref}"
Поля: {fields}

Существующие вакансии:
{кандидаты — title, team, owner, salary, status}

Ответь JSON: { "match": "ID вакансии или null", "reason": "краткое объяснение" }
```

**Логика после resolution:**

```python
if result.match and confidence >= 0.6:
    # Обновить строку в vacancies
    UPDATE vacancies SET ... WHERE id = result.match
    # Записать в журнал
    INSERT INTO vacancy_revisions (action='update', vacancy_id=result.match, ...)

elif result.match is None and action == 'create':
    # Создать новую вакансию
    INSERT INTO vacancies (...)
    INSERT INTO vacancy_revisions (action='create', ...)

elif confidence < 0.6:
    # Не уверены — записываем как pending, не трогаем vacancies
    INSERT INTO vacancy_revisions (action='pending', ...)
```

---

## 8. Слой 05 — RAG

### embeddings creation

Для каждого нового сообщения из `chat_messages` фоновой задачей:

1. Передать `text` в embedding-модель (например, `text-embedding-3-small`).
2. Записать вектор в `chat_messages_embeddings`.

Запускается параллельно с state extraction, независимо.

### searching

При запросе к боту на `/chat/interactions`:

1. Embed запрос пользователя.
2. Cosine-поиск top-K релевантных сообщений из `chat_messages_embeddings`.
3. Собрать контекст из трёх источников:

| Источник | Что даёт | Таблица |
|---|---|---|
| Cosine-поиск | Релевантные сообщения из истории | `chat_messages_embeddings` |
| Текущее состояние | Актуальный статус/зарплата/овнер вакансии | `vacancies` |
| Память диалога | Связность диалога, профиль пользователя | `conversations` |

**Важно:** на вопросы «открыта ли / какая сейчас зарплата» отвечаем **из `vacancies`**, не из cosine-поиска по логу. Лог отвечает на «что обсуждалось», `vacancies` — на «как есть сейчас».

**Сборка контекста для LLM:**

```python
context = f"""
=== Память диалога ===
{conversation.running_summary or 'Первый диалог'}

Последние реплики:
{format_turns(conversation.recent_turns)}

=== Актуальное состояние вакансий (если релевантно) ===
{format_vacancies(relevant_vacancies)}

=== Релевантные сообщения из пространства ===
{format_messages(top_k_messages)}
"""
```

### answer generation

LLM генерирует ответ на основе собранного контекста.

После генерации:

1. Дописать новую реплику в `conversations.recent_turns`.
2. Если `len(recent_turns) > MAX_TURNS` (например, 10) — запустить фоновую свёртку.

**Свёртка `recent_turns` → `running_summary`:**

```python
# Фоновая задача
old_turns = recent_turns[:-KEEP_LAST]   # всё кроме последних N реплик
new_summary = llm.summarize(
    existing_summary=running_summary,
    turns_to_fold=old_turns
)
UPDATE conversations SET
    running_summary = new_summary,
    recent_turns    = recent_turns[-KEEP_LAST:],
    summary_updated_at = NOW()
WHERE id = ...
```

---

## 9. Слой 06 — Возврат ответа

Готовый ответ возвращается синхронно в теле HTTP-ответа на запрос `/chat/interactions`. Google Chat отображает его в том же треде, где пользователь задал вопрос.

```json
{
  "text": "Да, вакансия Python-разработчика в команде Platform открыта. Зарплата 320–360k RUB, нанимает Иван Петров. Последнее обновление — вчера."
}
```

Цикл замкнут: ЧАТ А → (инжест, фоновые задачи) → БД → (запрос пользователя) → RAG → ЧАТ Б.

---

## 10. Ключевые архитектурные решения

### Решение Q1: Модель состояния (без тредов)

**Проблема.** Поток сообщений в ЧАТ А — это лог событий (`chat_messages`, append-only). Но пользователь бота спрашивает про **текущее состояние**: открыта ли вакансия? какая теперь зарплата? кто отвечает? Сообщение «платим 350k» через час после «платим 320k» — это апдейт той же сущности, но треды для этого не нужны и неудобны.

**Решение.** Рядом с логом держать изменяемую таблицу `vacancies`. LLM самостоятельно связывает новые сообщения с существующими вакансиями по смыслу (entity resolution), без опоры на треды.

**Преимущества:**
- Пользователи пишут как обычно, никакой дисциплины не требуется.
- Вопрос «актуально ли» всегда читается из одной строки `vacancies`, а не из разрозненного лога.
- Полная история изменений сохраняется в `vacancy_revisions`.
- При неуверенности (confidence < 0.6) система не угадывает — пишет `pending`.

**Стоимость:** +2 таблицы, фоновый LLM-вызов на «интересные» сообщения. Пре-фильтр существенно снижает количество дорогих вызовов.

---

### Решение Q2: Память диалога — только саммари

**Проблема.** Хранить полную переписку каждого пользователя с ботом — дорого (БД растёт линейно), и не нужно: LLM не может использовать контекст длиной в сотни реплик.

**Решение.** Две «памяти» разного уровня:

| Уровень | Что хранит | Как меняется | Retention |
|---|---|---|---|
| `recent_turns` | Последние 6–10 реплик | Capped, старое вытесняется | Эфемерно |
| `running_summary` | Сжатая выжимка всего прошлого | Обновляется при свёртке | Постоянно |
| `user_profile` | Структурные факты о пользователе | LLM обновляет при апдейте | Постоянно |

**Ключ диалога:** `UNIQUE(user_id, space_id)`, где:
- `user_id` = `message.sender.name` из payload Google Chat (вид `users/123456789`, стабильный).
- `space_id` = `space.name` (личный DM-чат с ботом, один на пользователя, стабильный).

**Важно:** память диалога (`conversations`) изолирована от общего RAG по ЧАТ А. В промпт для ответа они подмешиваются из разных источников — не смешиваются в одной таблице.

---

## 11. Потоки данных

### Путь записи — новое сообщение в ЧАТ А

```
1. Пользователь пишет в ЧАТ А
2. WE API → Pub/Sub топик (новое событие)
3. Push-подписка → POST /chat/pub-sub-push
4. Валидация JWT, распаковка события
5. INSERT INTO chat_messages
6. Ответ 200 OK (немедленно)
7. [async] embed message → INSERT INTO chat_messages_embeddings
8. [async] пре-фильтр → extraction (LLM) → resolution (LLM)
             → UPSERT vacancies + INSERT vacancy_revisions
```

### Путь чтения — пользователь спрашивает бота

```
1. Пользователь пишет боту в ЧАТ Б
2. Google Chat API → POST /chat/interactions
3. Извлечь user_id, space_id, text
4. SELECT conversations WHERE user_id=… AND space_id=…
5. Embed запрос
6. Cosine-поиск top-K по chat_messages_embeddings
7. SELECT vacancies WHERE (релевантные по контексту)
8. Собрать контекст: summary + recent_turns + vacancies + top-K сообщений
9. LLM → ответ
10. Обновить conversations (recent_turns, при необходимости — свёртка)
11. Вернуть ответ в теле HTTP-ответа → пользователь видит в ЧАТ Б
```

---

## 12. Нерешённые вопросы и альтернативы

### Контекст треда (альтернативы для entity resolution)

Четыре подхода к пониманию связи между сообщениями — от простого к сложному:

**Вариант 1 — Временной приоритет.** Добавить дату к каждому сообщению в контексте и инструктировать LLM «последнее важнее». Нет дополнительного кода. Работает для ~80% случаев, когда связь очевидна из контекста.

**Вариант 2 — Весь тред целиком.** Когда cosine-поиск нашёл сообщение, подтянуть все сообщения с тем же `thread_id`. LLM видит полный диалог. Точно, но дорого по токенам — особенно для длинных тредов.

**Вариант 3 — Кэш саммари тредов.** Длинные треды суммаризировать один раз и кэшировать в отдельной таблице `thread_summaries`. Обновлять только при новых сообщениях. Хороший баланс цены и качества. Требует +1 таблица и фоновая задача.

**Вариант 4 — Дневные треды по расписанию.** Бот каждое утро создаёт тред «📅 Обсуждения за {дату}». Требует дисциплины от команды, но нет сложного кода обработки.

> Текущая архитектура использует **entity resolution (слой 04+)**, что снимает необходимость в вариантах 1–4 для задачи «отследить апдейт вакансии». Варианты 2–3 остаются актуальными для случаев, когда нужна **история дискуссии** (не только текущее состояние).

### Другие открытые вопросы

**Идемпотентность при повторной доставке.** Pub/Sub гарантирует at-least-once: одно сообщение может прийти дважды. Нужна дедупликация по `message_id` в `chat_messages` (уже есть `UNIQUE(message_id)`) и в фоновых задачах.

**Удаление и редактирование сообщений.** WE API отправляет события `MESSAGE_DELETED` и `MESSAGE_UPDATED`. Нужно решить: мягкое удаление в логе (`is_deleted`), пересчёт embedding при редактировании, пересчёт state extraction.

**Масштабирование фоновых задач.** При высокой нагрузке очередь задач (embeddings + extraction) может отставать. Рассмотреть Celery / Cloud Tasks / Pub/Sub для асинхронной обработки вместо in-process background tasks FastAPI.

**Rate limits LLM.** Extraction запускается на каждое «интересное» сообщение. При активном пространстве — риск упереться в rate limits. Батчинг или очередь с throttle.

**Приватность диалогов.** `conversations` с памятью пользователя — чувствительные данные. Изолировать от общего RAG, настроить row-level security или шифрование на уровне колонок.
