"""Deterministic static-analysis rules. One module per rule family; IDs are stable."""

from __future__ import annotations

from backend.audit.rules.allocations import (
    ConstantLiteralInLoop,
    DataFrameGrowthInLoop,
    DeepcopyInLoop,
    IterrowsLoop,
)
from backend.audit.rules.architecture import ImportTimeWork, UnboundedModuleCache
from backend.audit.rules.async_blocking import BlockingCallInAsync
from backend.audit.rules.base import BaseRule, FindingDraft, RuleMeta, ScopeTracker
from backend.audit.rules.caching import RecursionWithoutCache, RepeatedConstantCall
from backend.audit.rules.containers import ListAsQueue, ListSearchInLoop, NeedlessMaterialization
from backend.audit.rules.database import QueryInLoop
from backend.audit.rules.loops import (
    ListMembershipInLoop,
    LoopInvariantWork,
    NestedLoopJoin,
    SortInLoop,
)
from backend.audit.rules.network import ClientPerCall, NetworkCallInLoop
from backend.audit.rules.regex import RegexInLoop
from backend.audit.rules.strings import StringConcatInLoop

RULE_CLASSES: tuple[type[BaseRule], ...] = (
    ListMembershipInLoop,
    NestedLoopJoin,
    LoopInvariantWork,
    SortInLoop,
    StringConcatInLoop,
    RegexInLoop,
    ListSearchInLoop,
    ListAsQueue,
    NeedlessMaterialization,
    QueryInLoop,
    BlockingCallInAsync,
    NetworkCallInLoop,
    ClientPerCall,
    DeepcopyInLoop,
    ConstantLiteralInLoop,
    DataFrameGrowthInLoop,
    IterrowsLoop,
    RecursionWithoutCache,
    RepeatedConstantCall,
    ImportTimeWork,
    UnboundedModuleCache,
)


def default_rules() -> list[BaseRule]:
    return [cls() for cls in RULE_CLASSES]


def rule_catalog() -> list[RuleMeta]:
    return [cls.meta for cls in RULE_CLASSES]


__all__ = [
    "RULE_CLASSES",
    "BaseRule",
    "FindingDraft",
    "RuleMeta",
    "ScopeTracker",
    "default_rules",
    "rule_catalog",
]
