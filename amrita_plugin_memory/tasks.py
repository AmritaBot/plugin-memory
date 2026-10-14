"""时间退化的后台调度：周期执行分层清理，并把入口暴露给 CLI / 命令。"""

from __future__ import annotations

import contextlib

from nonebot import get_driver, logger
from nonebot_plugin_apscheduler import scheduler

from .config import DataManager
from .vector import AsyncUserMemory, get_db_conn

#: 分层清理任务的 job id，重复注册时先移除
SWEEP_JOB_ID = "amrita_memory_decay_sweep"


async def run_decay_sweep() -> dict[str, int]:
    """执行一次分层清理，返回统计。未启用时直接跳过。"""
    policy = (await DataManager().safe_get_config()).decay
    empty = {"scanned": 0, "demoted": 0, "purged": 0, "kept": 0}
    if not policy.enabled or not policy.sweep_enabled:
        logger.debug("[MemoryDecay] sweep skipped: disabled")
        return empty
    ope = AsyncUserMemory(get_db_conn())
    await ope.init()
    summary = await ope.sweep_decay(policy)
    logger.info(f"[MemoryDecay] sweep done: {summary}")
    return summary


@get_driver().on_startup
async def _setup_decay_scheduler() -> None:
    """按配置注册周期清理任务；关掉就移除旧任务。"""
    policy = (await DataManager().safe_get_config()).decay
    with contextlib.suppress(Exception):
        scheduler.remove_job(SWEEP_JOB_ID)
    if not policy.enabled or not policy.sweep_enabled:
        logger.info("[MemoryDecay] sweep disabled via config")
        return
    scheduler.add_job(
        run_decay_sweep,
        trigger="interval",
        hours=policy.sweep_interval_hours,
        id=SWEEP_JOB_ID,
        misfire_grace_time=300,
    )
    logger.info(f"[MemoryDecay] sweep scheduled every {policy.sweep_interval_hours}h")
