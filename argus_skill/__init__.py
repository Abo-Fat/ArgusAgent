"""argus-skill: supervised skill-driven coding agent.

Public API:
    SkillLoop      — the integrated matcher → distiller → engineer-with-reviewer loop.
    SkillStore     — on-disk markdown skill cache with LLM matcher.
    LoopOutcome    — result of a SkillLoop.run() invocation.
    RunnerBackend  — protocol any LLM backend must implement.
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core.models import LoopOutcome, ReviewDecision, RunnerOptions, RunnerResult
    from .core.ports import RunnerBackend, SkillSource
    from .loop import SkillLoop, SkillLoopConfig
    from .skills.store import Skill, SkillStore

_EXPORT_MODULES = {
    "LoopOutcome": ".core.models",
    "ReviewDecision": ".core.models",
    "RunnerBackend": ".core.ports",
    "RunnerOptions": ".core.models",
    "RunnerResult": ".core.models",
    "Skill": ".skills.store",
    "SkillLoop": ".loop",
    "SkillLoopConfig": ".loop",
    "SkillSource": ".core.ports",
    "SkillStore": ".skills.store",
}


def __getattr__(name: str) -> object:
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value: object = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "LoopOutcome",
    "ReviewDecision",
    "RunnerBackend",
    "RunnerOptions",
    "RunnerResult",
    "Skill",
    "SkillLoop",
    "SkillLoopConfig",
    "SkillSource",
    "SkillStore",
]

__version__ = "0.1.1"
