"""LLM-классификатор: новое объявление о найме или правка существующей вакансии.

Резолвер отвечает на вопрос «к какой записи относится сообщение», но не «это
новый набор или изменение условий». Похожая открытая вакансия матчится в обоих
случаях, поэтому намерение определяем отдельным узким классификатором. Решение
комбинируется с regex-эвристикой в entity_resolution._decide_new_posting.
"""

import json

from pydantic import BaseModel

from app.config import settings
from app.llm.client import chat
from app.logger import logger

_SYSTEM = """\
Ты — классификатор сообщений о вакансиях в корпоративном чате.
Определи намерение сообщения: это объявление о НОВОМ найме или правка/уточнение
условий уже идущей вакансии.

Тебе дают JSON:
- text: текст нового сообщения
- existing: похожая уже открытая вакансия (title, team, description) или null

new_posting = true — сообщение объявляет набор сотрудника на позицию:
  «ищем …», «нужен …», «требуется …», «открыли вакансию …», «в команду … нужен»,
  «приглашаем …», англ. «hiring / looking for / open position».
  Это true ДАЖЕ если existing — такая же роль: на одну позицию могут набирать
  несколько человек, и это отдельный найм.

new_posting = false — сообщение меняет условия существующей позиции или это
  короткий follow-up: зарплата, статус, требования, команда, овнер
  («подняли до 400k», «теперь удалёнка», «зп от 2000», «закрыли», «вышел
  кандидат», «по этой вакансии добавили …»).

Отвечай ТОЛЬКО валидным JSON без markdown-блоков:
- new_posting: true | false
- confidence: 0.0–1.0 — насколько уверен в ответе.\
"""


class PostingVerdict(BaseModel):
    new_posting: bool = False
    confidence: float = 0.0


def _existing_brief(existing: dict | None) -> dict | None:
    if not existing:
        return None
    return {k: existing.get(k) for k in ("title", "team", "description")}


async def classify_new_posting(text: str, existing: dict | None) -> PostingVerdict:
    """Намерение сообщения. При сбое/битом ответе — confidence 0.0 (решит regex)."""
    payload = json.dumps(
        {"text": text, "existing": _existing_brief(existing)}, ensure_ascii=False
    )
    try:
        raw = await chat(
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": payload},
            ],
            model=settings.openrouter_model_extract,
            response_format={"type": "json_object"},
        )
        return PostingVerdict(**json.loads(raw.strip()))
    except Exception as exc:
        logger.warning("posting_classify_error", error=str(exc))
        return PostingVerdict(new_posting=False, confidence=0.0)
