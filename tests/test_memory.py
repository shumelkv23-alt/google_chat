"""Тесты памяти диалога: свёртка старых реплик в running_summary (этап 7).

Стиль как в test_ingest: asyncio.run() + patch на модульные имена + живая БД
с cleanup. LLM-свёртка везде замокана — в сеть не ходим.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import select, text

from app.db.models import Conversation
from app.db.session import AsyncSessionLocal, engine
from app.services.memory import KEEP_LAST, MAX_TURNS, compact_conversation

_USER = "users/memory-test"
_SPACE = "spaces/MEMORY-TEST"


def _make_turns(n: int) -> list[dict]:
    """n реплик с чередованием ролей user/assistant."""
    turns = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        turns.append(
            {"role": role, "text": f"реплика {i}", "ts": "2026-01-01T00:00:00+00:00"}
        )
    return turns


async def _seed(turns: list[dict], summary: str | None) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM conversations WHERE user_id=:u AND space_id=:s"),
            {"u": _USER, "s": _SPACE},
        )
        session.add(
            Conversation(
                user_id=_USER,
                space_id=_SPACE,
                running_summary=summary,
                recent_turns=turns,
                turns_count=len(turns) // 2,
            )
        )
        await session.commit()


async def _load() -> Conversation:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(Conversation).where(
                    Conversation.user_id == _USER,
                    Conversation.space_id == _SPACE,
                )
            )
        ).scalar_one()


async def _cleanup() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM conversations WHERE user_id=:u AND space_id=:s"),
            {"u": _USER, "s": _SPACE},
        )
        await session.commit()


# --- summarizer: формирование промпта ---------------------------------------


def test_summarize_conversation_builds_prompt() -> None:
    from app.llm import summarizer

    async def _run() -> None:
        with patch.object(
            summarizer, "chat", new=AsyncMock(return_value="  новая выжимка  ")
        ) as mock_chat:
            result = await summarizer.summarize_conversation("старое", _make_turns(4))

        # Возврат тримится
        assert result == "новая выжимка"
        # Модель свёртки + и старая выжимка, и реплики попали в user-промпт
        kwargs = mock_chat.call_args.kwargs
        assert kwargs["model"] == summarizer.settings.openrouter_model_summarize
        user_msg = kwargs["messages"][-1]["content"]
        assert "старое" in user_msg
        assert "реплика 0" in user_msg
        assert "реплика 3" in user_msg

    asyncio.run(_run())


# --- compact_conversation: свёртка на живой БД -------------------------------


def test_compact_conversation_folds_old_turns() -> None:
    async def _run() -> None:
        await _seed(_make_turns(14), "былое")
        with patch(
            "app.services.memory.summarize_conversation",
            new=AsyncMock(return_value="свёрнутая память"),
        ) as mock_sum:
            await compact_conversation(_USER, _SPACE)

        # summarize получил старую выжимку + всё, кроме последних KEEP_LAST реплик
        called_summary, called_old = mock_sum.call_args.args
        assert called_summary == "былое"
        assert len(called_old) == 14 - KEEP_LAST

        conv = await _load()
        assert conv.running_summary == "свёрнутая память"
        assert len(conv.recent_turns) == KEEP_LAST
        assert conv.summary_updated_at is not None
        # В окне остались именно последние реплики
        assert conv.recent_turns[-1]["text"] == "реплика 13"
        assert conv.recent_turns[0]["text"] == "реплика 8"

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


def test_compact_conversation_noop_when_short() -> None:
    async def _run() -> None:
        await _seed(_make_turns(KEEP_LAST), "ничего не менять")
        with patch(
            "app.services.memory.summarize_conversation",
            new=AsyncMock(return_value="не должно вызваться"),
        ) as mock_sum:
            await compact_conversation(_USER, _SPACE)

        mock_sum.assert_not_called()
        conv = await _load()
        assert conv.running_summary == "ничего не менять"
        assert len(conv.recent_turns) == KEEP_LAST

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


# --- _append_turns: планирование фоновой свёртки -----------------------------


def test_append_turns_triggers_compaction_at_threshold() -> None:
    from app.api import interactions

    async def _run() -> None:
        # MAX_TURNS-2 реплик: после append (+2) ровно достигаем порога
        await _seed(_make_turns(MAX_TURNS - 2), None)
        bg = Mock()

        await interactions._append_turns(bg, _USER, _SPACE, "вопрос", "ответ")

        bg.add_task.assert_called_once()
        args = bg.add_task.call_args.args
        assert args[0] is compact_conversation
        assert args[1] == _USER and args[2] == _SPACE

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


def test_append_turns_no_compaction_below_threshold() -> None:
    from app.api import interactions

    async def _run() -> None:
        await _seed(_make_turns(2), None)  # 2 + 2 = 4 < MAX_TURNS
        bg = Mock()

        await interactions._append_turns(bg, _USER, _SPACE, "вопрос", "ответ")

        bg.add_task.assert_not_called()
        conv = await _load()
        assert len(conv.recent_turns) == 4  # окно не обрезано

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


# --- симуляция 15 обменов: окно ограничено, summary непустой -----------------


class _InlineBackgroundTasks:
    """Эмулирует BackgroundTasks: вместо отложенного запуска копит таски,
    чтобы тест выполнил их синхронно сразу после реплики."""

    def __init__(self) -> None:
        self.scheduled: list = []

    def add_task(self, func, *args) -> None:
        self.scheduled.append((func, args))


def test_simulate_15_exchanges_keeps_window_bounded() -> None:
    from app.api import interactions

    async def _run() -> None:
        await _seed([], None)
        with patch(
            "app.services.memory.summarize_conversation",
            new=AsyncMock(return_value="накопленная память"),
        ):
            for i in range(15):
                bg = _InlineBackgroundTasks()
                await interactions._append_turns(bg, _USER, _SPACE, f"q{i}", f"a{i}")
                # Выполняем запланированную свёртку «в фоне» синхронно
                for func, args in bg.scheduled:
                    await func(*args)

        conv = await _load()
        assert conv.turns_count == 15
        # Окно не растёт безгранично — свёртка держит его в пределах MAX_TURNS
        assert len(conv.recent_turns) <= MAX_TURNS
        # Свёртка случилась хотя бы раз → summary непустой
        assert conv.running_summary == "накопленная память"

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())
