# Google Chat Bot — Гайд по развёртыванию

## Архитектура системы

```
┌─────────────────────────────────────────────────────────────┐
│                    GOOGLE WORKSPACE                          │
│                                                             │
│   Чат А (Monitored chat)        Чат Б (диалог с ботом)     │
│   spaces/AAQAmmGOtCo            DM с ChatMonitorBot2        │
│        │                               │                    │
│        │ все сообщения                 │ @упоминание/DM     │
│        ▼                               ▼                    │
│   Workspace Events API          Google Chat HTTP            │
│   subscription                  interaction event           │
│        │                               │                    │
│        ▼                               │                    │
│   Cloud Pub/Sub topic                  │                    │
│   chat-events-topic                    │                    │
└────────┼───────────────────────────────┼────────────────────┘
         │ push                          │ POST + JWT
         ▼                               ▼
┌─────────────────────────────────────────────────────────────┐
│              FastAPI (Python) — localhost:8000               │
│                                                             │
│   POST /chat/pubsub-push      POST /chat/interaction        │
│   (мониторинг Чата А)         (диалог в Чате Б)            │
│        │                               │                    │
│        │ UPSERT                        │ RAG поиск          │
│        ▼                               ▼                    │
│              PostgreSQL 16 + pgvector                       │
│         chat_messages | message_embeddings | conversations  │
└─────────────────────────────────────────────────────────────┘
         ▲
    ngrok туннель
    (для локальной разработки)
```

---

## Схема подписки на Чат А (логика и взаимосвязь)

Вот полная цепочка того, как настраивается мониторинг Чата А — от OAuth до получения событий на эндпоинт:

```
┌─────────────────────────────────────────────────────────────────────┐
│  ШАГ 1: OAuth (один раз, вручную)                                   │
│                                                                     │
│  oauth-credentials.json                                             │
│  (скачан из GCP → Google Auth Platform)                             │
│          │                                                          │
│          │ запускаем create_subscription.py                         │
│          ▼                                                          │
│  Браузер открывается → логинимся → Google даёт токен               │
│          │                                                          │
│          ▼                                                          │
│       token.json  ← сохраняется на диск для повторных запусков     │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ используем токен
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ШАГ 2: Находим нужное пространство (Chat API)                      │
│                                                                     │
│  GET https://chat.googleapis.com/v1/spaces                          │
│                                                                     │
│  Ответ: список всех spaces где есть наш аккаунт                     │
│  [                                                                  │
│    { "name": "spaces/AAQAmmGOtCo", "displayName": "Чат А" },       │
│    { "name": "spaces/XYZ...",      "displayName": "Другой чат" },  │
│    ...                                                              │
│  ]                                                                  │
│                                                                     │
│  Берём нужный: spaces/AAQAmmGOtCo  ← это и есть ID Чата А          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ используем этот ID
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ШАГ 3: Pub/Sub topic (создаётся один раз вручную в GCP консоли)   │
│                                                                     │
│  GCP → Pub/Sub → Topics → Create Topic                              │
│  Topic ID: chat-events-topic                                        │
│                                                                     │
│  + Дать права chat-api-push@system.gserviceaccount.com             │
│    роль: Pub/Sub Publisher                                          │
│    (чтобы Google Chat мог публиковать в топик)                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ topic готов
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ШАГ 4: Workspace Events subscription (через Python + OAuth)        │
│                                                                     │
│  POST https://workspaceevents.googleapis.com/v1/subscriptions       │
│  {                                                                  │
│    targetResource: "//chat.googleapis.com/spaces/AAQAmmGOtCo",     │
│    eventTypes: [message.created, message.updated, ...],            │
│    notificationEndpoint: {                                          │
│      pubsubTopic: "projects/.../topics/chat-events-topic"          │
│    }                                                                │
│  }                                                                  │
│                                                                     │
│  Эффект: Google Chat начинает отправлять все события из             │
│  Чата А в наш Pub/Sub топик                                         │
│                                                                     │
│  ⚠️ Живёт только 4 часа (при includeResource=true)                  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ события летят в топик
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ШАГ 5: Pub/Sub push subscription (создаётся один раз в GCP)       │
│                                                                     │
│  GCP → Pub/Sub → Topics → chat-events-topic → Create Subscription  │
│  Delivery type: Push                                                │
│  Endpoint URL: https://ВАШ_NGROK/chat/pubsub-push                  │
│                                                                     │
│  Эффект: Pub/Sub берёт каждое сообщение из топика и делает         │
│  HTTP POST на наш эндпоинт                                          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ HTTP POST с Base64 payload
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  FastAPI: POST /chat/pubsub-push                                    │
│                                                                     │
│  Принимаем envelope → декодируем Base64 → получаем событие чата     │
│  Возвращаем 204 → Pub/Sub считает доставку успешной                 │
└─────────────────────────────────────────────────────────────────────┘
```

**Итого — три отдельные сущности:**

| Сущность | Что делает | Живёт |
|---|---|---|
| **OAuth token** | Авторизует Python-скрипт в Google API | До отзыва |
| **Workspace Events subscription** | Говорит Google Chat «шли события в топик» | 4 часа, надо обновлять |
| **Pub/Sub push subscription** | Говорит Pub/Sub «шли сообщения из топика на эндпоинт» | Постоянно |

---

## Компоненты системы

| Компонент | Назначение | Где |
|---|---|---|
| Google Chat App | Бот который принимает сообщения | GCP → Google Chat API |
| Workspace Events API | Подписка на все сообщения Чата А | GCP → subscription через Python |
| Cloud Pub/Sub | Очередь событий из Чата А | GCP → Pub/Sub |
| FastAPI | Сервер-мозг бота | Локально / сервер |
| PostgreSQL + pgvector | БД для сообщений и векторов | Docker |
| ngrok | Туннель для локальной разработки | Локально |
| OpenAI | Эмбеддинги + ответы (RAG) | API |

---

## Шаг 1 — GCP проект

### 1.1 Создать проект
- Зайти на `console.cloud.google.com`
- Вверху → выбрать проект → **New Project**
- Назвать проект

### 1.2 Включить APIs
Искать через поиск вверху и включать:
- `Google Chat API`
- `Cloud Pub/Sub API`
- `Google Workspace Events API`

### 1.3 Создать Pub/Sub topic
- GCP → **Pub/Sub → Topics → Create Topic**
- Topic ID: `chat-events-topic`
- Остальное по умолчанию → **Create**

### 1.4 Дать Google Chat право публиковать в topic
- GCP → **Pub/Sub → Topics → chat-events-topic → Permissions**
- **Add Principal**:
  - New principal: `chat-api-push@system.gserviceaccount.com`
  - Role: `Pub/Sub Publisher`
- **Save**

---

## Шаг 2 — Google Chat App (Бот Б)

### 2.1 Создать Chat App
- GCP → **Google Chat API → Configuration**
- Заполнить:
  - **App name**: `ChatMonitorBot2`
  - **Avatar URL**: `https://www.gstatic.com/images/branding/product/2x/chat_96dp.png`
  - **Description**: произвольно

### 2.2 Настроить Functionality
Поставить галочки:
- ✅ Receive 1:1 messages
- ✅ Join spaces and group conversations

### 2.3 Connection settings
- Выбрать **HTTP endpoint URL**
- Triggers: **Use a common HTTP endpoint URL for all triggers**
- URL: `https://ВАШ_NGROK_URL/chat/interaction`

> ⚠️ **НИКОГДА не ставить галочку "Build this Chat app as a Workspace add-on"** — это необратимое действие которое сломает бота

### 2.4 Visibility
- Поставить галочку видимости
- Добавить свой email в поле

### 2.5 App status
- Поставить **LIVE - available to users**

---

## Шаг 3 — OAuth для подписки на Чат А

### 3.1 Настроить OAuth consent screen
- GCP → **APIs & Services → OAuth (Google Auth Platform)**
- **Create OAuth client** → Audience: **External**
- Заполнить App name, support email, developer email
- В **Test users** добавить свой email
- Сохранить

### 3.2 Создать OAuth клиент
- GCP → **Google Auth Platform → Clients → Create Client**
- Application type: **Desktop app**
- Name: `chat-monitor-oauth`
- Скачать JSON → переименовать в `oauth-credentials.json`
- Положить в папку проекта

---

## Шаг 4 — Локальная инфраструктура

### 4.1 Создать папку проекта и venv
```powershell
mkdir chatbot
cd chatbot
python -m venv venv
venv\Scripts\activate
```

### 4.2 Установить зависимости
```powershell
pip install fastapi uvicorn asyncpg sqlalchemy[asyncio] python-dotenv google-auth google-auth-oauthlib google-api-python-client openai anthropic httpx
```

### 4.3 Поднять PostgreSQL с pgvector в Docker
```powershell
docker run -d --name chatbot-pg -e POSTGRES_PASSWORD=password -e POSTGRES_DB=chatbot -p 5432:5432 pgvector/pgvector:pg16
```

### 4.4 Создать таблицы в БД
```powershell
docker exec -it chatbot-pg psql -U postgres -d chatbot
```

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE chat_messages (
    name              TEXT PRIMARY KEY,
    space_name        TEXT NOT NULL,
    space_display     TEXT,
    sender_name       TEXT NOT NULL,
    sender_display    TEXT,
    sender_email      TEXT,
    text              TEXT,
    create_time       TIMESTAMPTZ NOT NULL,
    last_update_time  TIMESTAMPTZ,
    delete_time       TIMESTAMPTZ,
    raw_event         JSONB NOT NULL,
    ingested_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE message_embeddings (
    id            BIGSERIAL PRIMARY KEY,
    message_name  TEXT NOT NULL REFERENCES chat_messages(name) ON DELETE CASCADE,
    chunk_text    TEXT NOT NULL,
    embedding     VECTOR(1536) NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE conversations (
    id           BIGSERIAL PRIMARY KEY,
    space_name   TEXT NOT NULL,
    user_name    TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now()
);
```

Выйти: `\q`

### 4.5 Создать .env файл
```env
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/chatbot
OPENAI_API_KEY=твой_ключ
GOOGLE_SERVICE_ACCOUNT=service-account.json
```

---

## Шаг 5 — ngrok

### 5.1 Установить
- Скачать exe с `ngrok.com/download` → Windows (64-bit)
- Распаковать в `C:\ngrok\`
- Зарегистрироваться на `ngrok.com` → скопировать Authtoken

### 5.2 Настроить и запустить
```powershell
ngrok config add-authtoken ТВОЙ_ТОКЕН
ngrok http 8000
```

Скопировать URL вида `https://xxxx.ngrok-free.app` — вставить в Chat App Configuration.

> ⚠️ ngrok даёт новый URL при каждом перезапуске — нужно обновлять в GCP Chat Configuration

---

## Шаг 6 — FastAPI сервер

### 6.1 Создать main.py
```python
from fastapi import FastAPI, Request, Response
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
import os, base64, json

load_dotenv()

engine = create_async_engine(os.getenv("DATABASE_URL"))
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()

app = FastAPI(lifespan=lifespan)

@app.post("/chat/interaction")
async def chat_interaction(request: Request):
    try:
        event = await request.json()
    except Exception:
        return {}

    event_type = event.get("type")
    user = event.get("user", {}).get("displayName", "")
    question = event.get("message", {}).get("text", "")

    if event_type == "ADDED_TO_SPACE":
        return {"text": f"Привет, {user}! Я бот-ассистент."}

    if event_type == "MESSAGE":
        return {"text": f"Привет, {user}! Ты написал: {question}"}

    return {}

@app.post("/chat/pubsub-push")
async def pubsub_push(request: Request):
    try:
        envelope = await request.json()
    except Exception:
        return Response(status_code=204)

    msg = envelope.get("message", {})
    data_b64 = msg.get("data", "")

    if data_b64:
        try:
            data = json.loads(base64.b64decode(data_b64))
            print("=== PUBSUB EVENT ===")
            print(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"Decode error: {e}")

    return Response(status_code=204)

@app.get("/health")
async def health():
    return {"status": "ok"}
```

### 6.2 Запустить сервер
```powershell
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Шаг 7 — Создать подписку на Чат А

### 7.1 Создать create_subscription.py
```python
import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
]

SPACE_NAME = "spaces/AAQAmmGOtCo"  # ID вашего Чата А
PUBSUB_TOPIC = "projects/ВАШ_PROJECT_ID/topics/chat-events-topic"

def main():
    # Первый запуск — авторизация через браузер
    # Последующие — используем сохранённый token.json
    try:
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    except:
        flow = InstalledAppFlow.from_client_secrets_file("oauth-credentials.json", SCOPES)
        creds = flow.run_local_server(port=8080)
        with open("token.json", "w") as f:
            f.write(creds.to_json())

    svc = build("workspaceevents", "v1", credentials=creds)

    body = {
        "targetResource": f"//chat.googleapis.com/{SPACE_NAME}",
        "eventTypes": [
            "google.workspace.chat.message.v1.created",
            "google.workspace.chat.message.v1.updated",
            "google.workspace.chat.message.v1.deleted",
        ],
        "notificationEndpoint": {
            "pubsubTopic": PUBSUB_TOPIC
        },
        "payloadOptions": {
            "includeResource": True
        }
    }

    print("Создаём подписку...")
    result = svc.subscriptions().create(body=body).execute()
    print("Подписка создана!")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
```

### 7.2 Запустить
```powershell
python create_subscription.py
```

> ⚠️ Подписка живёт **4 часа** (при includeResource=true). Нужно продлевать или пересоздавать.

### 7.3 Создать Pub/Sub push subscription
- GCP → **Pub/Sub → Topics → chat-events-topic → Create Subscription**
- Subscription ID: `chat-push-sub`
- Delivery type: **Push**
- Endpoint URL: `https://ВАШ_NGROK_URL/chat/pubsub-push`
- **Create**

---

## Шаг 8 — Порядок запуска каждый раз

Каждый раз когда садишься работать — запускать в таком порядке:

```
1. Запустить Docker (если не запущен)
   docker start chatbot-pg

2. Активировать venv
   cd chatbot
   venv\Scripts\activate

3. Запустить ngrok (в отдельном окне)
   ngrok http 8000

4. Если ngrok дал новый URL — обновить в GCP Chat Configuration

5. Запустить FastAPI
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload

6. Если подписка истекла (раз в 4 часа) — пересоздать
   python create_subscription.py
```

---

## Важные предупреждения

| ⚠️ Проблема | Решение |
|---|---|
| Галочка "Workspace add-on" | НИКОГДА не ставить — необратимо |
| ngrok новый URL | Обновить в GCP Chat Configuration |
| Подписка истекла через 4ч | Запустить create_subscription.py заново |
| Chat API недоступен | Нужен Google Workspace аккаунт, не личный Gmail |
| Бот не находится в пространстве | Добавить через @упоминание или изменить Visibility на весь домен |

---

## Текущее состояние проекта

```
✅ GCP проект: vacanciesbot-496815
✅ Chat App: ChatMonitorBot2
✅ Pub/Sub topic: chat-events-topic
✅ OAuth credentials: oauth-credentials.json
✅ PostgreSQL + pgvector запущен в Docker
✅ FastAPI: /chat/interaction + /chat/pubsub-push
✅ Подписка на Чат А: spaces/AAQAmmGOtCo
✅ Сообщения из Чата А приходят в FastAPI

⏳ Сохранение сообщений в PostgreSQL
⏳ Генерация эмбеддингов (OpenAI)
⏳ RAG pipeline (поиск по БД + ответы)
⏳ Продление подписки (авто каждые 3ч)
```
