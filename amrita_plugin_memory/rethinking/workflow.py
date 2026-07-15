"""工作流编译 — 完全复用 Core Agent 框架。"""

from amrita_core.components.llm import JINJA2_RENDER
from amrita_core.components.process import BUILD_MESSAGE, LOAD_STATE
from amrita_core.components.react import (
    AGENT_ENTRY,
    AGENT_POST_PROCESS,
    REACT_COUNTER,
    SINGLE_STRATEGY_CALL,
)
from amrita_core.enums import BuiltinName
from amrita_sense import ALIAS, GOTO, NOP, WHILE, NodeCompose

from .nodes import LIMITING_MEMORY, STRATEGY_INIT

_single_call = SINGLE_STRATEGY_CALL(fallback_on_fail=False)

_workflow = None


def build_workflow():
    global _workflow
    if _workflow is None:
        wf: NodeCompose = (
            LOAD_STATE
            >> JINJA2_RENDER
            >> LIMITING_MEMORY
            >> BUILD_MESSAGE
            >> STRATEGY_INIT
            >> (
                GOTO(BuiltinName.STRATEGY_EOF)
                >> ALIAS(AGENT_ENTRY, BuiltinName.AGENT_STRATEGY)
                >> WHILE(_single_call).ACTION(REACT_COUNTER)
                >> AGENT_POST_PROCESS
                >> ALIAS(NOP, BuiltinName.STRATEGY_EOF)
            )
        )
        _workflow = wf.render()
    return _workflow
