"""SubconsciousRunner — 基于 ChatObject + Core Agent 框架的运行器。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from amrita.plugins.chat.utils.libchat import add_usage
from amrita_core import SuspendObjectStream
from amrita_core.base.backend import BackendSlots
from amrita_core.builtins.agent import ReActAgentStrategy
from amrita_core.builtins.workflows import REACT_BLOCK
from amrita_core.chatmanager import ChatObject
from amrita_core.components.llm import JINJA2_RENDER
from amrita_core.components.process import BUILD_MESSAGE, LOAD_STATE
from amrita_core.config import AmritaConfig, get_config
from amrita_core.preset import ModelPreset
from amrita_core.types import Message, UniResponseUsage
from amrita_core.utils import gather_usage
from jinja2 import Template
from nonebot import logger
from nonebot_plugin_amrita.cache import LRUCache
from nonebot_plugin_amrita.database import InsightsModel
from nonebot_plugin_amrita.memory import CachedUserDataRepository, MemorySessionsSchema
from nonebot_plugin_apscheduler import scheduler

from ..config import DATA_PATH, SubconsciousConfig
from ..vector import AsyncUserMemory, get_db_conn
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
from .nodes import LIMITING_MEMORY
from .types import ProfileResult, SessionSummary

_REPO_UID = "amrita_memory"

# 工作流 = 加载状态 → Jinja2 渲染 → 记忆限幅 → 构建消息 → ReAct 循环
_WORKFLOW = (
    LOAD_STATE >> JINJA2_RENDER >> LIMITING_MEMORY >> BUILD_MESSAGE >> REACT_BLOCK
).render()


async def _ign_cb(*_, **__): ...


class SubconsciousRunner:
    """常驻推理循环运行器。

    使用 ChatObject 作为数据容器 + 标准 Agent 框架（ReActAgentStrategy）。
    LIMITING_MEMORY 节点在 Agent Loop 之前运行 MemoryLimiter 压缩会话消息。
    持久化使用 CachedUserDataRepository（amrita_memory）。
    """

    def __init__(self, config: SubconsciousConfig) -> None:
        self._config = config
        self._job_id = f"subconscious_{config.target_user_id}"
        self._is_running = False
        self._last_abstracts: list[str] = []
        self._total_runs = 0
        self._prompt_dir = DATA_PATH.parent.parent / "config" / "amrita_plugin_memory"
        self._backend = SubconsciousBackend(config)
        self._chat_obj: ChatObject | None = None
        self._kb_manager: KnowledgeBaseManager | None = None
        # session 摘要缓存（LRU）：session DB id → 摘要文本，最多 128 条
        self._session_cache: LRUCache[int, str] = LRUCache(128)
        # 用户画像文件
        self._profile_path = DATA_PATH / "user_profile.md"

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _build_config(self) -> AmritaConfig:
        """构建自定义 AmritaConfig —— 从全局拷贝并覆写本插件关心的字段。"""
        cfg = get_config().model_copy(deep=True)
        cfg.builtin.loop_reasoning_trigger = self._config.loop_detect_threshold
        cfg.llm.enable_memory_abstract = self._config.enable_memory_compress
        return cfg

    #  生命周期

    async def start(self) -> None:
        logger.info(f"[Subconscious] Starting for user={self._config.target_user_id}")
        await self._load_from_repo()
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

    async def cancel_and_reschedule(self) -> None:
        """取消当前计划，递增惩罚计数器，重新计算延迟并调度一次运行。

        用户每次发消息时调用此方法。不 dump/恢复上下文——
        用户聊完天后有新内容，旧推理上下文应直接丢弃。
        """
        with contextlib.suppress(Exception):
            scheduler.remove_job(self._job_id)
        penalty = _state.increment_penalty()
        base = self._config.rethink_base_delay_minutes
        multiplier = self._config.rethink_penalty_multiplier
        cap = self._config.rethink_max_delay_minutes
        raw = base * (multiplier ** (penalty - 1))
        delay_minutes = min(raw, cap)
        logger.info(
            f"[Subconscious] Rescheduled: penalty=#{penalty}, "
            f"delay={delay_minutes:.0f}min (raw={raw:.0f}min, cap={cap}min)"
        )
        self._schedule_once(int(delay_minutes * 60))

    def _schedule_once(self, delay_seconds: int) -> None:
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
            logger.warning("[Subconscious] Already running, skip")
            return
        self._is_running = True

        preset = await _state.get_preset()
        prompt_text = await self._load_prompt()
        user_msg_text = (
            f"现在是第 {self._total_runs + 1} 次潜意识推理循环。"
            f"请检查用户 (ID: {self._config.target_user_id}) 的记忆库，执行必要的整理操作。"
            f"完成后务必调用 subconscious_iter_stop。"
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
            logger.info(f"[Subconscious] Cycle #{self._total_runs + 1}")
            chat_obj.begin()
            await chat_obj
        except Exception as e:
            logger.opt(exception=e, colors=True, raw=True).exception(
                f"[Subconscious] Error: {e}"
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
                f"[Subconscious] Abstract saved ({len(self._last_abstracts)}/{self._config.max_abstracts})"
            )
        self._total_runs += 1

        # 先持久化，任一失败则不重置惩罚（保留重试机会）
        save_ok = True
        try:
            await self._save_to_repo()
        except Exception as e:
            logger.warning(f"[Subconscious] Save to repo failed: {e}")
            save_ok = False
        try:
            await self._save_pending_to_repo()
        except Exception as e:
            logger.warning(f"[Subconscious] Save pending failed: {e}")
            save_ok = False

        if save_ok:
            _state.reset_penalty()
            logger.info(
                f"[Subconscious] Cycle done (total_runs={self._total_runs}), "
                f"penalty reset, waiting for next user chat"
            )
        else:
            logger.warning(
                f"[Subconscious] Cycle done but save failed, "
                f"penalty NOT reset (penalty={_state.get_penalty_count()})"
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
                f"[Subconscious] Global usage updated "
                f"(input={usage.prompt_tokens}, output={usage.completion_tokens}, "
                f"count={insights.usage_count})"
            )
        except Exception as e:
            logger.warning(f"[Subconscious] Update global usage failed: {e}")

    #  CachedUserDataRepository 持久化

    async def _load_from_repo(self) -> None:
        """从 CachedUserDataRepository 恢复状态。"""
        try:
            repo = CachedUserDataRepository()
            mem = await repo.get_memory(_REPO_UID)
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
            logger.warning(f"[Subconscious] Load from repo failed: {e}")

    async def _save_to_repo(self) -> None:
        """保存状态到 CachedUserDataRepository。"""
        try:
            repo = CachedUserDataRepository()
            mem = await repo.get_memory(_REPO_UID)
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
                f"[Subconscious] Save to repo failed: {e}"
            )

    async def _save_pending_to_repo(self) -> None:
        """持久化待发送消息到 CachedUserDataRepository 的 extra_prompt 中。"""
        try:
            repo = CachedUserDataRepository()
            mem = await repo.get_memory(_REPO_UID)
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
                f"[Subconscious] Save pending failed: {e}"
            )

    #  Prompt 加载

    async def _load_prompt(self) -> str:
        main_path = (self._prompt_dir / self._config.prompt_file).resolve()
        kn_path = (self._prompt_dir / self._config.prompt_knowledge_file).resolve()
        pr_path = (self._prompt_dir / self._config.prompt_profile_file).resolve()
        ensure_prompt_file(main_path, DEFAULT_SUBCONSCIOUS_PROMPT)
        ensure_prompt_file(kn_path, GUIDE_OF_KNOWLEDGES)
        ensure_prompt_file(pr_path, PROFILE_BUILDING_GUIDE)

        character_prompt = await load_character_prompt()

        template = Template(main_path.read_text(encoding="utf-8"))
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

        # 注入知识库 & 画像指南（从磁盘文件读取，用户可自定义）
        prompt += "\n\n" + kn_path.read_text(encoding="utf-8")
        prompt += "\n\n" + pr_path.read_text(encoding="utf-8")

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
            logger.debug(f"[Subconscious] Memory stats query failed: {e}")

        return prompt

    #  Session 读取 & 摘要缓存

    async def _read_recent_sessions(self, n: int = 5) -> list[SessionSummary]:
        """读取目标用户最近 N 个归档 sessions，按需生成摘要并缓存。"""
        try:
            uid = f"user_{self._config.target_user_id}"
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
        """用 MemoryLimiter 生成单个 session 的全会话摘要。"""
        from amrita_core.chatmanager.memory_limiter import MemoryLimiter
        from amrita_core.types import MemoryModel

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

        memory = MemoryModel(messages=[], abstract="")
        for m in raw_messages:
            if isinstance(m, dict):
                memory.messages.append(Message.model_validate(m))

        async with MemoryLimiter(
            memory,
            Message(role="system", content=""),
        ) as lim:
            # 把所有消息放入 dropped_part → 触发全量摘要
            lim._dropped_messages = list(memory.messages)
            memory.messages = []
            await lim._make_abstract()
            return (
                f"[{date_str}] {memory.abstract}"
                if memory.abstract
                else f"[{date_str}] 无法生成摘要"
            )

    #  用户画像

    async def _read_profile(
        self, start_line: int | None = None, end_line: int | None = None
    ) -> ProfileResult:
        """读取用户画像 Markdown 文件，支持行数滑动窗口。"""
        if not self._profile_path.exists():
            return {
                "summary": "",
                "content": "",
                "total_lines": 0,
                "range_start": 0,
                "range_end": 0,
            }
        try:
            text = self._profile_path.read_text(encoding="utf-8")
            parts = text.split("---", 1)
            summary = parts[0].strip() if parts else ""
            body = parts[1].strip() if len(parts) > 1 else ""
            body_lines = body.split("\n") if body else []
            total = len(body_lines)
            if start_line is None:
                s = 0
            else:
                s = max(0, start_line)
            if end_line is None:
                e = total
            else:
                e = max(s, min(total, end_line))
            return {
                "summary": summary,
                "content": "\n".join(body_lines[s:e]),
                "total_lines": total,
                "range_start": s,
                "range_end": e,
            }
        except Exception as e:
            logger.warning(f"[Subconscious] Read profile failed: {e}")
            return {
                "summary": "",
                "content": "",
                "total_lines": 0,
                "range_start": 0,
                "range_end": 0,
            }

    async def _update_profile(
        self,
        summary: str,
        new_lines: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> None:
        """增量更新用户画像。

        省略 start_line/end_line → 追加到末尾。
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
            # 追加模式
            new_body_lines = body_lines + new
            operation = f"append {len(new)} lines"
        else:
            # 替换模式
            s = max(0, start_line)
            e = max(s, min(len(body_lines), end_line))
            new_body_lines = body_lines[:s] + new + body_lines[e:]
            operation = f"replace [{s}:{e}] → [{s}:{s + len(new)}]"
        new_body = "\n".join(new_body_lines)

        self._profile_path.parent.mkdir(parents=True, exist_ok=True)
        content = f"{summary}\n---\n{new_body}"
        self._profile_path.write_text(content, encoding="utf-8")
        logger.info(
            f"[Subconscious] Profile updated: {operation}, summary={summary[:60]}..."
        )
