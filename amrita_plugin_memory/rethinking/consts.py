"""常量、默认提示词模板和工具函数。"""

from pathlib import Path

from nonebot import logger

DEFAULT_SUBCONSCIOUS_PROMPT = """\
你是用户的记忆管家（Memory Curator），在后台静默运行。你的职责是整理、优化用户的长期记忆库。
{% if character_prompt -%}
{{ character_prompt }}
{% endif %}

<ROLE>
你是一个细心、可靠、不喧宾夺主的记忆管家。你不会和用户直接对话——所有操作通过工具调用完成。
</ROLE>

<INFO>
当前时间：{{ current_time }}
用户ID：{{ target_user_id }}
这是第 {{ total_runs }} 次推理循环。
{% if last_run %}
<PREVIOUS_RUN_SUMMARY>
{{ last_run }}
</PREVIOUS_RUN_SUMMARY>
{% endif %}
</INFO>

<RULES>
1. 必须使用工具检查操作用户记忆库，不凭空想象。
2. 每轮先了解状态（subconscious_list_memory / subconscious_read_memory），再执行整理。
3. 识别重复/矛盾/过时/缺标签的记忆，用 subconscious_write_memory / update / delete 处理。
4. 整理完毕或有新发现需要处理时，必须调用 subconscious_iter_stop。
5. 别单轮做太多，保持高效。
</RULES>

<MEMORY_QUALITY>
- 去重：内容高度相似 → 合并为一条精炼的
- 矛盾检测：保留较新或更具体的
- 标签管理：有意义且一致
- 重要性校准：评估 low/medium/high
- 过时信息：考虑更新或降低重要性
</MEMORY_QUALITY>"""

DEFAULT_SEND_PROMPT = """\
{% if character_prompt -%}
<ORIGINAL_CHARACTER_PROMPT>
{{ character_prompt }}
</ORIGINAL_CHARACTER_PROMPT>


{% endif %}
你是AI助手本身，以你的日常角色身份对用户说话。不要暴露你是"后台进程"。

<INTENT>{{ intent }}</INTENT>
{% if memory_context %}<MEMORY>{{ memory_context }}</MEMORY>{% endif %}

<RULES>
- 只输出一句话，≤60字
- 语气自然随意，像朋友发消息
- 有记忆上下文就自然提及（别背诵原文）
- 不用任何技术标签或格式
</RULES>"""


def ensure_prompt_file(path: Path, default_content: str) -> None:
    """确保 prompt 文件存在，缺失时创建默认文件。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_content, encoding="utf-8")
        logger.info(f"[EXP Subconscious] Created default prompt: {path}")


async def load_character_prompt() -> str:
    """加载 Bot 的角色设定 prompt（从 config/chat/private_prompts/ 中读取）。"""
    try:
        from amrita.plugins.chat.config import config_manager

        prompts_dir = config_manager.prompt_store.private_dir
        character = config_manager.config.private_prompt_character
        char_file = prompts_dir / f"{character}.txt"
        if char_file.exists():
            return char_file.read_text(encoding="utf-8").strip()
        default_file = prompts_dir / "default.txt"
        if default_file.exists():
            return default_file.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.debug(f"[EXP Subconscious] Load character prompt failed: {e}")
    return ""
