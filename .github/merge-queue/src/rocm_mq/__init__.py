"""
rocm_mq — Federated Merge Queue.

Every symbol listed in ``__all__`` is part of the stable public API.
Adding symbols is backwards-compatible; removing is a breaking change.

Selected functions implemented in ``_helpers`` are exported here intentionally:
``_helpers`` is a private implementation module, while the names listed in
``__all__`` below are stable public API.
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
    Snapshot,
    Squash,
    TimelineActor,
    UpdateComment,
)
from rocm_mq.summary import render_cycle_summary

__all__ = [
    # State model
    "Action",
    "ActionOutcome",
    "Activate",
    "AppIdentity",
    "CommitStatus",
    "CommitStatusCreator",
    "CycleRenderContext",
    "Defer",
    "DeferredPR",
    "Eject",
    "LabelEvent",
    "MergeQueueConfig",
    "NaiveDatetimeError",
    "PRState",
    "PartialPRState",
    "RawPRState",
    "RawSnapshot",
    "RenderContext",
    "Snapshot",
    "Squash",
    "TimelineActor",
    "UpdateComment",
    # Functions
    "decide_cycle",
    "derive_pr",
    "derive_snapshot",
    "is_app_identity",
    "is_app_identity_actor",
    "parse_gh_timestamp",
    "queues_for_paths",
    "render_cycle_summary",
    "render_status_body",
]
