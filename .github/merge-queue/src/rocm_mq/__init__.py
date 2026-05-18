"""
rocm_mq — Federated Merge Queue, pure decision layer.

Public API surface (Phase 1 — state + helpers).

**Public-API-locked rule (RESEARCH.md "Phase 1 → Phase 2 Handoff Surface"):**
Every symbol listed in ``__all__`` is part of the stable contract consumed by:
  - Phase 2 ``snapshot.py`` and ``cmd_process.py`` (Raw* types, derive functions)
  - Phase 3 ``cmd_handle.py`` (RenderContext, Action variants)
  - Phase 4 ``cmd_audit.py`` (AppIdentity, is_app_identity)

Adding symbols to ``__all__`` is a Phase 2/3/4 PR (backwards-compatible extension).
Removing symbols is a breaking change requiring all consuming phases to be updated.

Symbols from ``_helpers`` are intentionally surfaced here for I/O-boundary
callers — the leading underscore communicates "I/O-boundary use only, not user-facing".

``decision``, ``comment``, and ``summary`` exports will be added in
Plans 02 and 03 of Phase 1 once those modules exist.
"""

from rocm_mq._helpers import (
    NaiveDatetimeError,
    is_app_identity,
    is_app_identity_actor,
    parse_gh_timestamp,
)
from rocm_mq.comment import render_status_body
from rocm_mq.decision import decide_cycle, derive_pr, derive_snapshot
from rocm_mq.pathmap import queues_for_paths
from rocm_mq.state import (
    Action,
    ActionOutcome,
    Activate,
    AppIdentity,
    CommitStatus,
    CommitStatusCreator,
    CycleRenderContext,
    Defer,
    DeferredPR,
    Eject,
    LabelEvent,
    MergeQueueConfig,
    PartialPRState,
    PRState,
    RawPRState,
    RawSnapshot,
    RenderContext,
    RequiredCheckResult,
    Snapshot,
    Squash,
    TimelineActor,
    UpdateComment,
)

__all__ = [
    "Action",
    "ActionOutcome",
    # Action union
    "Activate",
    # Config
    "AppIdentity",
    "CommitStatus",
    # Raw family
    "CommitStatusCreator",
    "CycleRenderContext",
    "Defer",
    # Q1 / Q4 sum types
    "DeferredPR",
    "Eject",
    "LabelEvent",
    "MergeQueueConfig",
    "NaiveDatetimeError",
    # Derived family
    "PRState",
    "PartialPRState",
    "RawPRState",
    "RawSnapshot",
    # Render context
    "RenderContext",
    "RequiredCheckResult",
    "Snapshot",
    "Squash",
    "TimelineActor",
    "UpdateComment",
    # decision
    "decide_cycle",
    "derive_pr",
    "derive_snapshot",
    "is_app_identity",
    "is_app_identity_actor",
    # Helpers
    "parse_gh_timestamp",
    # pathmap
    "queues_for_paths",
    # comment
    "render_status_body",
]
