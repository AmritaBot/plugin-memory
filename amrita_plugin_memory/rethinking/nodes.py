"""工作流 Node — 复用 Core Agent 框架。"""

from amrita_core.chatmanager import ChatObject
from amrita_core.chatmanager.memory_limiter import MemoryLimiter
from amrita_core.utils import gather_usage
from amrita_sense import Node


@Node(tag="subconscious_limiting_memory")
async def LIMITING_MEMORY(chat_obj: ChatObject) -> None:
    """运行 Core MemoryLimiter — 截断超限消息并生成摘要。

    等价于 chat_object.py 的 _limiting_memory，去掉了 SuspendEnum 暂停机制
    （因为我们不使用 ChatObject 的流式暂停）。
    """
    mem_ctx = chat_obj._di_memory
    input_ctx = chat_obj._di_input
    ab = chat_obj._di_ability
    resp = chat_obj._di_resp
    if not ab.config.llm.enable_memory_abstract:
        return
    assert mem_ctx.memory is not None, "Memory must be loaded before limiting"
    async with MemoryLimiter(mem_ctx.memory, input_ctx.train, config=ab.config) as lim:
        await lim.run_enforce()
        if abs_usage := lim.usage:
            resp.extra_usage = gather_usage(resp.extra_usage, abs_usage)
        mem_ctx.memory = lim.memory
