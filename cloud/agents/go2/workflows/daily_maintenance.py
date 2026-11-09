"""Run Go2's daily summary and trait evolution in the API process."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from cloud.agents.go2.memory import daily_summary
from cloud.agents.go2.personality import profile
from cloud.agents.go2.workflows.evolution import run_evolution_graph
from cloud.go2.models import get_text_llm
from cloud.space.execution import SYSTEM_CONTEXT, use_context

logger = logging.getLogger(__name__)


async def maintain_yesterday(today: date | None = None) -> str:
    """Use yesterday's recorded events; the profile gate makes retries safe."""
    source_date = ((today or date.today()) - timedelta(days=1)).isoformat()
    with use_context(SYSTEM_CONTEXT):
        summary = daily_summary.get_summary(source_date)
        if summary is None:
            summary = await daily_summary.generate_and_save(source_date)
        if not summary:
            return "no_events"
        if (profile.get_evolution_metadata().get("last_evolved") or "") >= source_date:
            return "existing"
        result = await run_evolution_graph(source_date, summary, model=get_text_llm())
        status = result.get("status", "error")
        if status == "error":
            logger.warning("Go2 daily trait evolution failed for %s", source_date)
        return status


async def maintenance_loop() -> None:
    """Retry on startup and hourly; each source date is committed at most once."""
    while True:
        try:
            status = await maintain_yesterday()
            if status not in {"existing", "no_events"}:
                logger.info("Go2 daily memory maintenance: %s", status)
            # Fill one older retained day per pass so old events gain summaries.
            with use_context(SYSTEM_CONTEXT):
                await daily_summary.ensure_yesterday_summary()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Go2 daily memory maintenance failed")
        await asyncio.sleep(3600)
