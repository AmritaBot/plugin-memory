"""SubconsciousRunner — 基于 ChatObject + Core Agent 框架的运行器。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from amrita.plugins.chat.utils.app import CachedUserDataRepository
from amrita.plugins.chat.utils.libchat import add_usage
from amrita.plugins.chat.utils.sql import InsightsModel
from amrita_core.base.backend import BackendSlots
from amrita_core.builtins.agent import ReActAgentStrategy
from amrita_core.chatmanager import ChatObject
from amrita_core.config import get_config
from amrita_core.preset import ModelPreset
from amrita_core.types import Message, UniResponseUsage
from amrita_core.utils import gather_usage
from amrita_sense import WorkflowInterpreter
from amrita_sense.exceptions import InterruptKeepContext
from amrita_sense.runtime.types import InterpreterContext
from jinja2 import Template
from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

from ..config import DATA_PATH, SubconsciousConfig
from ..vector import AsyncUserMemory, get_db_conn
from . import _state
from .backend import SubconsciousBackend
from .consts import (
    DEFAULT_SUBCONSCIOUS_PROMPT,
    ensure_prompt_file,
    load_character_prompt,
)
from .workflow import build_workflow

_REPO_UID = "user_00000000"


class SubconsciousRunner:
    """常驻推理循环运行器。

    使用 ChatObject 作为数据容器 + 标准 Agent 框架（ReActAgentStrategy）。
    LIMITING_MEMORY 节点在 Agent Loop 之前运行 MemoryLimiter 压缩会话消息。
    持久化使用 CachedUserDataRepository（uid=user_00000000）。
    """

    def __init__(self, config: SubconsciousConfig) -> None:
        self._config = config
        self._job_id = f"subconscious_{config.target_user_id}"
        self._is_running = False
        self._last_abstracts: list[str] = []
        self._total_runs = 0
        self._prompt_dir = DATA_PATH.parent.parent / "config" / "amrita_plugin_memory"
        self._iter_stop_result: dict[str, Any] = {}
        self._backend = SubconsciousBackend(config)
        self._workflow = build_workflow()
        self._chat_obj: ChatObject | None = None
        self._saved_dump: InterpreterContext | None = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    #  生命周期

    async def start(self) -> None:
        logger.info(
            f"[EXP Subconscious] Starting for user={self._config.target_user_id}"
        )
        await self._load_from_repo()
        run_date = datetime.now(timezone.utc) + timedelta(
            seconds=self._config.initial_delay_seconds
        )
        scheduler.add_job(
            self._run,
            trigger="date",
            run_date=run_date,
            id=self._job_id,
            misfire_grace_time=30,
        )
        logger.info(f"[EXP Subconscious] First run at {run_date.isoformat()}")

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            scheduler.remove_job(self._job_id)

    async def pause_due_to_user_activity(self) -> None:
        if self._chat_obj is not None and self._chat_obj.is_running():
            logger.info("[EXP Subconscious] Dumping interpreter & terminating")
            self._saved_dump = self._chat_obj._interpreter.dump_interpreter()
            self._chat_obj.terminate()
            return
        job = scheduler.get_job(self._job_id)
        if job is None:
            return
        delay = random.randint(
            self._config.pause_wakeup_min_seconds, self._config.pause_wakeup_max_seconds
        )
        logger.info(f"[EXP Subconscious] Pausing, wakeup in {delay}s")
        scheduler.remove_job(self._job_id)
        self._schedule_wakeup(delay)

    def _schedule_wakeup(self, delay_seconds: int) -> None:
        run_date = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        scheduler.add_job(
            self._run,
            trigger="date",
            run_date=run_date,
            id=self._job_id,
            misfire_grace_time=30,
        )

    #  核心运行

    async def _run(self) -> None:
        if self._is_running:
            logger.warning("[EXP Subconscious] Already running, skip")
            return
        self._is_running = True
        self._iter_stop_result = {}

        preset = await _state.get_preset()
        prompt_text = await self._load_prompt()
        user_msg_text = (
            f"现在是第 {self._total_runs + 1} 次潜意识推理循环。"
            f"请检查用户 (ID: {self._config.target_user_id}) 的记忆库，执行必要的整理操作。"
            f"完成后务必调用 subconscious_iter_stop。"
        )
        train = Message(role="system", content=prompt_text)
        user_input = user_msg_text

        chat_obj = ChatObject(
            train=train,
            user_input=user_input,
            preset=preset,
            session_id=f"subconscious_{self._config.target_user_id}",
            backend=BackendSlots(self._backend, self._backend),
            config=get_config(),
            agent_strategy=ReActAgentStrategy,
            exception_ignored=(InterruptKeepContext,),
        )
        self._chat_obj = chat_obj

        # 替换 workflow
        chat_obj._workflow = self._workflow
        chat_obj._interpreter = WorkflowInterpreter(
            self._workflow,
            chat_obj.io_stream,
            extra_args=chat_obj._interpreter._ava_args[1:],
            extra_kwargs=chat_obj._interpreter._ava_kwargs,
            exception_ignored=chat_obj._interpreter._exc_ignored,
        )

        if self._saved_dump is not None:
            chat_obj._interpreter.rebase_context(self._saved_dump)
            self._saved_dump = None
            logger.info("[EXP Subconscious] Resumed from saved dump")

        try:
            logger.info(f"[EXP Subconscious] Cycle #{self._total_runs + 1}")
            chat_obj.begin()
            await chat_obj
        except Exception as e:
            logger.opt(exception=e, colors=True, raw=True).exception(
                f"[EXP Subconscious] Error: {e}"
            )
        finally:
            self._chat_obj = None
            self._is_running = False

        # MemoryLimiter 在 workflow 中已经产出 chat_obj._di_memory.memory.abstract
        await self._post_process(chat_obj)

    @staticmethod
    def _is_native_thinking(preset: ModelPreset) -> bool:
        return (
            preset.thinking_config is not None
            and preset.thinking_config.thinking_type == "enabled"
        )

    #  后处理

    async def _post_process(self, chat_obj: ChatObject) -> None:
        """本轮结束后：提取摘要、更新全局 usage、持久化、调度下次运行。"""
        # 1. 更新全局 usage（复用 Bot 的 InsightsModel 统计）
        await self._update_global_usage(chat_obj)

        # 2. 提取 MemoryLimiter 产出的摘要
        mem = chat_obj._di_memory.memory
        if mem is not None and mem.abstract:
            self._last_abstracts.append(mem.abstract)
            if len(self._last_abstracts) > self._config.max_abstracts:
                self._last_abstracts.pop(0)
            logger.debug(
                f"[EXP Subconscious] Abstract saved ({len(self._last_abstracts)}/{self._config.max_abstracts})"
            )
        self._total_runs += 1
        await self._save_to_repo()
        await self._save_pending_to_repo()

        next_dt = self._resolve_next_time(self._iter_stop_result)
        scheduler.add_job(
            self._run,
            trigger="date",
            run_date=next_dt,
            id=self._job_id,
            misfire_grace_time=30,
        )
        logger.info(
            f"[EXP Subconscious] Next run: {next_dt.isoformat()} (total_runs={self._total_runs})"
        )

    @staticmethod
    async def _update_global_usage(chat_obj: ChatObject) -> None:
        """更新全局 InsightsModel usage，复用 Bot 的 add_usage 逻辑。

        chat_obj._di_resp.response 是 LLM 最终返回的 UniResponse，
        chat_obj._di_resp.extra_usage 是 MemoryLimiter 等组件累积的额外 token。
        """
        try:
            resp_state = chat_obj._di_resp
            response = resp_state.response
            if response is None:
                return
            usg = response.usage or UniResponseUsage(
                prompt_tokens=0, completion_tokens=0, total_tokens=0
            )
            usage = gather_usage(usg, resp_state.extra_usage)
            insights = await InsightsModel.get()
            add_usage(insights, usage)
            await insights.save()
            logger.debug(
                f"[EXP Subconscious] Global usage updated "
                f"(input={usage.prompt_tokens}, output={usage.completion_tokens}, "
                f"count={insights.usage_count})"
            )
        except Exception as e:
            logger.warning(f"[EXP Subconscious] Update global usage failed: {e}")

    def _resolve_next_time(self, stop_result: dict[str, Any]) -> datetime:
        if stop_result.get("next_time"):
            try:
                return datetime.fromisoformat(str(stop_result["next_time"]))
            except (ValueError, TypeError):
                pass
        if stop_result.get("delay_seconds"):
            try:
                return datetime.now(timezone.utc) + timedelta(
                    seconds=int(stop_result["delay_seconds"])
                )
            except (ValueError, TypeError):
                pass
        return datetime.now(timezone.utc) + timedelta(
            seconds=self._config.default_interval_seconds
        )

    #  CachedUserDataRepository 持久化

    async def _load_from_repo(self) -> None:
        """从 CachedUserDataRepository 恢复状态。"""
        try:
            repo = CachedUserDataRepository()
            mem = await repo.get_memory(0, False, uid=_REPO_UID)
            # extra_prompt 存 JSON 元状态
            if mem.extra_prompt:
                data = json.loads(mem.extra_prompt)
                self._total_runs = int(data.get("total_runs", 0))
                self._last_abstracts = data.get("last_abstracts", [])
                if isinstance(self._last_abstracts, list):
                    self._last_abstracts = [str(a) for a in self._last_abstracts][
                        : self._config.max_abstracts
                    ]
                else:
                    self._last_abstracts = []
                # 恢复待发送消息
                pending = data.get("pending_messages", [])
                if isinstance(pending, list):
                    _state.set_pending(pending)
            # memory_json.abstract 用于 Core Jinja2 模板的 <SUMMARY> 注入
            if mem.memory_json.abstract:
                # 确保 abstract 也在 last_abstracts 中（兜底）
                if (
                    not self._last_abstracts
                    or self._last_abstracts[-1] != mem.memory_json.abstract
                ):
                    self._last_abstracts.append(mem.memory_json.abstract)
                    if len(self._last_abstracts) > self._config.max_abstracts:
                        self._last_abstracts.pop(0)
        except Exception as e:
            logger.warning(f"[EXP Subconscious] Load from repo failed: {e}")

    async def _save_to_repo(self) -> None:
        """保存状态到 CachedUserDataRepository。"""
        try:
            repo = CachedUserDataRepository()
            mem = await repo.get_memory(0, False, uid=_REPO_UID)
            latest_abstract = self._last_abstracts[-1] if self._last_abstracts else ""
            mem.memory_json.abstract = latest_abstract
            mem.extra_prompt = json.dumps(
                {
                    "total_runs": self._total_runs,
                    "last_run_time": datetime.now(timezone.utc).isoformat(),
                    "last_abstracts": self._last_abstracts,
                },
                ensure_ascii=False,
            )
            await repo.update_memory_data(mem)
        except Exception as e:
            logger.opt(exception=e, colors=True, raw=True).exception(
                f"[EXP Subconscious] Save to repo failed: {e}"
            )

    async def _save_pending_to_repo(self) -> None:
        """持久化待发送消息到 CachedUserDataRepository 的 extra_prompt 中。"""
        try:
            repo = CachedUserDataRepository()
            mem = await repo.get_memory(0, False, uid=_REPO_UID)
            existing: dict[str, Any] = {}
            if mem.extra_prompt:
                try:
                    existing = json.loads(mem.extra_prompt)
                except json.JSONDecodeError:
                    pass
            existing["pending_messages"] = _state.get_pending()
            mem.extra_prompt = json.dumps(existing, ensure_ascii=False)
            await repo.update_memory_data(mem)
        except Exception as e:
            logger.opt(exception=e, colors=True, raw=True).exception(
                f"[EXP Subconscious] Save pending failed: {e}"
            )

    #  Prompt 加载

    async def _load_prompt(self) -> str:
        prompt_path = (self._prompt_dir / self._config.prompt_file).resolve()
        ensure_prompt_file(prompt_path, DEFAULT_SUBCONSCIOUS_PROMPT)

        # 加载 Bot 角色设定（从 PromptStore 的 private_prompts）
        character_prompt = await load_character_prompt()

        template = Template(prompt_path.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc)
        prompt = await asyncio.to_thread(
            template.render,
            last_abstracts=self._last_abstracts,
            last_run=(self._last_abstracts[-1] if self._last_abstracts else ""),
            current_time=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            target_user_id=self._config.target_user_id or "(未配置)",
            total_runs=self._total_runs,
            character_prompt=character_prompt,
        )

        # Phase 3: 膨胀感知 — 查 ChromaDB 总量，超阈值注入警告
        try:
            pid = f"user_{self._config.target_user_id}"
            ope = AsyncUserMemory(get_db_conn())
            await ope.init()
            result = await ope.get_all_notes(pid, include=["metadatas"])
            if result and result.get("ids"):
                total = len(result["ids"])
                if total > self._config.memory_warn_threshold:
                    low_count = sum(
                        1
                        for m in (result.get("metadatas") or [])
                        if isinstance(m, dict) and m.get("importance") == "low"
                    )
                    prompt += (
                        f"\n\n⚠️ 当前记忆库有 {total} 条记忆"
                        f"（其中低重要性 {low_count} 条）。"
                        f"建议本轮优先调用 subconscious_get_memory_stats 分析分布，"
                        f"然后用 subconscious_duplicate_helper 获取待整理列表，"
                        f"进行合并去重和低质记忆清理。"
                    )
        except Exception as e:
            logger.debug(f"[EXP Subconscious] Memory stats query failed: {e}")

        return prompt

    #  持久化 helpers — 现在由 _save_to_repo 统一处理
