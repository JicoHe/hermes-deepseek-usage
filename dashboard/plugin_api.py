"""Backend half of the deepseek-usage plugin.

Balance and usage aggregation live in ``deepseek_usage.core``; this module only
exposes them to the desktop status-bar plugin over the plugin API namespace
(``/api/plugins/deepseek-usage/...``).
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from deepseek_usage import core  # noqa: E402

router = APIRouter()


@router.get("/summary")
async def summary() -> dict:
    return await run_in_threadpool(core.summary)


@router.post("/refresh")
async def refresh() -> dict:
    return await run_in_threadpool(core.summary, True)
