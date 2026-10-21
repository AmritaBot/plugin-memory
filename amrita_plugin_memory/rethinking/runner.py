"""SubconsciousRunner — 基于 ChatObject + Core Agent 框架的运行器。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from amrita.plugins.chat.utils.libchat import add_usage
from amrita_core import SuspendObjectStream
from amrita_core.base.backend import BackendSlots
from amrita_core.builtins.agent import ReActAgentStrategy
from amrita_core.builtins.workflows import REACT_ONLY
from amrita_core.chatmanager import ChatObject
from amrita_core.components.compaction import ContextCompactor
from amrita_core.config import AmritaConfig, get_config
from amrita_core.preset import ModelPreset
from amrita_core.types import CONTENT_LIST_TYPE, Message, UniResponseUsage
from amrita_core.utils import gather_usage
from jinja2 import Template
from nonebot import logger
from nonebot_plugin_amrita.cache import LRUCache
from nonebot_plugin_amrita.database import InsightsModel
from nonebot_plugin_amrita.memory import CachedUserDataRepository, MemorySessionsSchema
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from ..config import DATA_PATH, SubconsciousConfig
from ..keys import make_scope_id
from ..models import SubconsciousState
from ..profile_store import PROFILE_PATH, ProfileResult, read_profile
from . import _state
from .backend import SubconsciousBackend
from .consts import (
    DEFAULT_SUBCONSCIOUS_PROMPT,
    GUIDE_OF_KNOWLEDGES,
    PROFILE_BUILDING_GUIDE,
    PROMPT_README,
    ensure_prompt_file,
    load_character_prompt,
)
from .knowledge import KnowledgeBaseManager
from .schedule import IdlePlanner, describe
from .types import SessionSummary

_STATE_UID = "amrita_memory"
#: 重排节流窗口（秒）
_REPLAN_THROTTLE_SECONDS = 60.0
_LEGACY_REPO_UID = (
    "amrita_memory"  # 旧版本元状态占用的 Bot Memory 表 uid（仅用于升级迁移）
)

# 工作流 = Core 内置 REACT_ONLY（已包含上下文压缩节点），不再自建
_WORKFLOW = REACT_ONLY.render()


async def _ign_cb(*_, **__): ...


class SubconsciousRunner:
    """常驻推理循环运行器。

    使用 ChatObject 作为数据容器 + 标准 Agent 框架（ReActAgentStrategy）。
    工作流直接复用 Core 的 REACT_ONLY，其中的 COMPACT_HISTORY 节点在 Agent
    Loop 之前压缩会话消息。元状态持久化使用独立的 SubconsciousState 表。
    """

    def __init__(self, config: SubconsciousConfig) -> None:
        self._config = config
        self._job_id = f"subconscious_{config.target_user_id}"
        self._is_running = False
        self._manual_task: asyncio.Task | None = None
        self._last_abstracts: list[str] = []
        self._total_runs = 0
        self._prompt_dir = DATA_PATH.parent.parent / "config" / "amrita_plugin_memory"
        self._backend = SubconsciousBackend(config)
        self._chat_obj: ChatObject | None = None
        self._kb_manager: KnowledgeBaseManager | None = None
        # session 摘要缓存（LRU）：session DB id -> 摘要文本，最多 128 条
        self._session_cache: LRUCache[int, str] = LRUCache(128)
        self._profile_path = PROFILE_PATH
        # 空闲窗口规划器（热度图 + 冷段二分）
        self._planner = IdlePlanner(config)
        # 重排节流：一分钟内重复触发只更新惩罚与热度，不重复规划
        self._last_replan_at: float = 0.0
        self._replan_throttle: float = _REPLAN_THROTTLE_SECONDS
        # 状态写入串行化：并发 save 先后到达时，旧快照不能盖掉新快照
        self._state_lock = asyncio.Lock()
        # 正在跑的周期任务，用户新消息到来时可以掐断
        self._cycle_task: asyncio.Task | None = None
        # 本轮是否已取过上下文，供 iter_stop 校验
        _state.reset_context_fetched()
        _state.reset_cycle_valid()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def target_user_id(self) -> str:
        return self._config.target_user_id

    def _build_config(self) -> AmritaConfig:
        """构建自定义 AmritaConfig —— 从全局拷贝并覆写本插件关心的字段。"""
        cfg = get_config().model_copy(deep=True)
        cfg.builtin.loop_reasoning_trigger = self._config.loop_detect_threshold
        # Core 1.x 用 context_strategy 表达压缩策略，bool 开关映射到 compact/none
        cfg.llm.context_strategy = (
            "compact" if self._config.enable_memory_compress else "none"
        )
        return cfg

    async def start(self) -> None:
        logger.info(f"[Subconscious] Starting for user={self._config.target_user_id}")
        await self._load_state()
        # 确保 prompt 目录和 README 存在
        readme_path = self._prompt_dir / "prompt" / "README.md"
        ensure_prompt_file(readme_path, PROMPT_README)
        # 初始化全局知识库（参数从配置文件读取）
        if self._config.enable_knowledge:
            self._kb_manager = KnowledgeBaseManager(
                DATA_PATH,
                collection_name=self._config.knowledge_collection_name,
                max_chars=self._config.knowledge_max_chars,
            )
            await self._kb_manager.init()
            await self._kb_manager.validate_on_startup()
        else:
            logger.info("[Subconscious] Knowledge base disabled via config")
        logger.info("[Subconscious] Idle — waiting for user chat to trigger first run")

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            scheduler.remove_job(self._job_id)
        if self._manual_task is not None and not self._manual_task.done():
            self._manual_task.cancel()

    async def cancel_and_reschedule(self, chat_ts: float | None = None) -> None:
        """取消当前计划并重新规划下一个空闲窗口。

        用户每次发消息时调用：先给热度图记一次观测，再递增惩罚，
        然后按新的冷段门槛重新选窗口。不 dump/恢复上下文——
        用户聊完天后有新内容，旧推理上下文应直接丢弃。
        """
        # 先记下历史是否存在：首条消息会把热度图从空变成非空，
        # 若观测后再判断，冷启动就会被自己这条消息绕过
        had_history = True
        with contextlib.suppress(Exception):
            had_history = self._planner.has_history()
            self._planner.observe(chat_ts)
        penalty = _state.increment_penalty()

        # 用户在聊天，正在跑的那一轮已经过时了，掐掉避免它把旧结论写回记忆
        self._abort_running_cycle()

        now_mono = time.monotonic()
        if now_mono - self._last_replan_at < self._replan_throttle:
            # 同窗口内的后续消息不重排，但仍把待执行任务往后挪，
            # 否则旧任务会在用户刚开始的这轮活动里跑起来
            self._defer_pending_job(penalty, had_history)
            await self._save_state_logged()
            return
        self._last_replan_at = now_mono
        with contextlib.suppress(Exception):
            scheduler.remove_job(self._job_id)

        delay_seconds = self._compute_delay(penalty, had_history)
        self._schedule_once(delay_seconds)
        # 惩罚计数与热度图落盘，避免重启后丢掉已累积的退避
        await self._save_state_logged()

    def _compute_delay(self, penalty: int, had_history: bool) -> int:
        """算下一次运行要等多久（秒）。规划失败时退到保底窗口。"""
        base = self._config.rethink_base_delay_minutes
        cap = self._config.rethink_max_delay_minutes
        if not self._config.use_idle_window_planner:
            multiplier = self._config.rethink_penalty_multiplier
            raw = base * (multiplier ** min(max(penalty - 1, 0), 32))
            delay_minutes = min(raw, cap)
            logger.info(
                f"[Subconscious] Rescheduled(legacy): penalty=#{penalty}, "
                f"delay={delay_minutes:.0f}min (cap={cap}min)"
            )
            return int(delay_minutes * 60)

        try:
            if had_history:
                plan = self._planner.plan(penalty)
            else:
                # 冷启动只攒到一条样本，不足以判断作息，先走保底窗口
                plan = self._planner.fallback_plan(penalty)
        except Exception as e:
            logger.opt(exception=e, colors=True, raw=True).error(
                f"[Subconscious] Plan failed, fall back to fixed delay: {e}"
            )
            return int(min(base, cap) * 60)
        logger.info(f"[Subconscious] Rescheduled: penalty=#{penalty}, {describe(plan)}")
        return int(max(60.0, (plan.start - datetime.now(timezone.utc)).total_seconds()))

    def _defer_pending_job(self, penalty: int, had_history: bool) -> None:
        """节流期内不重新规划，只把待执行任务顺延到新的候选窗口。"""
        try:
            job = scheduler.get_job(self._job_id)
            if job is None:
                # 本就没有待执行任务（可能在运行中），补一个
                self._schedule_once(self._compute_delay(penalty, had_history))
                return
            delay = self._compute_delay(penalty, had_history)
            run_date = datetime.now(timezone.utc) + timedelta(seconds=delay)
            if job.next_run_time is not None and job.next_run_time < run_date:
                scheduler.reschedule_job(
                    self._job_id, trigger="date", run_date=run_date
                )
        except Exception as e:
            logger.warning(f"[Subconscious] Defer pending job failed: {e}")

    def _abort_running_cycle(self) -> None:
        """用户发了新消息，掐断正在跑的那一轮。"""
        task = self._cycle_task
        if task is None or task.done():
            return
        logger.info("[Subconscious] New chat arrived, aborting in-flight cycle")
        task.cancel()

    async def _save_state_logged(self) -> None:
        """保存状态，失败要出声：写盘失败会让重启后的退避凭空消失。"""
        try:
            await self._save_state()
        except Exception as e:
            logger.warning(f"[Subconscious] Save state failed: {e}")

    def _schedule_once(self, delay_seconds: int) -> None:
        run_date = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        scheduler.add_job(
            self._run,
            trigger="date",
            run_date=run_date,
            id=self._job_id,
            misfire_grace_time=30,
            replace_existing=True,
        )

    async def trigger_now(self) -> None:
        """手动触发：取消待执行任务并立刻跑一轮，不阻塞调用方。"""
        with contextlib.suppress(Exception):
            scheduler.remove_job(self._job_id)
        if self._manual_task is not None and not self._manual_task.done():
            self._manual_task.cancel()
        # 持有引用，避免任务被 GC 回收
        self._manual_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """调度入口 — 保证任何异常路径都会释放运行标志。"""
        if self._is_running:
            logger.warning("[Subconscious] Already running, skip")
            return
        self._is_running = True
        self._cycle_task = asyncio.current_task()
        self._context_fetched = False
        try:
            # 窗口已实际占用：记一次观测，避免下一轮重复选同一段
            with contextlib.suppress(Exception):
                self._planner.note_window_taken(time.time())
            await self._run_cycle()
        finally:
            self._is_running = False

    async def _run_cycle(self) -> None:
        try:
            preset = await _state.get_preset()
        except Exception as e:
            logger.opt(exception=e, colors=True, raw=True).error(
                f"[Subconscious] No usable preset, skip cycle: {e}"
            )
            return
        prompt_text = await self._load_prompt()
        user_msg_text = (
            f"请先调用 subconscious_get_context 了解当前环境，"
            f"再检查用户 (ID: {self._config.target_user_id}) 的记忆库，"
            f"执行必要的整理操作。完成后务必调用 subconscious_iter_stop。"
        )
        train = Message(role="system", content=prompt_text)
        user_input = user_msg_text

        io_stream = SuspendObjectStream(callback=_ign_cb)

        chat_obj = ChatObject(
            train=train,
            user_input=user_input,
            preset=preset,
            session_id=f"subconscious_{self._config.target_user_id}",
            backend=BackendSlots(self._backend, self._backend),
            config=self._build_config(),
            agent_strategy=ReActAgentStrategy,
            io_stream=io_stream,
            workflow=_WORKFLOW,
        )
        self._chat_obj = chat_obj

        try:
            logger.info("[Subconscious] Cycle start")
            chat_obj.begin()
            await chat_obj
        except asyncio.CancelledError:
            # 用户发来新消息把本轮掐了：旧上下文作废，不写摘要也不动惩罚
            logger.info("[Subconscious] Cycle aborted by new user message")
            self._chat_obj = None
            raise
        except Exception as e:
            logger.opt(exception=e, colors=True, raw=True).exception(
                f"[Subconscious] Error: {e}"
            )
        finally:
            self._chat_obj = None

        # 压缩节点已在 workflow 中把摘要写入 chat_obj._di_memory.memory.abstract
        await self._post_process(chat_obj)

    @staticmethod
    def _is_native_thinking(preset: ModelPreset) -> bool:
        return (
            preset.thinking_config is not None
            and preset.thinking_config.thinking_type == "enabled"
        )

    async def _post_process(self, chat_obj: ChatObject) -> None:
        """本轮结束后：提取摘要、更新全局 usage、持久化、调度下次运行。"""
        # 1. 更新全局 usage（复用 Bot 的 InsightsModel 统计）
        await self._update_usage(chat_obj)

        # 2. 提取压缩节点产出的摘要
        mem = chat_obj._di_memory.memory
        if mem is not None and mem.abstract:
            self._last_abstracts.append(mem.abstract)
            if len(self._last_abstracts) > self._config.max_abstracts:
                self._last_abstracts.pop(0)
            logger.debug(
                f"[Subconscious] Abstract saved ({len(self._last_abstracts)}/{self._config.max_abstracts})"
            )
        self._total_runs += 1
        _state.set_last_run_at(datetime.now(timezone.utc).timestamp())

        # 先持久化，任一失败则不重置惩罚（保留重试机会）。
        # 记下快照时的计数：保存期间用户可能又发消息把惩罚顶上去，
        # 那部分增量不能被这次 reset 抹掉。
        penalty_before = _state.get_penalty_count()
        save_ok = True
        try:
            await self._save_state()
        except Exception as e:
            logger.warning(f"[Subconscious] Save state failed: {e}")
            save_ok = False

        if not save_ok:
            logger.warning(
                f"[Subconscious] Cycle done but save failed, "
                f"penalty NOT reset (penalty={_state.get_penalty_count()})"
            )
        elif not _state.is_cycle_valid():
            # 空转周期（没取上下文就收工）不清惩罚，保留压力等下一轮
            logger.warning("[Subconscious] Cycle invalid, penalty kept")
        elif _state.get_penalty_count() != penalty_before:
            # 保存期间用户又发了消息，新增的那部分惩罚不能被这次收尾抹掉
            logger.info(
                "[Subconscious] Penalty kept: new chat arrived during the cycle"
            )
        else:
            _state.reset_penalty()
            # 惩罚清零也要落盘，否则重启会加载旧退避
            try:
                await self._save_state()
            except Exception as e:
                logger.warning(f"[Subconscious] Penalty reset not persisted: {e}")
            logger.info(
                f"[Subconscious] Cycle done (total_runs={self._total_runs}), "
                f"penalty reset, waiting for next user chat"
            )

    async def _update_usage(self, chat_obj: ChatObject) -> None:
        """更新全局 InsightsModel 与目标用户的 usage，复用 Bot 的 add_usage 逻辑。

        chat_obj._di_resp.response 是 LLM 最终返回的 UniResponse，
        chat_obj._di_resp.extra_usage 是压缩等组件累积的额外 token。
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
                f"[Subconscious] Global usage updated "
                f"(input={usage.prompt_tokens}, output={usage.completion_tokens}, "
                f"count={insights.usage_count})"
            )
            await self._add_user_usage(usage)
        except Exception as e:
            logger.warning(f"[Subconscious] Update usage failed: {e}")

    async def _add_user_usage(self, usage: UniResponseUsage[int]) -> None:
        """把本轮 token 记到目标用户的元数据，与用户自己聊天的统计同一口径。"""
        uid = self._config.target_user_id
        if not uid:
            return
        try:
            repo = CachedUserDataRepository()
            meta = await repo.get_metadata(make_scope_id(uid, is_group=False))
            add_usage(meta, usage)
            await repo.update_metadata(meta)
            logger.debug(
                f"[Subconscious] User usage updated (uid={uid}, "
                f"input={usage.prompt_tokens}, output={usage.completion_tokens})"
            )
        except Exception as e:
            logger.warning(f"[Subconscious] Update user usage failed: {e}")

    @staticmethod
    async def _read_state_payload(uid: str) -> dict[str, Any]:
        async with get_session() as session:
            row = (
                await session.execute(
                    select(SubconsciousState).where(SubconsciousState.uid == uid)
                )
            ).scalar_one_or_none()
        if row is None:
            return {}
        try:
            return json.loads(row.payload)
        except json.JSONDecodeError:
            return {}

    @staticmethod
    async def _write_state_payload(uid: str, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        async with get_session() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(SubconsciousState).where(SubconsciousState.uid == uid)
                    )
                ).scalar_one_or_none()
                if row is None:
                    session.add(SubconsciousState(uid=uid, payload=text))
                else:
                    row.payload = text
                    row.updated_at = datetime.now()

    async def _load_state(self) -> None:
        """从 SubconsciousState 表恢复状态，必要时迁移旧版本数据。"""
        try:
            payload = await self._read_state_payload(_STATE_UID)
            if not payload:
                payload = await self._migrate_legacy_state()
            if not payload:
                return
            self._total_runs = int(payload.get("total_runs", 0))
            _state.set_penalty_count(int(payload.get("penalty", 0)))
            abstracts = payload.get("last_abstracts", [])
            self._last_abstracts = (
                [str(a) for a in abstracts][: self._config.max_abstracts]
                if isinstance(abstracts, list)
                else []
            )
            pending = payload.get("pending_messages", [])
            if isinstance(pending, list):
                _state.set_pending(pending)
            suggestions = payload.get("knowledge_suggestions", [])
            if isinstance(suggestions, list):
                _state.set_knowledge_suggestions(suggestions)
            _state.set_last_iter_summary(str(payload.get("last_iter_summary", "")))
            _state.set_last_run_at(self._read_last_run_ts(payload))
            self._planner.load(payload)
            # 旧 penalty 语义是"被推迟的次数"，与新语义（要求的冷段长度）不可换算，
            # 升级后清零一次，避免带着旧值把门槛顶到上限。
            # 仍走旧调度时数值是自洽的，别动它。
            if (
                self._config.use_idle_window_planner
                and "idle_planner" not in payload
                and _state.get_penalty_count() > 0
            ):
                logger.info(
                    "[Subconscious] Migrate: reset legacy penalty "
                    f"#{_state.get_penalty_count()} (old delay-based semantics)"
                )
                _state.reset_penalty()
        except Exception as e:
            logger.warning(f"[Subconscious] Load state failed: {e}")

    @staticmethod
    def _read_last_run_ts(payload: dict[str, Any]) -> float:
        """取上轮运行时间，兼容 0.7.1 只写 last_run_time 的旧快照。"""
        raw = payload.get("last_run_ts")
        try:
            if raw is not None:
                return float(raw or 0.0)
        except (TypeError, ValueError):
            pass
        legacy = payload.get("last_run_time")
        if isinstance(legacy, str) and legacy:
            try:
                return datetime.fromisoformat(legacy).timestamp()
            except ValueError:
                return 0.0
        return 0.0

    async def _migrate_legacy_state(self) -> dict[str, Any]:
        """迁移旧版本挪用的 Bot Memory 表元状态（extra_prompt/memory_json）。"""
        payload: dict[str, Any] = {}
        try:
            repo = CachedUserDataRepository()
            mem = await repo.get_memory(_LEGACY_REPO_UID)
            if mem.extra_prompt:
                data: dict[str, Any] = json.loads(mem.extra_prompt)
                payload["total_runs"] = int(data.get("total_runs", 0))
                abstracts = data.get("last_abstracts", [])
                payload["last_abstracts"] = (
                    [str(a) for a in abstracts][: self._config.max_abstracts]
                    if isinstance(abstracts, list)
                    else []
                )
                pending = data.get("pending_messages", [])
                if isinstance(pending, list):
                    payload["pending_messages"] = pending
                suggestions = data.get("knowledge_suggestions", [])
                if isinstance(suggestions, list):
                    payload["knowledge_suggestions"] = suggestions
            if mem.memory_json.abstract and not payload.get("last_abstracts"):
                payload["last_abstracts"] = [mem.memory_json.abstract]
        except Exception as e:
            logger.warning(f"[Subconscious] Legacy state migrate failed: {e}")
            return {}
        if payload:
            await self._write_state_payload(_STATE_UID, payload)
            logger.info("[Subconscious] Migrated legacy state to SubconsciousState")
        return payload

    async def _save_state(self) -> None:
        """保存元状态到 SubconsciousState 表。

        加锁串行化：并发调用时若旧快照后提交，会把新的热度与惩罚盖掉。
        """
        async with self._state_lock:
            await self._save_state_locked()

    async def _save_state_locked(self) -> None:
        """真正写盘的部分，调用方需已持有 _state_lock。"""
        payload: dict[str, Any] = {
            "total_runs": self._total_runs,
            "last_run_time": datetime.now(timezone.utc).isoformat(),
            "last_run_ts": _state.get_last_run_at(),
            "last_iter_summary": _state.get_last_iter_summary(),
            "penalty": _state.get_penalty_count(),
            "last_abstracts": self._last_abstracts,
            "pending_messages": _state.get_pending(),
            "knowledge_suggestions": _state.get_knowledge_suggestions(),
        }
        # 只有启用空闲窗口调度时才写热度图：旧调度模式下写进去，
        # 会让下次升级的迁移判断误以为已是新格式，跳过旧 penalty 清零
        if self._config.use_idle_window_planner:
            payload.update(self._planner.dump())
        # 整行替换前先补齐老版本实例写过、本实例不认识的字段，
        # 否则旧实例一存盘，热度图累积就被抹成"从没规划过"
        try:
            existing = await self._read_state_payload(_STATE_UID)
        except Exception:
            existing = {}
        for key, value in existing.items():
            if key not in payload:
                payload[key] = value
        await self._write_state_payload(_STATE_UID, payload)

    async def _load_prompt(self) -> str:
        """渲染 system prompt。

        只允许不变量进入 system：角色、规则、质量口径、输出格式，
        以及静态的画像/知识库指南。精确时间、上轮摘要、待办计数、
        记忆库规模等动态信息一律交给 subconscious_get_context 按需拉取，
        这样跨轮的 system 前缀逐字节一致，能吃到 provider 的前缀缓存。
        """
        main_path = (self._prompt_dir / self._config.prompt_file).resolve()
        kn_path = (self._prompt_dir / self._config.prompt_knowledge_file).resolve()
        pr_path = (self._prompt_dir / self._config.prompt_profile_file).resolve()
        ensure_prompt_file(main_path, DEFAULT_SUBCONSCIOUS_PROMPT)
        ensure_prompt_file(kn_path, GUIDE_OF_KNOWLEDGES)
        ensure_prompt_file(pr_path, PROFILE_BUILDING_GUIDE)

        character_prompt = await load_character_prompt()

        template: Template = Template(main_path.read_text(encoding="utf-8"))
        # 日粒度日期：每天才变一次，不影响缓存命中，又能给记忆新旧提供锚点
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prompt = await asyncio.to_thread(
            template.render,
            current_date=current_date,
            target_user_id=self._config.target_user_id or "(未配置)",
            character_prompt=character_prompt,
        )

        # 指南文件仍走 Jinja2，但只提供不变量：旧文件里的 {{ current_time }}
        # 会被渲染成空串，而不是原样泄漏到 system 里
        static_vars = {
            "current_date": current_date,
            "target_user_id": self._config.target_user_id or "(未配置)",
        }
        for guide in (kn_path, pr_path):
            prompt += "\n\n" + await asyncio.to_thread(
                Template(guide.read_text(encoding="utf-8")).render, **static_vars
            )
        return prompt

    async def _read_recent_sessions(self, n: int = 5) -> list[SessionSummary]:
        """读取目标用户最近 N 个归档 sessions，按需生成摘要并缓存。"""
        try:
            uid = make_scope_id(self._config.target_user_id, is_group=False)
            repo = CachedUserDataRepository()
            raw = await repo.get_sesssions(uid)
            if not raw:
                return []
            recent = sorted(raw, key=lambda s: s.created_at, reverse=True)[:n]
            result: list[SessionSummary] = []
            for s in recent:
                sid = s.id if s.id is not None else 0
                data = getattr(s, "data", None)
                raw_messages: list[dict[str, object]] = (
                    getattr(data, "messages", []) or [] if data is not None else []
                )
                msg_count = len(raw_messages)
                if sid in self._session_cache:
                    # 标记为最近使用并读取
                    result.append(
                        SessionSummary(
                            id=sid,
                            created_at=s.created_at,
                            summary=self._session_cache[sid],
                            message_count=msg_count,
                        )
                    )
                else:
                    summary = await self._summarize_session(s)
                    self._session_cache.put(sid, summary)
                    result.append(
                        SessionSummary(
                            id=sid,
                            created_at=s.created_at,
                            summary=summary,
                            message_count=msg_count,
                        )
                    )
            return result
        except Exception as e:
            logger.warning(f"[Subconscious] Read sessions failed: {e}")
            return []

    async def _summarize_session(self, session: MemorySessionsSchema) -> str:
        """用 Core 的 ContextCompactor 生成单个 session 的全会话摘要。"""
        data = session.data
        if data is None:
            return "空会话"
        raw_messages: list[dict[str, object]] = getattr(data, "messages", []) or []
        if not raw_messages:
            return "空会话"

        created_at: float = getattr(session, "created_at", 0.0)
        try:
            dt = datetime.fromtimestamp(created_at, tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            date_str = str(created_at)

        messages: CONTENT_LIST_TYPE = [
            Message.model_validate(m) for m in raw_messages if isinstance(m, dict)
        ]
        if not messages:
            return "空会话"
        abstract = await ContextCompactor(config=get_config()).summarize(messages)
        return f"[{date_str}] {abstract}" if abstract else f"[{date_str}] 无法生成摘要"

    async def _read_profile(
        self, start_line: int | None = None, end_line: int | None = None
    ) -> ProfileResult:
        """读取用户画像 Markdown 文件，支持行数滑动窗口。"""
        return read_profile(self._profile_path, start_line, end_line)

    async def _update_profile(
        self,
        summary: str,
        new_lines: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> None:
        """增量更新用户画像。

        省略 start_line/end_line -> 追加到末尾。
        指定时替换 [start_line, end_line) 之间的行。
        """
        # 读取现有正文
        existing_body = ""
        if self._profile_path.exists():
            text = self._profile_path.read_text(encoding="utf-8")
            parts = text.split("---", 1)
            existing_body = parts[1] if len(parts) > 1 else ""

        body_lines = existing_body.split("\n") if existing_body else []
        new = new_lines.split("\n")
        if start_line is None or end_line is None:
            new_body_lines = body_lines + new
            operation = f"append {len(new)} lines"
        else:
            s = max(0, start_line)
            e = max(s, min(len(body_lines), end_line))
            new_body_lines = body_lines[:s] + new + body_lines[e:]
            operation = f"replace [{s}:{e}] -> [{s}:{s + len(new)}]"
        new_body = "\n".join(new_body_lines)

        self._profile_path.parent.mkdir(parents=True, exist_ok=True)
        content = f"{summary}\n---\n{new_body}"
        self._profile_path.write_text(content, encoding="utf-8")
        logger.info(
            f"[Subconscious] Profile updated: {operation}, summary={summary[:60]}..."
        )
