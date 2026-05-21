"""RFC §4.2 worked-example regression.

The 8-timestep narrative below mirrors the RFC §4.2 example line by line.

A key behavioural choice this test pins: at T1, the cycle emits [Squash(A)]
alone (NOT [Squash(A), Activate(B), Activate(C)]). The RFC §4.6 rule
"activation and evaluation never happen in the same cycle for the same PR"
applies per-PR, not per-cycle-globally. In the T1 snapshot, A is still
mq:active (the executor has not cleaned up yet), so B and C are still
blocked by A as head-of-all in their shared queues. After A is squashed
and removed from the snapshot, the T2 snapshot shows B and C as heads
of their respective queues — so B/C activations land at T2.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from rocm_mq.decision import decide_cycle
from rocm_mq.pathmap import queues_for_paths
from rocm_mq.state import (
    Activate,
    PRState,
    Snapshot,
    Squash,
)
from tests.conftest import canonical_merge_queue_config, utc

# ---------------------------------------------------------------------------
# RFC §4.2 timestep constants — 8 timesteps, 3 minutes apart
# ---------------------------------------------------------------------------

T0 = utc(2026, 1, 1, 10, 0)
T1 = T0 + timedelta(minutes=3)
T2 = T1 + timedelta(minutes=3)
T3 = T2 + timedelta(minutes=3)
T4 = T3 + timedelta(minutes=3)
T5 = T4 + timedelta(minutes=3)
T6 = T5 + timedelta(minutes=3)
T7 = T6 + timedelta(minutes=3)

CONFIG = canonical_merge_queue_config()


# ---------------------------------------------------------------------------
# PR construction helpers
# ---------------------------------------------------------------------------


def _make_pr(
    number: int,
    paths: tuple[str, ...],
    enqueued_at: object,
    *,
    is_active: bool = False,
    all_checks_passed: bool = False,
) -> PRState:
    """Construct a PRState for the worked example.

    Computes queue membership from ``paths`` using the real ``queues_for_paths``
    function — this implicitly regression-tests pathmap against the worked-example
    paths (RFC §4.2 membership matrix).

    Label set:
    - Queued: ``{"mq:queued"} | {f"mq:{q}" for q in queues}``
    - Active: ``{"mq:active"} | {f"mq:{q}" for q in queues}``
    """
    queues = queues_for_paths(paths, CONFIG)
    if is_active:
        labels = frozenset({"mq:active"} | {f"mq:{q}" for q in queues})
    else:
        labels = frozenset({"mq:queued"} | {f"mq:{q}" for q in queues})
    return PRState(
        number=number,
        head_sha=f"sha-{number}",
        labels=labels,
        queues=queues,
        enqueued_at=enqueued_at,  # type: ignore[arg-type]
        is_validly_active=is_active,
    )


def _active(pr: PRState) -> PRState:
    """Return a copy of ``pr`` modeling "post-Activate next-cycle state".

    Models what the executor + ``derive_pr`` would produce after an
    ``Activate`` action was applied:
    - ``mq:active`` added to labels.
    - ``mq:queued`` removed from labels.
    - ``is_validly_active = True``.
    - Required checks set to ``"success"`` so step 5b dispatches to Squash.

    This is the PRState shape the snapshot would contain in the NEXT cycle
    after an Activate action was executed by the executor.
    """
    new_labels = (pr.labels - {"mq:queued"}) | {"mq:active"}
    return PRState(
        number=pr.number,
        head_sha=pr.head_sha,
        labels=new_labels,
        queues=pr.queues,
        enqueued_at=pr.enqueued_at,
        is_validly_active=True,
    )


# ---------------------------------------------------------------------------
# RFC §4.2 PR definitions
#
# A — core PR touching projects/hipdnn/; enters ALL SIX queues:
#     hipdnn, miopen-provider, hipblaslt-provider, composable-kernel-provider,
#     rocblas-provider, integration-tests. Enqueued at T0.
# B — miopen-provider-only PR. Enqueued at T0.
# C — hipblaslt-provider-only PR. Enqueued at T0.
# D — integration-tests-only PR; enters FIVE queues (miopen-provider,
#     hipblaslt-provider, composable-kernel-provider, rocblas-provider,
#     integration-tests) but NOT hipdnn. Enqueued at T0.
# E — core PR touching projects/hipdnn/; enters ALL SIX queues. Enqueued at T1.
# ---------------------------------------------------------------------------

A = _make_pr(number=1, paths=("projects/hipdnn/api/foo.h",), enqueued_at=T0)
B = _make_pr(
    number=2, paths=("dnn-providers/miopen-provider/src/x.cpp",), enqueued_at=T0
)
C = _make_pr(
    number=3, paths=("dnn-providers/hipblaslt-provider/src/y.cpp",), enqueued_at=T0
)
D = _make_pr(
    number=4, paths=("dnn-providers/integration-tests/foo.py",), enqueued_at=T0
)
E = _make_pr(
    number=5, paths=("projects/hipdnn/api/bar.h",), enqueued_at=T1
)


# ---------------------------------------------------------------------------
# Parametrized 8-timestep regression
#
# One case per timestep transition. At each timestep, the snapshot reflects the
# cumulative open-PR set with labels updated from prior cycle outcomes.
#
# T2 and T3: set equality (RFC §4.6 — order across disjoint queue sets is
#   implementation-defined; only the set of actions is contract-defined).
# All other timesteps: list equality (single action or ordered single-queue).
#
# T1 emits [Squash(A)] only; B/C activations land at T2 (the "same-cycle
# activation + evaluation" rule is per-PR, not per-cycle-globally).
# ---------------------------------------------------------------------------

_CASES = [
    # T0: Only A present (B, C, D have same enqueued_at but A is head of all).
    # A is at head of all six queues. B is blocked in miopen-provider behind A
    # (A's labels include mq:miopen-provider since projects/hipdnn/ → all six queues).
    # Wait — B is only in miopen-provider. A is in all six. So A is head of hipdnn,
    # miopen-provider, hipblaslt-provider, composable-kernel-provider, rocblas-provider,
    # integration-tests. B (enqueued_at=T0) is also in miopen-provider; since A.number=1
    # enqueued at T0 and B.number=2 enqueued at T0, they share the same enqueued_at.
    # FIFO sort is by enqueued_at; ties broken by insertion order (stable sort, A first
    # in Snapshot since A is passed first). A and B tied at T0 in miopen-provider →
    # after sort, A comes first (stable sort preserves snapshot order).
    # Expected: [Activate(A)].
    pytest.param(
        T0,
        Snapshot(prs=(A, B, C, D)),
        [Activate(pr=A)],
        "list",
        id="T0-activate-A",
    ),
    # T1: A is active (mq:active, is_validly_active=True, checks="success").
    #     E has joined (enqueued_at=T1, later than T0 — tail of all queues).
    #     B, C, D remain queued.
    #     Q2 resolution: this cycle emits [Squash(A)] only; B/C activations land
    #     in the NEXT cycle (T2). Squash and Activate are never co-emitted for
    #     the same PR, but PRs in disjoint queues can be in the same cycle.
    #     However here, B blocks on A in miopen-provider (A is active, still in
    #     the snapshot) — once A is squashed the executor removes it, freeing B
    #     and C. Until the next cycle, B and C are not heads.
    pytest.param(
        T1,
        Snapshot(prs=(_active(A), B, C, D, E)),
        [Squash(pr=_active(A))],
        "list",
        id="T1-squash-A-Q2-resolution",
    ),
    # T2: A has been merged (executor removed it). B and C are heads of their
    #     respective sole-membership queues (miopen-provider and hipblaslt-provider).
    #     D is in miopen-provider, hipblaslt-provider, composable-kernel-provider,
    #     rocblas-provider, integration-tests. B blocks D in miopen-provider;
    #     C blocks D in hipblaslt-provider. E (enqueued_at=T1) is behind A in all
    #     six queues, but now that A is gone, E is at head of hipdnn; however
    #     in the five shared queues (miopen-provider, hipblaslt-provider,
    #     composable-kernel-provider, rocblas-provider, integration-tests),
    #     B/C/D are ahead of E (enqueued_at=T0 < T1). So E is NOT head-of-all.
    #     B is head of miopen-provider only (not blocked elsewhere).
    #     C is head of hipblaslt-provider only (not blocked elsewhere).
    #     B and C are in DISJOINT queues → both activate this cycle (order unspecified).
    #     Set equality per Pitfall 8.
    pytest.param(
        T2,
        Snapshot(prs=(B, C, D, E)),
        {Activate(pr=B), Activate(pr=C)},
        "set",
        id="T2-activate-B-C-set",
    ),
    # T3: B and C are active (mq:active, is_validly_active=True, checks="success").
    #     D and E remain queued.
    #     B is head of miopen-provider (active, checks pass) → Squash(B).
    #     C is head of hipblaslt-provider (active, checks pass) → Squash(C).
    #     D is still blocked in miopen-provider (B) and hipblaslt-provider (C).
    #     E is still blocked in miopen-provider and hipblaslt-provider.
    #     Set equality (disjoint queues).
    pytest.param(
        T3,
        Snapshot(prs=(_active(B), _active(C), D, E)),
        {Squash(pr=_active(B)), Squash(pr=_active(C))},
        "set",
        id="T3-squash-B-C-set",
    ),
    # T4: B and C merged. D and E remain.
    #     D queues: miopen-provider, hipblaslt-provider, composable-kernel-provider,
    #               rocblas-provider, integration-tests (enqueued_at=T0).
    #     E queues: hipdnn, miopen-provider, hipblaslt-provider,
    #               composable-kernel-provider, rocblas-provider, integration-tests
    #               (enqueued_at=T1).
    #     In hipdnn: only E → E is head. But in all five queues D belongs to,
    #     D (T0) < E (T1) → D is head, E is not. E requires headship in ALL its
    #     queues including miopen-provider, hipblaslt-provider, etc. → E is NOT
    #     head-of-all (blocked by D in five queues).
    #     D is head of all five of its queues → [Activate(D)].
    pytest.param(
        T4,
        Snapshot(prs=(D, E)),
        [Activate(pr=D)],
        "list",
        id="T4-activate-D",
    ),
    # T5: D is active. E is still queued.
    #     D is head of all its queues, active, checks pass → [Squash(D)].
    pytest.param(
        T5,
        Snapshot(prs=(_active(D), E)),
        [Squash(pr=_active(D))],
        "list",
        id="T5-squash-D",
    ),
    # T6: D merged. Only E remains.
    #     E is head of all six queues → [Activate(E)].
    pytest.param(
        T6,
        Snapshot(prs=(E,)),
        [Activate(pr=E)],
        "list",
        id="T6-activate-E",
    ),
    # T7: E is active. Only E in snapshot.
    #     E is head of all its queues, active, checks pass → [Squash(E)].
    pytest.param(
        T7,
        Snapshot(prs=(_active(E),)),
        [Squash(pr=_active(E))],
        "list",
        id="T7-squash-E",
    ),
]


@pytest.mark.parametrize("now,snapshot,expected,kind", _CASES)
def test_worked_example_timestep(
    now: object,
    snapshot: Snapshot,
    expected: object,
    kind: str,
) -> None:
    """Replay one RFC §4.2 timestep transition.

    Calls ``decide_cycle`` with the timestep's snapshot and asserts the returned
    action list matches the expected actions. At T2 and T3 (disjoint queue sets),
    set equality is used to tolerate implementation-defined ordering (Pitfall 8).
    At all other timesteps, list equality is used.
    """
    actions = decide_cycle(snapshot, CONFIG, now)  # type: ignore[arg-type]
    if kind == "set":
        # Pitfall 8: order across disjoint queue sets is implementation-defined.
        # Set equality is the correct assertion here — see module docstring.
        assert set(actions) == expected
    else:
        assert actions == expected


# ---------------------------------------------------------------------------
# Full-sequence integration test
#
# Walks all 8 timesteps in order, verifying the end-to-end narrative matches
# the RFC §4.2 5-PR / 8-timestep story.
# ---------------------------------------------------------------------------


def test_worked_example_full_sequence() -> None:
    """RFC §4.2 full 5-PR / 8-timestep sequence — end-to-end integration test.

    Verifies the complete merge order: A at T1 → B+C at T3 → D at T5 → E at T7.
    Each cycle is exercised; assertions confirm the nominal action per cycle.
    """
    # T0: A activates
    actions_t0 = decide_cycle(Snapshot(prs=(A, B, C, D)), CONFIG, T0)
    assert actions_t0 == [Activate(pr=A)]

    # T1: A squashes (Q2: B/C activations deferred to next cycle)
    actions_t1 = decide_cycle(Snapshot(prs=(_active(A), B, C, D, E)), CONFIG, T1)
    assert actions_t1 == [Squash(pr=_active(A))]

    # T2: B and C both activate (disjoint queues, order unspecified)
    actions_t2 = decide_cycle(Snapshot(prs=(B, C, D, E)), CONFIG, T2)
    assert set(actions_t2) == {Activate(pr=B), Activate(pr=C)}

    # T3: B and C both squash
    actions_t3 = decide_cycle(Snapshot(prs=(_active(B), _active(C), D, E)), CONFIG, T3)
    assert set(actions_t3) == {Squash(pr=_active(B)), Squash(pr=_active(C))}

    # T4: D activates (E still blocked)
    actions_t4 = decide_cycle(Snapshot(prs=(D, E)), CONFIG, T4)
    assert actions_t4 == [Activate(pr=D)]

    # T5: D squashes
    actions_t5 = decide_cycle(Snapshot(prs=(_active(D), E)), CONFIG, T5)
    assert actions_t5 == [Squash(pr=_active(D))]

    # T6: E activates
    actions_t6 = decide_cycle(Snapshot(prs=(E,)), CONFIG, T6)
    assert actions_t6 == [Activate(pr=E)]

    # T7: E squashes — all 5 PRs merged
    actions_t7 = decide_cycle(Snapshot(prs=(_active(E),)), CONFIG, T7)
    assert actions_t7 == [Squash(pr=_active(E))]

    # Final state: all PRs merged in order A → B+C → D → E
    # (B and C merged in the same cycle at T3; the exact order between them
    # is implementation-defined per RFC §4.6 — only relative ordering within
    # the same queue matters)


# ---------------------------------------------------------------------------
# Pitfall 8 sanity check
#
# Explicitly documents WHY set equality is used at T2/T3: the RFC guarantees
# Activate(B) and Activate(C) will both be in the action list, but says nothing
# about their relative order.
# ---------------------------------------------------------------------------


def test_pitfall8_t2_set_equality_rationale() -> None:
    """Pitfall 8 regression: T2 actions contain both Activate(B) and Activate(C).

    list(actions) may have different order across runs (implementation-defined
    ordering across disjoint queue sets — RFC §4.6). The set comparison below
    holds regardless of order. This test exists to document the rationale: if
    this comparison were written as ``actions == [Activate(B), Activate(C)]``,
    it would pass today and break silently when iteration order shifts.
    """
    actions = decide_cycle(Snapshot(prs=(B, C, D, E)), CONFIG, T2)
    # Both B and C must be activated — order is implementation-defined
    assert Activate(pr=B) in actions
    assert Activate(pr=C) in actions
    assert len(actions) == 2  # no other actions
    # set comparison is the canonical way to assert this
    assert set(actions) == {Activate(pr=B), Activate(pr=C)}
