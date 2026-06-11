# Batch-режим v2 — план доработки обработчика сообщений

> Документ описывает полный план переработки `PROCESSING_MODE=batch`: каталог проблем текущей
> реализации, девять этапов изменений с кодом и SQL, порядок релизов, тестовые сценарии и
> инварианты, которые система обязана соблюдать после доработки.
>
> Базовый документ: `batch_mode.md` (текущая реализация). Здесь описывается **дельта** к нему.

---

## Содержание

1. [Цели и принципы](#1-цели-и-принципы)
2. [Каталог проблем текущей реализации](#2-каталог-проблем-текущей-реализации)
3. [Этап 0 — миграция: фундамент в данных](#3-этап-0--миграция-фундамент-в-данных)
4. [Этап 1 — условный mark_processed](#4-этап-1--условный-mark_processed)
5. [Этап 2 — last-write-wins по времени сообщения](#5-этап-2--last-write-wins-по-времени-сообщения)
6. [Этап 3 — replay: единый механизм пересборки поля](#6-этап-3--replay-единый-механизм-пересборки-поля)
7. [Этап 4 — целостность пачки](#7-этап-4--целостность-пачки)
8. [Этап 5 — точность связывания](#8-этап-5--точность-связывания)
9. [Этап 6 — защита от ядовитой пачки](#9-этап-6--защита-от-ядовитой-пачки)
10. [Этап 7 — триггер флаша и блокировки](#10-этап-7--триггер-флаша-и-блокировки)
11. [Этап 8 — онлайн-ветка: страховка порядка](#11-этап-8--онлайн-ветка-страховка-порядка)
12. [Этап 9 — наблюдаемость](#12-этап-9--наблюдаемость)
13. [Порядок релизов и зависимости](#13-порядок-релизов-и-зависимости)
14. [Инварианты системы](#14-инварианты-системы)
15. [Тестовые сценарии](#15-тестовые-сценарии)

---

## 1. Цели и принципы

Batch-режим существует ради одного: **точность связывания follow-up'ов** должна быть выше,
чем в `per_message`. Всё остальное — обслуживание этой цели. Доработка строится на четырёх
принципах:

**P1 — Данные не теряются и не искажаются.** Правка, удаление или повторная доставка в любой
момент времени (включая середину флаша) не должны приводить к потере изменений, вакансиям от
удалённых сообщений или дублям.

**P2 — Результат не зависит от порядка обработки.** У системы два пути с разной задержкой
(онлайн-ветка — секунды, пачка — минуты) плюс Pub/Sub без гарантии порядка. Итоговое состояние
вакансии должно определяться **хронологией чата**, а не тем, какая ветка успела первой.

**P3 — Контекст, найденный LLM, не выбрасывается.** Если модель в пачке поняла связь между
сообщениями — эта связь должна дойти до записи в БД напрямую, а не переоткрываться заново
эмбеддинг-поиском.

**P4 — Любой сбой деградирует видимо и ограниченно.** Ядовитое сообщение не должно вечно
блокировать очередь space; частичный сбой пачки не должен ронять успешные items; любое
залипание должно быть видно в метриках до того, как его заметят пользователи.

---

## 2. Каталог проблем текущей реализации

Нумерация проблем (B1–B13) используется дальше по тексту — каждый этап указывает, какие
проблемы он закрывает.

### Критичные гонки (потеря и искажение данных)

**B1 — Правка во время флаша теряется.**
`_fetch_pending` забрал сообщение в пачку → пока идёт LLM-вызов (секунды), приходит `updated` →
`handle_edit_batch` видит `is_processed = false` и по протоколу только обновляет текст →
флаш завершается и помечает `is_processed = true`. Итог: экстракция прошла по **старому**
тексту, правка молча проглочена, re-extract не случится никогда — для системы сообщение
«обработано».

**B2 — Удаление во время флаша создаёт вакансию-зомби.**
Сообщение в пачке, идёт LLM-вызов → приходит `deleted` → `handle_delete` ставит
`is_deleted = true` и ищет ревизии для отката — их **ещё нет** (флаш не дошёл до
`_apply_items`) → handle_delete завершается «нечего откатывать» → флаш применяет результат
удалённого сообщения. Вакансия от несуществующего сообщения живёт вечно: повторного события
`deleted` не будет.

**B3 — Онлайн-ветка ломает хронологию.**
Сообщение M1 (10:00, «подняли до 300») без структурной привязки ждёт пачки. M2 (10:01,
thread-reply «нет, до 350») имеет якорь → обрабатывается онлайн немедленно →
`salary_max = 350`. В 10:04 флашится пачка с M1 → `salary_max = 300`. Более **старое**
сообщение перезатёрло более **новое**, потому что порядок применения не совпал с хронологией.
Та же проблема возникает при out-of-order доставке Pub/Sub.

**B4 — Правка обработанного сообщения молча выбрасывается ключом идемпотентности.**
Сообщение дало ревизию `(source_message_id, action='update')`. Его отредактировали →
re-extract дал снова `update`, но с другими полями → `ON CONFLICT DO NOTHING` по ключу
`(source_message_id, action)` **молча отбрасывает** новую ревизию. В зависимости от того,
связаны ли insert ревизии и UPDATE вакансии, либо вакансия обновится без аудита, либо не
обновится вообще. Механизм, защищающий от дублей при retry, ломает легитимные правки.

**B5 — Нет дедупликации на приёме.**
Pub/Sub — at-least-once. Без уникального индекса на `chat_messages.message_id` и
`ON CONFLICT` в `persist_message` повторная доставка создаёт вторую строку → в пачку попадают
два одинаковых сообщения → возможны дублирующие create.

### Потеря контекста на границах пачки

**B6 — `LIMIT BATCH_SIZE` режет треды.**
В очереди 130 сообщений → fetch берёт первые 100 → follow-up на позиции 101 теряет родителя
с позиции 40. Связь, ради которой строился batch-режим, рвётся механически.

**B7 — Пачка не видит недавнюю историю.**
Контекст LLM = список открытых вакансий + необработанные сообщения. Follow-up «по нему подняли
до 400», чей оригинал ушёл в предыдущую пачку, резолвится эмбеддингом `entity_ref` с порогом
0.75 — слабость per_message вернулась через чёрный ход на каждой границе пачек.

**B8 — Связи внутри пачки выбрасываются на этапе resolve.**
LLM поняла: [2] — update вакансии, созданной [0]. Но `_apply_items` гонит каждый item через
**независимый** `resolve_and_save` с эмбеддинг-поиском. Свежесозданная из [0] вакансия может
не пройти порог 0.75 (эмбеддинг короткого entity_ref против только что записанного title) —
update улетает в `pending` или не туда. Главная ценность пачки теряется на последней миле.

**B9 — Потерянный LLM индекс неотличим от осознанного "none".**
Контракт «action=none не включается в ответ» означает: если модель **забыла** про сообщение,
оно молча помечается processed. Сообщение о вакансии исчезает без следа и без ошибки.

### Надёжность и эксплуатация

**B10 — Ядовитая пачка зацикливается навечно.**
Одно сообщение стабильно ломает JSON-ответ → `extract_batch` возвращает `[]` → пачка остаётся
pending → следующий тик → тот же сбой. Вечный цикл: токены горят каждые 30 секунд, очередь
space встала, в логах — ничего критического.

**B11 — Исключение на одном item роняет всю пачку.**
`resolve_and_save` упал на item 5 из 20 → исключение → `_mark_processed` не вызван → **вся**
пачка ретраится: лишний LLM-вызов, повторное применение items 1–4 (идемпотентность спасает от
дублей ревизий, но не от лишней работы и не от расхождений при недетерминированном втором
ответе LLM).

**B12 — Глобальный `asyncio.Lock` сериализует все space'ы.**
Медленный флаш одного чата блокирует флаши всех остальных. Плюс при ожидании (а не пропуске)
тикер копит очередь корутин на залипшем space.

**B13 — Таймаут-флаш режет живой разговор.**
Триггер `count OR max-age` флашит по таймеру посреди переписки: follow-up через 15 секунд
после флаша попадает в следующую пачку и теряет соседей. Пачки нарезаются по таймеру, а не по
естественным границам разговора.

### Карта закрытия

| Проблема | Закрывается этапом |
|----------|--------------------|
| B1, B2 | Этап 1 (+ Этап 3 герметизирует) |
| B3 | Этап 2 |
| B4 | Этапы 0 + 3 |
| B5 | Этап 0 |
| B6 | Этап 4 |
| B7 | Этап 5.1 |
| B8 | Этап 5.2 |
| B9 | Этап 4.3 |
| B10 | Этап 6 |
| B11 | Этап 3 (транзакция на item) |
| B12 | Этап 7.2 |
| B13 | Этап 7.1 |

---

## 3. Этап 0 — миграция: фундамент в данных

**Закрывает:** B5 целиком, создаёт поля для этапов 1, 2, 3, 6.
**Файлы:** `alembic/versions/0004_batch_v2.py`, `app/services/pubsub_persist.py`.

Одна миграция, всё сразу — катать поля по одному дороже.

### 3.1. chat_messages

```sql
ALTER TABLE chat_messages
    ADD COLUMN text_hash      text,
    ADD COLUMN flush_attempts int  NOT NULL DEFAULT 0,
    ADD COLUMN claimed_at     timestamptz,
    ADD COLUMN process_status text NOT NULL DEFAULT 'pending';
    -- 'pending' | 'processed' | 'failed'

-- бэкфилл: вся текущая история считается обработанной, hash — от текущего текста
UPDATE chat_messages
SET process_status = CASE WHEN is_processed THEN 'processed' ELSE 'pending' END,
    text_hash      = encode(sha256(convert_to(coalesce(text, ''), 'UTF8')), 'hex');

-- дедупликация на входе: Pub/Sub at-least-once
CREATE UNIQUE INDEX uq_chat_messages_message_id
    ON chat_messages (message_id);

-- пересоздание partial-индекса под новый статус
DROP INDEX IF EXISTS ix_chat_messages_unprocessed;
CREATE INDEX ix_chat_messages_pending
    ON chat_messages (space_id, created_at)
    WHERE process_status = 'pending' AND is_deleted = false;
```

Семантика полей:

| Поле | Назначение |
|------|------------|
| `text_hash` | sha256 от `text`; обновляется **всегда вместе** с `text` (persist и handle_edit). Версия содержимого для условного mark_processed (этап 1) и идемпотентного ключа ревизий (3.3) |
| `flush_attempts` | счётчик неудачных флашей этого сообщения; драйвер бисекции и dead-letter (этап 6) |
| `claimed_at` | задел под мультиворкер (этап 7.3); до него — всегда NULL, логика его игнорирует |
| `process_status` | замена голому `is_processed`: добавляется терминальное состояние `failed` |

`is_processed` оставить на переходный период и обновлять синхронно с `process_status`
(совместимость со старым кодом), удалить отдельной миграцией после стабилизации.

### 3.2. vacancy_revisions

```sql
ALTER TABLE vacancy_revisions
    ADD COLUMN source_created_at timestamptz,  -- created_at сообщения-источника (LWW, этап 2)
    ADD COLUMN source_text_hash  text,          -- text_hash источника на момент экстракции
    ADD COLUMN applied           bool NOT NULL DEFAULT true,   -- реально изменила поле вакансии
    ADD COLUMN is_superseded     bool NOT NULL DEFAULT false,  -- аннулирована (правка/удаление)
    ADD COLUMN batch_id          uuid;          -- какой флаш породил (этап 9, отладка)

-- бэкфилл source_created_at из chat_messages
UPDATE vacancy_revisions r
SET source_created_at = m.created_at,
    source_text_hash  = m.text_hash
FROM chat_messages m
WHERE m.message_id = r.source_message_id
  AND r.source_message_id IS NOT NULL;

-- новый идемпотентный ключ: учитывает ВЕРСИЮ текста (закрывает B4)
DROP INDEX IF EXISTS uq_vacancy_revisions_source_action;
CREATE UNIQUE INDEX uq_vacancy_revisions_source_action_hash
    ON vacancy_revisions (source_message_id, action, source_text_hash)
    WHERE source_message_id IS NOT NULL;

-- индекс под replay (этап 3): выборка живой цепочки поля
CREATE INDEX ix_vacancy_revisions_field_chain
    ON vacancy_revisions (vacancy_id, changed_field, source_created_at)
    WHERE is_superseded = false AND changed_field IS NOT NULL;
```

Смысл нового ключа: retry того же флаша даёт тот же `source_text_hash` → дубль ловится,
как раньше. Re-extract после правки даёт **новый** hash → ревизия легитимно вставляется.
Конфликт «идемпотентность против правок» (B4) снят на уровне схемы.

### 3.3. Дедупликация в persist_message

```python
# app/services/pubsub_persist.py
async def persist_message(incoming: IncomingMessage) -> bool:
    """Возвращает True, если строка реально вставлена (первая доставка)."""
    stmt = (
        insert(ChatMessage)
        .values(
            message_id=incoming.message_id,
            text=incoming.text,
            text_hash=sha256_hex(incoming.text),
            process_status="pending",
            # ... остальные поля как раньше
        )
        .on_conflict_do_nothing(index_elements=["message_id"])
        .returning(ChatMessage.id)
    )
    row = (await session.execute(stmt)).first()
    await session.commit()
    return row is not None
```

И в endpoint'е:

```python
inserted = await persist_message(incoming)
if inserted:
    background_tasks.add_task(route_created_batch, incoming)
# повторная доставка: 200 без фоновой задачи — событие уже в системе
return Response(status_code=200)
```

Повторная доставка Pub/Sub теперь полностью невидима для пайплайна: ни второй строки,
ни второго запуска онлайн-ветки.

---
## 4. Этап 1 — условный mark_processed

**Закрывает:** B1, B2 (окно гонки сужается до миллисекунд; герметично закрывается этапом 3).
**Файлы:** `app/services/batch_processor.py`.

### 4.1. Идея

Между `_fetch_pending` и `_mark_processed` проходят секунды (LLM-вызов). За это время
сообщение может быть отредактировано или удалено. Текущий код помечает пачку **слепо** —
по списку id. Новый код помечает **условно** — только если сообщение не изменилось с момента
fetch.

Механика: при fetch снимается снапшот версий (`id → text_hash`), при завершении флаша
UPDATE проверяет совпадение хэша и отсутствие удаления.

### 4.2. Код

```python
# flush_batch — новая обвязка
async def flush_batch(space_id: str) -> None:
    batch = await _fetch_pending(space_id)
    if not batch:
        return
    snapshot = {m.id: m.text_hash for m in batch}   # фиксируем версии ДО любой работы
    batch_id = uuid4()

    await _embed_batch(batch)                        # идемпотентно, как раньше
    markup = _build_markup(batch)
    items  = await extract_batch(markup)
    ok_ids = await _apply_items(batch, items, batch_id)   # см. этап 3: транзакция на item
    await _mark_processed_conditional(snapshot, only_ids=ok_ids)
```

```python
async def _mark_processed_conditional(
    snapshot: dict[int, str], only_ids: set[int]
) -> None:
    """Помечает processed только те сообщения, что (а) успешно применены,
    (б) не изменились с момента fetch, (в) не удалены."""
    pairs = [(mid, h) for mid, h in snapshot.items() if mid in only_ids]
    if not pairs:
        return
    await session.execute(
        text("""
            UPDATE chat_messages AS m
            SET process_status = 'processed', is_processed = true
            FROM (SELECT unnest(:ids)    AS id,
                         unnest(:hashes) AS h) AS s
            WHERE m.id = s.id
              AND m.text_hash = s.h          -- не было правки во время флаша
              AND m.is_deleted = false       -- не было удаления во время флаша
        """),
        {"ids": [p[0] for p in pairs], "hashes": [p[1] for p in pairs]},
    )
    await session.commit()
```

### 4.3. Разбор сценариев

**B1, правка во время флаша.** `handle_edit_batch` обновил `text` и `text_hash` →
условный UPDATE этот id пропускает → сообщение остаётся `pending` → следующий тик берёт его
**со свежим текстом** и переразбирает. Ревизии, успевшие записаться от старого текста,
аннулируются механизмом supersede из этапа 3 (handle_edit вызывает его для любого сообщения,
у которого есть ревизии, — независимо от process_status).

**B2, удаление во время флаша.** `is_deleted = true` → не помечаем processed → но и в очередь
сообщение больше не попадёт (фильтр `_fetch_pending`). Остаётся окно: `_apply_items` мог
успеть создать вакансию **до** прихода delete. Второй рубеж — перепроверка непосредственно
перед применением:

```python
# внутри _apply_items, перед resolve_and_save каждого item:
fresh = await session.get(ChatMessage, msg.id)        # дешёвая выборка по PK
if fresh.is_deleted or fresh.text_hash != snapshot[msg.id]:
    continue   # сообщение изменилось/удалено, пока шёл LLM — пропускаем item
```

Двойная проверка (перед apply + при mark) сужает окно с десятков секунд (длительность
LLM-вызова) до миллисекунд (между SELECT и INSERT одного item). Полная герметизация — в
этапе 3: проверка и запись попадают в одну транзакцию, а handle_delete после простановки
`is_deleted` повторно ищет ревизии (вторым запросом через 0 секунд он найдёт те, что успели
закоммититься в окне) и зовёт supersede + replay.

### 4.4. Изменение в handle_delete

Чтобы добить остаточное окно B2, `handle_delete` дополняется: после soft-delete сообщения он
**безусловно** вызывает supersede-механизм этапа 3 (а не только когда нашёл ревизии в первом
запросе). Supersede идемпотентен: нет ревизий — no-op; есть — аннулирует и делает replay.
Поскольку insert ревизии и UPDATE вакансии в этапе 3 атомарны, любая ревизия, видимая после
коммита, корректно откатывается, а невидимых изменений вакансии не существует.

---

## 5. Этап 2 — last-write-wins по времени сообщения

**Закрывает:** B3 (гонка веток), плюс out-of-order доставку Pub/Sub и повторные применения.
**Файлы:** `app/services/entity_resolution.py` (точка записи изменений вакансии).

### 5.1. Идея

Два пути обработки с разной задержкой означают: **порядок применения никогда не будет
гарантированно совпадать с хронологией чата**. Чинить это проверками «нет ли кого старше в
очереди» — латание дыр по одной. Правильное решение — сделать запись инвариантной к порядку:

> Поле вакансии изменяется входящей ревизией, только если её источник **не старше** источника
> текущего значения поля.

Время берётся из `created_at` сообщения (время чата), а не из времени обработки.

### 5.2. Код

```python
async def apply_field_change(
    vacancy: Vacancy,
    field: str,
    new_value: Any,
    src_msg: ChatMessage,
    action: str,
    batch_id: UUID | None,
) -> bool:
    """Возвращает True, если поле вакансии реально изменено."""
    last = await _get_last_applied_revision(vacancy.id, field)
    # last: applied=true, is_superseded=false, источник жив

    is_newer = (
        last is None
        or src_msg.created_at > last.source_created_at
        or (src_msg.created_at == last.source_created_at          # тай-брейк:
            and src_msg.message_id > last.source_message_id)      # детерминированно по id
    )

    inserted = await _insert_revision(            # ON CONFLICT ... RETURNING, см. этап 3
        vacancy_id=vacancy.id, action=action, changed_field=field,
        old_value=getattr(vacancy, field), new_value=new_value,
        source_message_id=src_msg.message_id,
        source_created_at=src_msg.created_at,
        source_text_hash=src_msg.text_hash,
        applied=is_newer, batch_id=batch_id,
    )
    if not inserted:
        return False          # дубль (retry) — уже применяли, выходим
    if is_newer:
        setattr(vacancy, field, new_value)
    return is_newer
```

Ключевые свойства:

- **Ревизия пишется всегда** — аудит полный: видно всё, что бот извлёк, включая опоздавшие
  изменения (`applied = false`).
- **Поле меняется только у победителя LWW.** Запоздавшее старое сообщение фиксируется в
  истории, но вакансию не трогает.
- **Тай-брейк по `message_id`** при равных timestamp — результат детерминирован при любом
  порядке применения.

### 5.3. Разбор B3 с новым кодом

```
M1 (10:00) "подняли до 300"  — без якоря, ждёт пачки
M2 (10:01) thread-reply "до 350" — якорь найден, онлайн-ветка
```

1. 10:01 — онлайн-ветка применяет M2: цепочка `salary_max` пуста → `applied=true`,
   `salary_max = 350`, `source_created_at = 10:01`.
2. 10:04 — пачка применяет M1: `last.source_created_at = 10:01 > 10:00` → ревизия пишется с
   `applied = false`, **поле не трогается**. `salary_max` остаётся 350. ✔

Тот же механизм автоматически чинит out-of-order Pub/Sub: события можно применять в любом
порядке, итог одинаков.

### 5.4. Где применяется

`apply_field_change` — единственная точка записи в поля вакансии. Через неё проходят:
онлайн-ветка, `_apply_items` пачки, re-extract после правки. Прямые `setattr`/UPDATE полей
вакансии вне этой функции запрещаются (грепнуть и заменить).

Внутри `_apply_items` items применяются строго в порядке `message_index` (хронология пачки) —
тогда внутри одной пачки LWW почти не срабатывает; он страхует именно **межпачечные** и
**межветочные** пересечения.

---

## 6. Этап 3 — replay: единый механизм пересборки поля

**Закрывает:** B4 (вместе с этапом 0), B11; герметизирует B1/B2; унифицирует
`_revert_message_deltas`, обработку правок и применение ревизий в один механизм.
**Файлы:** `app/services/revisions.py` (новый), `app/services/edits.py`,
`app/services/entity_resolution.py`.

### 6.1. Инвариант

Сейчас три разных куска кода делают по сути одно — поддерживают согласованность поля вакансии
с историей ревизий: `_revert_message_deltas` (удаление), re-extract (правка), применение
новых ревизий. У каждого свои краевые случаи, и они конфликтуют между собой (B4). Заменяем
одним инвариантом:

> **Значение поля вакансии = `new_value` последней живой применимой ревизии этого поля,
> упорядоченной по `(source_created_at, source_message_id)`.**
>
> «Живая» = `is_superseded = false` И сообщение-источник `is_deleted = false`.

Любое событие (новая ревизия, правка, удаление) — это изменение множества живых ревизий +
вызов `replay_field`, который приводит поле к инварианту.

### 6.2. Код

```python
# app/services/revisions.py

async def replay_field(vacancy_id: int, field: str) -> None:
    """Пересобирает значение поля из живой цепочки ревизий."""
    chain = await session.execute(
        select(VacancyRevision)
        .join(ChatMessage,
              ChatMessage.message_id == VacancyRevision.source_message_id)
        .where(
            VacancyRevision.vacancy_id == vacancy_id,
            VacancyRevision.changed_field == field,
            VacancyRevision.is_superseded == false(),
            ChatMessage.is_deleted == false(),
        )
        .order_by(VacancyRevision.source_created_at,
                  VacancyRevision.source_message_id)
    )
    chain = chain.scalars().all()

    if chain:
        value = chain[-1].new_value
    else:
        value = await _get_base_value(vacancy_id, field)
        # old_value самой первой (включая superseded) ревизии поля —
        # «как было до того, как бот вообще начал менять это поле»

    await session.execute(
        update(Vacancy).where(Vacancy.id == vacancy_id)
        .values({field: value})
    )
    # пересчёт applied: true только у chain[-1], false у остальных
    await _recompute_applied_flags(vacancy_id, field, winner=chain[-1] if chain else None)


async def supersede_message_revisions(message_id: str) -> set[tuple[int, str]]:
    """Аннулирует все ревизии сообщения. Возвращает {(vacancy_id, field)} для replay."""
    rows = await session.execute(
        update(VacancyRevision)
        .where(VacancyRevision.source_message_id == message_id,
               VacancyRevision.is_superseded == false())
        .values(is_superseded=True)
        .returning(VacancyRevision.vacancy_id, VacancyRevision.changed_field)
    )
    return {(v, f) for v, f in rows if f is not None}
```

### 6.3. Композиция событий

Все три проблемных события теперь — короткие композиции двух примитивов:

**Удаление обработанного сообщения** (заменяет `_revert_message_deltas` целиком):

```python
async def handle_delete(message_id: str) -> None:
    await soft_delete_message(message_id)                  # is_deleted = true
    affected = await supersede_message_revisions(message_id)
    for vacancy_id, field in affected:
        await replay_field(vacancy_id, field)
    for vacancy_id in {v for v, _ in affected}:
        await _soft_delete_vacancy_if_orphaned(vacancy_id)
        # нет живых create-ревизий → vacancies.is_deleted = true
```

Пример из старого документа (MSG1/MSG2) отрабатывает автоматически: replay по `salary_max`
находит живую ревизию MSG2 → 350000 остаётся; replay по `team` находит пустую цепочку →
откат к базовому значению. Отдельная логика `_fields_changed_after` больше не нужна.

**Правка обработанного сообщения:**

```python
async def handle_edit_processed(msg: ChatMessage, new_text: str) -> None:
    msg.text, msg.text_hash, msg.is_edited = new_text, sha256_hex(new_text), True
    affected = await supersede_message_revisions(msg.message_id)
    for vacancy_id, field in affected:
        await replay_field(vacancy_id, field)              # мир без старой версии
    await reembed_message(msg)
    await run_extraction(msg)                              # новые ревизии: новый text_hash →
                                                           # идемпотентный ключ их пропускает (B4 закрыт)
```

Семантика правки становится честной: «мир, как если бы сообщение сразу пришло в новой
редакции». Старые ревизии аннулированы, поля пересобраны, новая экстракция применена через
LWW (этап 2) — и если кто-то успел изменить поле сообщением **новее** правленого, правка его
корректно не перезатрёт.

**Правка pending-сообщения** — как раньше: только `text` + `text_hash` + `is_edited`, плюс
(новое) safety-вызов `supersede + replay` — на случай, если флаш успел записать ревизии в
окне гонки B1. Если ревизий нет (нормальный случай) — это no-op.

### 6.4. Транзакция на item (закрывает B11)

`_apply_items` оборачивает **каждый item** в собственную транзакцию:

```python
async def _apply_items(batch, items, batch_id) -> set[int]:
    ok: set[int] = set()
    index_to_vacancy: dict[int, int] = {}                  # см. этап 5.2
    for item in sorted(items, key=lambda i: i.message_index):
        msg = batch[item.message_index]
        try:
            async with session.begin():                    # одна транзакция на item
                fresh = await session.get(ChatMessage, msg.id, with_for_update=True)
                if fresh.is_deleted or fresh.text_hash != msg.text_hash:
                    continue                               # гонка — пропуск (этап 1)
                result = await _apply_one(fresh, item, index_to_vacancy, batch_id)
                if result.vacancy_id:
                    index_to_vacancy[item.message_index] = result.vacancy_id
            ok.add(msg.id)
        except Exception:
            log.exception("apply_item_failed", message_id=msg.message_id, batch_id=batch_id)
            await _bump_flush_attempts(msg.id)             # этап 6
            # НЕ прерываем цикл: остальные items продолжают применяться
    return ok
```

Свойства:

- Упал item 5 из 20 → items 1–4 закоммичены и попадут в `ok_ids` (пометятся processed),
  item 5 остаётся pending с инкрементом `flush_attempts`, items 6–20 применяются дальше.
  Повторный флаш возьмёт **только** упавший item, а не всю пачку.
- `with_for_update=True` на строке сообщения + проверка `is_deleted`/`text_hash` **внутри**
  транзакции → гонки B1/B2 закрыты герметично: конкурентный handle_edit/handle_delete либо
  ждёт коммита item и видит его ревизии, либо успевает первым — и тогда item пропускается.
- Изменение вакансии гейтится результатом insert ревизии:
  `INSERT ... ON CONFLICT DO NOTHING RETURNING id` → пустой результат = «уже применяли»
  (retry) → поле не трогаем. Дабл-апплай исключён конструктивно.

---

## 7. Этап 4 — целостность пачки

**Закрывает:** B6, B9.
**Файлы:** `app/services/batch_processor.py`, `app/llm/batch_extractor.py`.

### 7.1. Дозабор тредов (B6)

Тред — атомарная единица контекста; резать его по `LIMIT` — терять ровно то, ради чего
строилась пачка. После основного fetch добираем хвосты попавших тредов:

```python
async def _fetch_pending(space_id: str) -> list[ChatMessage]:
    base = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.space_id == space_id,
               ChatMessage.process_status == "pending",
               ChatMessage.is_deleted == false())
        .order_by(ChatMessage.created_at)
        .limit(settings.BATCH_SIZE)
    )
    batch = list(base.scalars())

    thread_ids = {m.thread_id for m in batch if m.thread_id}
    if thread_ids:
        got = {m.id for m in batch}
        extra = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.space_id == space_id,
                   ChatMessage.process_status == "pending",
                   ChatMessage.is_deleted == false(),
                   ChatMessage.thread_id.in_(thread_ids),
                   ChatMessage.id.notin_(got))
            .order_by(ChatMessage.created_at)
        )
        batch += list(extra.scalars())
        batch.sort(key=lambda m: m.created_at)

    return _cap_by_tokens(batch)
```

### 7.2. Лимит по токенам, а не по штукам

100 коротких реплик и 100 простыней с описаниями вакансий — несравнимые нагрузки на контекст.
Бюджетируем по токенам (грубая оценка `len(text) // 3` достаточна), режем по границе треда:

```python
def _cap_by_tokens(batch: list[ChatMessage],
                   budget: int = settings.BATCH_TOKEN_BUDGET) -> list[ChatMessage]:
    """budget по умолчанию 12_000. Резать пачку можно только между тредами."""
    out, used = [], 0
    i = 0
    while i < len(batch):
        # группа = весь тред целиком, либо одиночное сообщение
        if batch[i].thread_id:
            group = _take_thread(batch, i)   # все сообщения этого thread_id из batch
        else:
            group = [batch[i]]
        cost = sum(len(m.text or "") // 3 for m in group)
        if out and used + cost > budget:
            break                       # остаток уйдёт в следующий тик
        out += group
        used += cost
        i += len(group)
    return out
```

(Реализацию `_take_thread` — взять подряд идущие сообщения одного thread_id — оставить
простой; точность оценки токенов некритична, важен порядок величины.)

Незабранный остаток никуда не девается: он pending, его возьмёт следующий тик. Поскольку резка
идёт по границе треда, разрыв контекста не происходит.

### 7.3. Полное покрытие индексов (B9)

Контракт с LLM меняется: вердикт обязателен по **каждому** индексу, `action: "none"` — явно.

Изменение системного промпта:

```
Верни JSON-массив с РОВНО ОДНИМ объектом на КАЖДЫЙ индекс из секции «Новые сообщения»,
включая сообщения не о вакансиях — для них action = "none".
Пропуск индекса считается ошибкой.
```

Валидация после парсинга:

```python
def _validate_coverage(items: list[BatchItem], batch_len: int) -> tuple[list[BatchItem], set[int]]:
    seen: dict[int, BatchItem] = {}
    for it in items:
        if not (0 <= it.message_index < batch_len):
            log.warning("llm_index_out_of_range", index=it.message_index)
            continue                                  # галлюцинация индекса — отброс
        if it.message_index in seen:
            log.warning("llm_duplicate_index", index=it.message_index)
            continue                                  # дубль — берём первый
        seen[it.message_index] = it
    missing = set(range(batch_len)) - set(seen)
    return list(seen.values()), missing
```

`missing` индексы **не попадают в `ok_ids`** → не помечаются processed → уходят на повторный
флаш с инкрементом `flush_attempts`. «Модель забыла» и «модель решила, что это не про
вакансии» теперь различимы: первое — повтор, второе — явный `none` и честный processed.

---

## 8. Этап 5 — точность связывания

**Закрывает:** B7, B8. Это основной прирост качества — то, ради чего batch-режим существует.
**Файлы:** `app/services/batch_processor.py` (`_build_markup`), `app/llm/batch_extractor.py`.

### 8.1. Хвост обработанной истории как read-only контекст (B7)

Пачка должна видеть не только открытые вакансии, но и **недавние обработанные сообщения с их
привязками**. Тогда follow-up через границу пачек резолвится подстановкой, а не угадыванием.

Выборка: последние `HISTORY_TAIL=25` processed-сообщений space за последние
`HISTORY_HOURS=24` часов, с привязкой через ревизии:

```sql
SELECT m.message_id, m.sender, m.text, m.created_at,
       v.id AS vacancy_id, v.title AS vacancy_title
FROM chat_messages m
LEFT JOIN LATERAL (
    SELECT r.vacancy_id
    FROM vacancy_revisions r
    WHERE r.source_message_id = m.message_id
      AND r.is_superseded = false
    ORDER BY r.id DESC
    LIMIT 1
) rv ON true
LEFT JOIN vacancies v ON v.id = rv.vacancy_id AND v.is_deleted = false
WHERE m.space_id = :space_id
  AND m.process_status = 'processed'
  AND m.is_deleted = false
  AND m.created_at > now() - interval '24 hours'
ORDER BY m.created_at DESC
LIMIT 25;
```

Формат в промпте (история — в хронологическом порядке, с метками `[hN]`):

```
Открытые вакансии:
- id=12 «Backend (Python)» (до 300к) [latest]
- id=14 «DevOps Engineer»

Недавняя история (ТОЛЬКО контекст, items по ним НЕ возвращать):
[h0] Иван (вчера 16:02) → вакансия id=12: ищем питониста в бэкенд, до 300к
[h1] Петя (вчера 17:40) → вакансия id=14: нужен DevOps, удалёнка
[h2] Мария (сегодня 09:15): всем привет, я из бухгалтерии

Новые сообщения (извлекать):
[0] Иван: по питонисту подняли до 400
[1] Саша (↳ ответ на [h1]): а какой грейд?
```

Два важных приёма:

- **Стрелка `→ вакансия id=N`** превращает связывание в подстановку: LLM возвращает
  `entity_ref: "12"` (или `link_to_history: "h0"`), и резолв становится точным lookup'ом
  вместо эмбеддинг-поиска.
- **reply_to из истории**: если процитированное сообщение обработано ранее, метка `[↳ ответ
  на hN]` указывает прямо в историю — текущая логика `reply_to_text` (подгрузка текста цитаты
  из БД) этим поглощается и усиливается, потому что теперь видна ещё и привязка цитаты.

В системный промпт — жёсткое правило: *«Сообщения [hN] — только контекст. Возвращать items
разрешено исключительно для индексов из секции "Новые сообщения"»*. Валидация из 7.3
дополнительно отбрасывает любые items с индексами вне диапазона пачки.

### 8.2. `link_to_index`: связи внутри пачки доходят до БД (B8)

Расширение схемы ответа:

```python
class BatchItem(ExtractionResult):
    message_index: int
    link_to_index: int | None = None       # та же сущность, что у сообщения N этой пачки
    link_to_vacancy_id: int | None = None  # прямая ссылка: id из списка вакансий или из [hN]
    # наследует: action, entity_ref, fields, confidence
```

Правила для LLM (порядок приоритета привязки, дополняет текущий):

```
1. Явная цитата [↳ ответ на N]            → link_to_index = N
2. Явная цитата [↳ ответ на hN]           → link_to_vacancy_id вакансии из [hN]
3. Один тред (общая метка «тред A»)       → link_to_index первого сообщения треда
4. Смысловая связь внутри пачки           → link_to_index
5. Упоминание вакансии из списка/истории  → link_to_vacancy_id
6. Ничего из перечисленного               → оба поля null (резолв по entity_ref)

ЗАПРЕЩЕНО: привязывать по совпадению зарплаты или других чисел — только по структуре.
```

Применение в `_apply_one` (вызывается из транзакционного цикла этапа 6.4):

```python
async def _apply_one(msg, item, index_to_vacancy, batch_id):
    vacancy_id = None

    if item.link_to_index is not None:
        vacancy_id = index_to_vacancy.get(item.link_to_index)
        # цепочки работают: [4]→[2]→[0] — к моменту [4] мапа уже содержит [2],
        # потому что items применяются в порядке message_index

    if vacancy_id is None and item.link_to_vacancy_id is not None:
        vacancy_id = await _verify_open_vacancy(item.link_to_vacancy_id)
        # верифицируем: id существует, не удалён, не закрыт; иначе None и идём дальше

    if vacancy_id is None:
        anchor = await find_anchor_vacancy(msg)            # цитата/тред — как раньше
        vacancy_id = anchor.id if anchor else None

    if vacancy_id is not None:
        if item.action == "create":
            item.action = "update"                          # та же логика, что была
        return await apply_to_vacancy(vacancy_id, item, msg, batch_id)
        # apply_to_vacancy → apply_field_change (LWW, этап 2) для каждого поля

    return await resolve_and_save(msg, item, batch_id)
    # старый путь (эмбеддинг entity_ref, порог 0.75, _decide_new_posting) — только fallback
```

Эмбеддинг-резолв остаётся ровно там, где структурного сигнала действительно нет. Связь,
которую модель нашла, больше не переоткрывается заново и не теряется на пороге 0.75.

**Важно про `link_to_vacancy_id`:** LLM может галлюцинировать id, поэтому верификация
(`_verify_open_vacancy`) обязательна — несуществующий/закрытый id тихо деградирует в
следующий уровень резолва, а не падает и не создаёт мусор.

---

## 9. Этап 6 — защита от ядовитой пачки

**Закрывает:** B10.
**Файлы:** `app/services/batch_processor.py`, `app/llm/batch_extractor.py`.

### 9.1. Состояния и пороги

```
PENDING ──(флаш ок)──────────────► PROCESSED
   │
   ├─(флаш неудачен)── flush_attempts += 1
   │
   ├─(attempts ≥ BISECT_THRESHOLD=3, в составе пачки)──► бисекция пачки
   │
   └─(attempts ≥ FAIL_THRESHOLD=5, одиночное)──► FAILED + алерт
                                                    │
                            (правка текста человеком)┘──► PENDING, attempts=0
```

### 9.2. Инкремент попыток

Попытка считается неудачной для сообщения, если: (а) `extract_batch` вернул `[]` после своих
ретраев — инкремент **всем** сообщениям пачки; (б) индекс попал в `missing` валидации 7.3 —
инкремент этому сообщению; (в) item упал в `_apply_items` — инкремент этому сообщению
(уже есть в коде этапа 6.4).

```python
async def _bump_flush_attempts(*message_pks: int) -> None:
    await session.execute(
        update(ChatMessage)
        .where(ChatMessage.id.in_(message_pks))
        .values(flush_attempts=ChatMessage.flush_attempts + 1)
    )
```

### 9.3. Бисекция

Если в выбранной пачке есть сообщение с `flush_attempts >= 3`, флаш переключается в режим
деления: пачка режется пополам **по границе тредов**, половины флашатся как независимые пачки.

```python
async def flush_batch(space_id: str) -> None:
    batch = await _fetch_pending(space_id)
    if not batch:
        return
    if max(m.flush_attempts for m in batch) >= settings.BISECT_THRESHOLD and len(batch) > 1:
        left, right = _split_on_thread_boundary(batch)
        await _flush_concrete(space_id, left)
        await _flush_concrete(space_id, right)
        return
    await _flush_concrete(space_id, batch)
```

Здоровая половина проходит и помечается processed; больная делится дальше на следующих тиках.
За `log2(N)` итераций ядовитое сообщение изолируется до пачки из одного элемента.

### 9.4. Dead-letter

Одиночное сообщение с `flush_attempts >= 5`:

```python
await session.execute(
    update(ChatMessage)
    .where(ChatMessage.id == msg.id)
    .values(process_status="failed")
)
log.error("message_dead_lettered", message_id=msg.message_id,
          text_preview=msg.text[:500], attempts=msg.flush_attempts)
metrics.messages_failed_total.inc()
```

`failed` — терминальный статус: `_fetch_pending` фильтрует по `process_status = 'pending'`,
сообщение выпадает из очереди автоматически. Очередь space разблокирована, токены не горят.
Возврат в работу — правка сообщения человеком: `handle_edit_batch` для `failed` сбрасывает
статус в `pending` и обнуляет `flush_attempts`.

### 9.5. Самопочинка парсинга

Дешёвый приём в `extract_batch`: при ретрае после битого JSON добавлять в диалог сообщение об
ошибке предыдущей попытки:

```python
messages.append({"role": "assistant", "content": raw_broken_response})
messages.append({"role": "user", "content":
    f"Ответ не распарсился: {parse_error}. Верни ТОЛЬКО валидный JSON-массив по схеме, "
    f"без markdown-ограждений и пояснений."})
```

Заметная доля битых ответов чинится со второй попытки именно так — модель видит свою ошибку,
а не повторяет её вслепую.

---

## 10. Этап 7 — триггер флаша и блокировки

**Закрывает:** B12, B13.
**Файлы:** `app/services/batch_processor.py`, `app/core/settings.py`.

### 10.1. Quiet window: флаш по естественным границам разговора (B13)

Текущий триггер `count OR max-age` режет живую переписку по таймеру. Добавляется третье
условие — тишина в space. Итоговый трёхусловный триггер:

```
flush(space), если:
    cnt >= BATCH_SIZE                                        -- (1) защита от распухания
 OR (cnt > 0 AND now - last_msg_at >= QUIET_SECONDS)         -- (2) разговор затих
 OR (now - oldest_pending >= MAX_AGE_SECONDS)                -- (3) жёсткий потолок
```

Роли условий: (2) — **основной** рабочий триггер, пачки совпадают со всплесками разговора и
родственные сообщения систематически оказываются вместе; (1) и (3) — предохранители: бурный
чат не копит гигантскую пачку, одинокое сообщение не висит дольше потолка даже при
непрекращающемся фоновом трёпе.

```sql
-- flush_due_batches: один запрос даёт все три величины
SELECT space_id,
       COUNT(*)        AS cnt,
       MIN(created_at) AS oldest,
       MAX(created_at) AS last_msg_at
FROM chat_messages
WHERE process_status = 'pending' AND is_deleted = false
GROUP BY space_id;
```

```python
# настройки (дефолты)
BATCH_SIZE = 100
QUIET_SECONDS = 75            # 60–90с: дольше типичной паузы при наборе follow-up
MAX_AGE_SECONDS = 300         # прежний BATCH_TIMEOUT_SECONDS
BATCH_POLL_SECONDS = 15       # тикер чаще: quiet window требует разрешающей способности
BATCH_TOKEN_BUDGET = 12_000   # этап 7.2 документа (лимит по токенам)
```

`BATCH_POLL_SECONDS` уменьшается до 15: при тике в 30с реальная задержка после затишья
плавает в диапазоне 75–105с; при 15с — 75–90с. Запрос лёгкий (partial index), учащение
бесплатно.

### 10.2. Lock per space + пропуск вместо ожидания (B12)

```python
from collections import defaultdict

_space_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

async def flush_batch(space_id: str) -> None:
    lock = _space_locks[space_id]
    if lock.locked():
        return                  # space уже флашится этим процессом — пропускаем тик
    async with lock:
        ...
```

Два изменения в одном: (а) медленный space больше не блокирует остальные; (б) `if locked():
return` вместо ожидания — тикер не копит очередь корутин на залипшем space, незабранные
сообщения просто дождутся следующего тика.

### 10.3. Мультиворкер (заготовка, активировать при горизонтальном масштабировании)

`asyncio.Lock` защищает только внутри процесса. Для нескольких воркеров — claim на уровне БД,
поле `claimed_at` уже добавлено этапом 0:

```sql
-- claim пачки: атомарно и без блокировок между воркерами
UPDATE chat_messages
SET claimed_at = now()
WHERE id IN (
    SELECT id FROM chat_messages
    WHERE space_id = :space_id
      AND process_status = 'pending'
      AND is_deleted = false
      AND (claimed_at IS NULL OR claimed_at < now() - interval '10 minutes')
    ORDER BY created_at
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

Протухание claim через `2 × MAX_AGE` (10 минут): воркер умер посреди флаша — другой подберёт
пачку без ручного вмешательства. После успешного `_mark_processed_conditional` сбрасывать
`claimed_at = NULL` у непомеченных (изменившихся) сообщений. До появления второго воркера код
не активируется — флаг `MULTI_WORKER=false`.

---

## 11. Этап 8 — онлайн-ветка: страховка порядка

**Дополняет:** B3 (LWW этапа 2 уже гарантирует корректность; здесь — оптимизация контекста).
**Файлы:** `app/services/batch_processor.py::route_created_batch()`.

После этапа 2 онлайн-ветка не может ничего испортить: даже если она применит follow-up раньше
его более старого соседа, LWW не даст старому перезатереть новое. Остаётся неоптимальность
другого рода: тред, разорванный между онлайн-обработкой и пачкой, разбирается **двумя
независимыми LLM-взглядами** вместо одного цельного. Дешёвая проверка:

```python
async def route_created_batch(incoming: IncomingMessage) -> None:
    anchor = await find_anchor_vacancy(incoming)
    if anchor is None:
        return                                   # как раньше: ждёт пачки

    if incoming.thread_id:
        older_pending = await session.scalar(
            select(exists().where(
                ChatMessage.thread_id == incoming.thread_id,
                ChatMessage.process_status == "pending",
                ChatMessage.is_deleted == false(),
                ChatMessage.created_at < incoming.created_at,
            ))
        )
        if older_pending:
            log.info("online_deferred_to_batch", message_id=incoming.message_id)
            return                               # уходим в пачку вместе со старшим соседом

    await _process_online(incoming, anchor)      # embed → extract → conditional mark
```

Компромисс: жертвуем секундами отклика ради цельного разбора треда одной пачкой. Для системы,
где точность важнее моментальности (иначе batch-режим не был бы нужен), обмен правильный.
Сообщение, ушедшее в пачку, обработается в пределах `QUIET_SECONDS`–`MAX_AGE_SECONDS`.

Онлайн-ветка также переводится на общие примитивы: `mark_processed` → условный (этап 1),
запись полей → `apply_field_change` (этап 2). После этого обе ветки пишут в вакансии через
одну и ту же точку — расхождение логики исключено конструктивно.

---

## 12. Этап 9 — наблюдаемость

**Делает видимыми:** все этапы выше. Без метрик этапы 6–7 слепые.
**Файлы:** `app/core/metrics.py`, обвязка в `batch_processor.py`.

### 12.1. Метрики

```
# Counters
batch_flush_total{space, result="ok|partial|retry|empty"}
messages_failed_total                          ← алерт при росте
revisions_total{action, applied="true|false"}
resolve_method_total{method="quote|thread|link_to_index|link_to_vacancy|history|embedding|new"}

# Histograms
batch_size_messages
batch_size_tokens
batch_flush_duration_seconds
llm_extract_duration_seconds

# Gauges
oldest_pending_age_seconds{space}              ← ГЛАВНЫЙ health-индикатор
pending_messages_total{space}
```

### 12.2. Как читать

**`oldest_pending_age_seconds` растёт выше `2 × MAX_AGE`** → очередь встала: ядовитая пачка,
лежащий LLM-провайдер или мёртвый тикер. Единственная метрика, по которой стоит будить
алертом.

**`resolve_method_total`** — прямое измерение эффекта этапа 5: после деплоя доля `embedding`
должна заметно упасть, доли `link_to_index`/`history`/`quote` — вырасти. Если через неделю
`embedding` всё ещё доминирует — промпт не доносит правила привязки, идти крутить его. Это
KPI всей доработки.

**`revisions_total{applied="false"}`** — частота срабатывания LWW: сколько изменений пришло
с опозданием. Стабильно высокая доля → онлайн-ветка и пачка систематически конфликтуют,
смотреть тайминги.

**`batch_flush_total{result="partial"}`** (часть items упала) → смотреть
`apply_item_failed` в логах по `batch_id`.

### 12.3. Сквозной batch_id

`batch_id` (uuid, генерируется в начале `flush_batch`) пишется: во все лог-записи флаша, во
все ревизии пачки (`vacancy_revisions.batch_id`, поле из этапа 0). Отладка «почему вакансия
стала такой» сводится к двум запросам:

```sql
-- кто менял вакансию
SELECT * FROM vacancy_revisions WHERE vacancy_id = :id
ORDER BY source_created_at;

-- что происходило в том флаше
SELECT * FROM vacancy_revisions WHERE batch_id = :batch_id;
-- + grep batch_id по логам: markup, сырой ответ LLM, валидация, items
```

В лог флаша на уровне DEBUG — полный markup и сырой ответ LLM: при разборе инцидентов
связывания это единственный способ понять, что именно видела модель.

---

## 13. Порядок релизов и зависимости

| Релиз | Этапы | Содержание | Зависимости |
|-------|-------|------------|-------------|
| **R1 — корректность данных** | 0, 1, 2, 3 | миграция, дедуп, условный mark, LWW, replay, транзакция на item | — |
| **R2 — точность** | 4, 5 | дозабор тредов, токен-бюджет, покрытие индексов, история, link_to_index | R1 |
| **R3 — устойчивость** | 6, 7 | бисекция, dead-letter, quiet window, per-space lock | R1 (поля), желательно R2 |
| **R4 — дошлифовка** | 8, 9 | страховка онлайн-ветки, метрики | R1, R2 |

Принципы нарезки:

- **R1 не делится.** Этапы 0–3 образуют целостный контур «данные не теряются и не
  искажаются»: условный mark без replay оставляет дыру B4, LWW без транзакции на item — дыру
  B11. По частям выкатывать хуже, чем целиком.
- Метрики (этап 9) можно и стоит подмешивать в каждый релиз по мере появления точек
  измерения — выделены в R4 только формально.
- После R1 прогнать **бэктест**: реплей реальной недельной истории сообщений на копии БД,
  сравнить итоговые состояния вакансий до/после. LWW и replay меняют семантику применения —
  расхождения нужно увидеть на копии, а не в проде.

### Что НЕ делаем (отброшенные альтернативы)

- **Очередь правок/удалений с версионированием событий** (event sourcing полного цикла) —
  правильная, но избыточная архитектура для текущего масштаба; replay-инвариант даёт те же
  гарантии на порядок дешевле.
- **Глобальная сериализация: всё через пачку, без онлайн-ветки** — упростило бы порядок, но
  убивает быстрый отклик на thread-reply; LWW решает конфликт без этой жертвы.
- **Распределённый lock (Redis/advisory) уже сейчас** — до второго воркера это мёртвый код;
  заготовка `claimed_at` лежит в схеме и ждёт.

---

## 14. Инварианты системы

После R1–R3 система обязана соблюдать следующие инварианты. Каждый — кандидат в
property-based тест или периодическую проверку консистентности.

**I1 (содержимое поля).** Для каждого поля каждой вакансии: значение равно `new_value`
последней живой применимой ревизии по `(source_created_at, source_message_id)`, либо базовому
значению при пустой цепочке. *Проверка:* SQL-запрос-аудитор по всем вакансиям, расхождение =
баг.

**I2 (нет осиротевших изменений).** Не существует ревизии с `applied = true`, чей источник
`is_deleted = true` или `is_superseded = true`. *Проверка:* один SELECT с JOIN.

**I3 (нет вакансий-зомби).** Каждая неудалённая вакансия имеет хотя бы одну живую
create-ревизию. *Проверка:* SELECT с NOT EXISTS.

**I4 (терминальность обработки).** Сообщение с `process_status = 'processed'` имеет
`text_hash`, совпадающий с hash'ем на момент пометки; правка переводит его обратно в цикл
re-extract (через supersede + новые ревизии), но не оставляет «обработанным со старым
смыслом».

**I5 (полнота очереди).** Каждое сообщение из `pending` либо моложе `MAX_AGE`, либо его
`flush_attempts > 0` (система пытается), либо оно `failed` (система сдалась видимо).
Состояния «висит вечно и никто не пытается» не существует. *Проверка:* метрика
`oldest_pending_age_seconds` + запрос-аудитор.

**I6 (порядконезависимость).** Применение одного и того же набора событий (created/updated/
deleted) в любом порядке даёт одно и то же итоговое состояние вакансий. *Проверка:*
property-based тест — генерация случайных историй, применение в случайных перестановках,
сравнение итогов.

---

## 15. Тестовые сценарии

Минимальный набор интеграционных тестов, покрывающий каталог проблем. Каждый тест назван по
проблеме, которую охраняет.

### Гонки (R1)

- **T-B1:** fetch пачки → правка сообщения (новый text_hash) → завершение флаша → assert:
  сообщение pending, следующий флаш разбирает новый текст, ревизий от старого текста с
  `is_superseded = false` нет.
- **T-B2:** fetch пачки → delete сообщения → завершение флаша → assert: вакансия от
  удалённого сообщения отсутствует либо soft-deleted; поля чужих вакансий не задеты.
- **T-B3:** M1 (старше, без якоря) + M2 (новее, с якорем, онлайн) → пачка применяет M1 после →
  assert: поле = значение M2; ревизия M1 существует с `applied = false`.
- **T-B4:** processed-сообщение → правка → re-extract с другими полями → assert: новая
  ревизия записана (новый text_hash), поле вакансии обновлено, старые ревизии superseded.
- **T-B5:** двойная доставка Pub/Sub одного message_id → assert: одна строка, одна фоновая
  задача, одна ревизия.
- **T-LWW-ties:** два сообщения с равным created_at, разные значения одного поля, применение
  в обоих порядках → assert: итог одинаков (тай-брейк по message_id).
- **T-replay-delete:** сценарий MSG1/MSG2 из старого документа (раздел 13) → assert:
  salary_max = 350000, team откатан в None — теперь через replay, без `_fields_changed_after`.

### Целостность пачки (R2)

- **T-B6:** 130 pending, тред пересекает границу 100 → assert: тред целиком в одной пачке.
- **T-B9:** мок LLM возвращает items без одного индекса → assert: индекс не processed,
  flush_attempts += 1, остальные processed.
- **T-tokens:** пачка превышает токен-бюджет → assert: резка по границе треда, остаток
  обработан следующим тиком.

### Точность (R2)

- **T-B7:** create в пачке 1 → флаш → follow-up «по нему подняли» в пачке 2 → assert: история
  в markup пачки 2 содержит [hN] с привязкой; update попал в ту же вакансию через
  `link_to_vacancy_id` (мок LLM), `resolve_method = history`.
- **T-B8:** create [0] + update [2] с `link_to_index = 0` в одной пачке → assert: update
  применён к вакансии из [0] напрямую, эмбеддинг-поиск не вызывался.
- **T-halluc-id:** мок LLM возвращает `link_to_vacancy_id` несуществующей вакансии → assert:
  деградация в fallback-резолв, без исключения и без мусорной записи.
- **T-chain:** [4]→[2]→[0] цепочка ссылок → assert: все три на одной вакансии.

### Устойчивость (R3)

- **T-B10:** мок LLM стабильно ломается на пачке из 8 сообщений, ядовитое — одно → assert:
  за ≤ 3 бисекции 7 сообщений processed, ядовитое — failed, алерт-метрика инкрементнута,
  очередь space пуста.
- **T-B11:** resolve падает на item 5 из 20 → assert: 19 processed, item 5 pending с
  attempts=1, следующий флаш содержит только его.
- **T-failed-revive:** failed-сообщение правится → assert: pending, attempts=0, разобрано
  следующим флашем.
- **T-B12:** медленный флаш space A (мок-задержка LLM) → тик → assert: space B флашится
  параллельно, по space A тик пропущен без накопления корутин.
- **T-B13:** сообщения каждые 20с при QUIET=75 → assert: флаша нет до затишья; пауза 80с →
  флаш; непрерывный поток дольше MAX_AGE → флаш по потолку.

### Property-based (I6)

- **T-I6:** генератор случайных историй (create/update/close/edit/delete, 2–4 вакансии,
  10–40 сообщений) → применение в N случайных перестановках путей (онлайн/пачка/правки) →
  assert: итоговые состояния вакансий идентичны во всех перестановках. Это самый ценный тест
  файла: он охраняет сразу этапы 1–3 и ловит регрессии, которые точечные тесты не видят.

---

## Приложение А — сводка новых настроек

| Переменная | Дефолт | Этап | Назначение |
|------------|--------|------|------------|
| `BATCH_SIZE` | 100 | — | прежний смысл: потолок выборки по количеству |
| `BATCH_TOKEN_BUDGET` | 12000 | 4 | потолок пачки по токенам (режет по границе треда) |
| `QUIET_SECONDS` | 75 | 7 | тишина в space → флаш |
| `MAX_AGE_SECONDS` | 300 | 7 | жёсткий потолок ожидания (бывш. BATCH_TIMEOUT_SECONDS) |
| `BATCH_POLL_SECONDS` | 15 | 7 | период тикера (учащён под quiet window) |
| `HISTORY_TAIL` | 25 | 5 | сообщений истории в контекст пачки |
| `HISTORY_HOURS` | 24 | 5 | глубина истории |
| `BISECT_THRESHOLD` | 3 | 6 | flush_attempts → бисекция пачки |
| `FAIL_THRESHOLD` | 5 | 6 | flush_attempts одиночного → dead-letter |
| `MULTI_WORKER` | false | 7 | активация claim-механики через claimed_at |

## Приложение Б — словарь новых терминов

| Термин | Смысл |
|--------|-------|
| **условный mark** | пометка processed только при неизменности text_hash и отсутствии delete |
| **LWW** | last-write-wins по `source_created_at`: поле меняет только хронологически новейший источник |
| **живая ревизия** | `is_superseded = false` и источник `is_deleted = false` |
| **replay** | пересборка поля вакансии из живой цепочки ревизий по инварианту I1 |
| **supersede** | аннулирование ревизий сообщения (при правке/удалении) с последующим replay |
| **бисекция** | деление проблемной пачки пополам для изоляции ядовитого сообщения |
| **dead-letter** | терминальный статус `failed`: сообщение исключено из очереди до правки человеком |
| **quiet window** | триггер флаша по тишине в space, основной механизм нарезки пачек |
| **[hN]** | метка сообщения из read-only истории в markup пачки |
| **link_to_index** | ссылка LLM «та же сущность, что у сообщения N этой пачки» |
