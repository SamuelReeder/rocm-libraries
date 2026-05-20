"""
rocm_mq.dogfood.dog_06 — DOG-06 marquee 5-PR RFC §4.2 worked-example driver.

RFC §4.2 + §6 DOG-06 scenario: replay the canonical 5-PR worked example
end-to-end. The narrative this driver materializes:

    PR_A enqueues (touches integration-tests path → engages every provider
        queue + integration-tests) → activates → squash-merges.
    PR_B enqueues (single-provider miopen-provider) AND PR_C enqueues
        (single-provider hipblaslt-provider — DISJOINT queue from B) → both
        activate after A's squash → both squash-merge in parallel (RFC §4.2
        "B and C can proceed in parallel because they share no queue").
    PR_D enqueues (integration-tests path again — engages every provider) →
        activates → squash-merges.
    PR_E enqueues (miopen-provider — same queue as PR_B, the narrative beat
        "E touches provider in the same queue as something earlier") →
        activates → squash-merges.

The single JSON output the driver emits reads top-to-bottom like the RFC §4.2
narrative — a reviewer holding the JSON next to the RFC should be able to
follow the story without consulting the per-PR URLs. Every
``squash_merge_completed`` event carries ``tree_diff_status='ahead'`` per
Phase 2 SC#3 (closed in 02-05) and CONTEXT.md D-05's implication — DOG-06
surfaces a future GitHub-side regression of the Apr-2026 silent-corruption
pattern even without a deliberate scenario.

**Why DOG-06 has its own plan + driver** (plan 03-14 <objective>):
  * Wall-time budget (60 min) is the largest of any dogfood scenario.
  * Timeline event capture is more sophisticated — five interleaved PR
    lifecycles aggregated into one ordering-preserving timeline.
  * The RFC §4.3 binding invariant — every ``squash_merge_completed`` MUST
    be preceded by ``merge_queue_active_status_posted`` for the SAME PR —
    is asserted from the timeline post-hoc.
  * The tree_diff_status capture per CONTEXT.md D-05 is a separate
    extraction pipeline (status-comment substring match + cycle-summary
    artifact fallback).

**Phase 5 porting-prep role:** DOG-06's per-run JSON IS the canonical replay
reference an upstream reviewer reads to verify the queue's invariants
without re-running the dogfood suite (CONTEXT.md D-04 + deferred:
"DOG-06 5-PR scenario as an evidence pack for upstream review").

Two-mode driver per CONTEXT.md D-04:

  1. Unit-test mode: ``run_scenario(client, ...)`` is called with a
     FakeGitHub extension that simulates the 5-PR lifecycle deterministically
     (handler enqueue, processor activation+squash per PR, cycle-summary
     artifact). Tests live in ``tests/test_dogfood_dog_06.py``. Verifies
     orchestration + invariant assertions without touching the live API.

  2. Live-fork mode: ``python -m rocm_mq.dogfood.dog_06 --owner SamuelReeder
     --repo rocm-libraries`` against the live fork. Operator-initiated AFTER
     mq-handler.yml + mq-processor.yml are deployed and the App installation
     is live. Drivers leave PRs in place per D-04 (no teardown).

Operational note (per plan 03-14 <action>): if the driver runs longer than
TIMEOUT_S due to real-CI variability, driver re-invocation does NOT clean up
half-progress — the operator must manually finalize partially-progressed
PRs (clear ``mq:*`` labels, comment ``/dequeue``) before retrying.

Output: writes a single per-run JSON under
``.planning/phases/03-handler-processor-on-fork/dogfood-runs/`` with the D-04
schema; ``passed=True`` iff all 5 PRs squash-merged AND the per-PR ordering
invariant holds AND every squash carries ``tree_diff_status='ahead'``.

Timeout budget: 60 min (RESEARCH.md Area #10 — 4+ serial squashes through
real CI; the longest scenario in the suite).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rocm_mq.dogfood._base import (
    DogfoodResult,
    create_dogfood_pr,
    download_cycle_summary_artifact,
    emit_result,
    post_command,
)

# ---------------------------------------------------------------------------
# Module constants — locked by plan 03-14 Task 1 behavior block.
# ---------------------------------------------------------------------------

SCENARIO_ID: str = "dog_06"
TIMEOUT_S: int = 60 * 60  # 3600s — RESEARCH.md Area #10 / plan 03-14 must_haves.

# Marker the renderer embeds in every status comment body (comment.py).
_STATUS_MARKER: str = "<!-- rocm-mq-status -->"

# Substring the processor's squash status comment carries when the Phase B
# tree-diff sanity check (02-05 SC#3 closure) reports ``status='ahead'``.
# The driver's tree_diff_status extraction matches on this literal substring
# in BOTH the squash status comment body AND the downloaded cycle-summary
# artifact (artifact fallback per RESEARCH.md Area #11 — the comment body is
# the primary source; artifact is the secondary).
_TREE_DIFF_AHEAD_MARKER: str = "tree_diff_status=ahead"

# Substrings in the squash status comment body that mark it as a squash-
# success event (not an active or queued or eject comment). The processor's
# renderer emits "## Squashed" as the section header on a successful squash
# (per the comment.render_status_body pattern + the Activate→Squash action
# sequence in summary.py). Matched as a substring to stay robust to minor
# wording drift.
_SQUASH_SECTION_MARKER: str = "Squashed"
_ACTIVE_SECTION_MARKER: str = "Active"


@dataclass(frozen=True, slots=True)
class _PRDescriptor:
    """Per-PR descriptor pinning the RFC §4.2 narrative beats.

    Frozen + slots — Pitfall 11 / CLAUDE.md decision-layer-style hashable.
    Drivers consume these positionally so the RFC §4.2 narrative ordering
    (A first, then B/C parallel, then D, then E) maps to the list order.
    """

    letter: str  # "A".."E"
    paths: tuple[str, ...]  # one or more repo-relative file paths
    queue_membership_comment: str  # short comment for source-file traceability


# Per-PR descriptors keyed by the RFC §4.2 worked-example narrative.
#
# Path choices derive directly from path_to_queues.yml:
#   * ``dnn-providers/integration-tests/`` routes to {miopen-provider,
#     hipblaslt-provider, hip-kernel-provider, fusilli-provider,
#     integration-tests} — the RFC §4.2 asymmetric edge (integration-tests
#     changes block every provider's queue, but a provider change does NOT
#     block integration-tests itself; this is exactly the queue-membership
#     story PR_A and PR_D need).
#   * Each ``dnn-providers/<X>-provider/`` routes to ``[<X>-provider]`` —
#     a single queue that contains only that provider.
#
# PR_A through PR_E ordering maps to the RFC §4.2 narrative:
#   * PR_A (integration-tests) merges first — engages every provider queue.
#   * PR_B (miopen) and PR_C (hipblaslt) can parallelize — disjoint queues.
#   * PR_D (integration-tests again) — engages every provider queue.
#   * PR_E (miopen again) — same queue as PR_B, the "E touches a previously-
#     used queue" beat.
PR_DESCRIPTORS: tuple[_PRDescriptor, ...] = (
    _PRDescriptor(
        letter="A",
        paths=("dnn-providers/integration-tests/dogfood-dog_06-pr_a.txt",),
        queue_membership_comment=(
            "Engages integration-tests + every provider queue per "
            "path_to_queues.yml asymmetric integration-tests edge "
            "(RFC §4.2 lines: A blocks everything downstream)."
        ),
    ),
    _PRDescriptor(
        letter="B",
        paths=("dnn-providers/miopen-provider/dogfood-dog_06-pr_b.txt",),
        queue_membership_comment=(
            "Single-provider queue (miopen-provider). DISJOINT from PR_C — "
            "can parallelize after A squashes (RFC §4.2 lines: B and C "
            "proceed in parallel because they share no queue)."
        ),
    ),
    _PRDescriptor(
        letter="C",
        paths=("dnn-providers/hipblaslt-provider/dogfood-dog_06-pr_c.txt",),
        queue_membership_comment=(
            "Single-provider queue (hipblaslt-provider). DISJOINT from PR_B."
        ),
    ),
    _PRDescriptor(
        letter="D",
        paths=("dnn-providers/integration-tests/dogfood-dog_06-pr_d.txt",),
        queue_membership_comment=(
            "Engages integration-tests + every provider queue (same shape "
            "as PR_A; RFC §4.2 lines: D is the next integration-tests PR "
            "after B/C clear)."
        ),
    ),
    _PRDescriptor(
        letter="E",
        paths=("dnn-providers/miopen-provider/dogfood-dog_06-pr_e.txt",),
        queue_membership_comment=(
            "Same queue as PR_B (miopen-provider) — the RFC §4.2 narrative "
            "beat 'E touches a provider in the same queue as something "
            "earlier'."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Comment-scanning helpers
# ---------------------------------------------------------------------------


def _list_status_comments(
    client: Any, owner: str, repo: str, pr_number: int
) -> list[Any]:
    """Return all status-marker-bearing comments on a PR (oldest-first)."""
    resp = client.rest.issues.list_comments(owner, repo, pr_number)
    comments = list(resp.parsed_data or [])
    return [c for c in comments if _STATUS_MARKER in str(getattr(c, "body", ""))]


def _comment_body(comment: Any) -> str:
    return str(getattr(comment, "body", ""))


def _is_active_comment(body: str) -> bool:
    return _ACTIVE_SECTION_MARKER in body


def _is_squash_comment(body: str) -> bool:
    return _SQUASH_SECTION_MARKER in body


def _extract_run_url(body: str) -> str:
    """Extract the first GHA run URL embedded in a status comment body.

    The processor's renderer writes ``Processor cycle: <url>`` per the
    comment-rendering convention. We match the substring ``actions/runs/``
    and read forward until a whitespace boundary so the extraction is robust
    to surrounding markdown (links, bold, line breaks).
    """
    needle = "actions/runs/"
    idx = body.find(needle)
    if idx < 0:
        return ""
    # Walk forward from the start of the surrounding URL token. The URL
    # begins at the previous whitespace or ``(`` or newline; find that
    # boundary by scanning backwards.
    start = idx
    while start > 0 and body[start - 1] not in (" ", "\n", "\t", "(", "[", "<"):
        start -= 1
    end = idx + len(needle)
    while end < len(body) and body[end] not in (" ", "\n", "\t", ")", "]", ">"):
        end += 1
    return body[start:end]


def _extract_run_id_from_url(run_url: str) -> int | None:
    """Parse the integer run_id trailing ``/actions/runs/`` in a GHA run URL."""
    needle = "/actions/runs/"
    idx = run_url.find(needle)
    if idx < 0:
        return None
    tail = run_url[idx + len(needle):]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _extract_tree_diff_status(body: str, fallback_summary: str) -> str:
    """Return the tree_diff_status value extracted from comment/summary text.

    Primary source: the squash status comment body (the literal
    ``tree_diff_status=ahead`` substring written by the processor's renderer
    on a successful Phase B tree-diff sanity check).

    Fallback: the cycle-summary artifact body (RESEARCH.md Area #11 — the
    processor uploads ``cycle-summary.md`` as an artifact; we read the same
    substring from there if the comment body lacked it for whatever reason).

    Returns the literal string ``"ahead"`` on detection, or the empty string
    on absence — the empty-string sentinel is what flips the
    ``tree_diff_status='ahead'`` invariant assertion to False, signalling
    the Apr-2026 silent-corruption regression pattern.
    """
    if _TREE_DIFF_AHEAD_MARKER in body:
        return "ahead"
    if _TREE_DIFF_AHEAD_MARKER in fallback_summary:
        return "ahead"
    return ""  # sentinel — flips invariant assertion to False


# ---------------------------------------------------------------------------
# Per-PR observation walker — derives the timeline from comment lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PRObservation:
    """Result of inspecting one PR's status comments at a given poll moment."""

    pr_number: int
    letter: str
    has_active_comment: bool
    has_squash_comment: bool
    active_run_url: str
    squash_run_url: str
    squash_body: str  # full body — used for tree_diff_status extraction


def _observe_pr(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    letter: str,
) -> _PRObservation:
    """Inspect a PR's status comments; return what stage it has reached."""
    comments = _list_status_comments(client, owner, repo, pr_number)
    has_active = False
    has_squash = False
    active_url = ""
    squash_url = ""
    squash_body = ""
    for c in comments:
        body = _comment_body(c)
        if _is_active_comment(body) and not has_active:
            has_active = True
            active_url = _extract_run_url(body)
        if _is_squash_comment(body) and not has_squash:
            has_squash = True
            squash_url = _extract_run_url(body)
            squash_body = body
    return _PRObservation(
        pr_number=pr_number,
        letter=letter,
        has_active_comment=has_active,
        has_squash_comment=has_squash,
        active_run_url=active_url,
        squash_run_url=squash_url,
        squash_body=squash_body,
    )


# ---------------------------------------------------------------------------
# Global polling loop — fires when ALL 5 PRs have a squash status comment
# ---------------------------------------------------------------------------


def _poll_all_prs_to_squash(
    client: Any,
    owner: str,
    repo: str,
    pr_records: list[tuple[str, int, str]],
    *,
    timeout_s: float,
    interval_s: float,
    on_poll: Callable[[], None] | None = None,
) -> dict[int, _PRObservation]:
    """Poll until EVERY PR in ``pr_records`` has a squash status comment.

    Returns a mapping ``{pr_number: _PRObservation}`` once the global
    predicate fires. Raises ``TimeoutError`` if ``timeout_s`` elapses first.

    The ``on_poll`` seam is a test-only hook invoked BEFORE each poll
    iteration so the test fixture can advance the simulated processor
    cycles between polls. In live-fork mode the caller passes ``None`` and
    the real processor posts the comments asynchronously.

    ``timeout_s`` is REQUIRED per the same threat model as ``poll_pr_state``
    (T-03-10-02 — no infinite-loop default). ``time.monotonic`` is the
    elapsed clock so wall-clock jumps cannot extend the budget.
    """
    start = time.monotonic()
    while True:
        if on_poll is not None:
            on_poll()
        observations: dict[int, _PRObservation] = {}
        all_squashed = True
        for letter, pr_number, _pr_url in pr_records:
            obs = _observe_pr(client, owner, repo, pr_number, letter)
            observations[pr_number] = obs
            if not obs.has_squash_comment:
                all_squashed = False
        if all_squashed:
            return observations
        elapsed = time.monotonic() - start
        if elapsed >= timeout_s:
            unmerged = [
                f"PR_{obs.letter} (#{obs.pr_number})"
                for obs in observations.values()
                if not obs.has_squash_comment
            ]
            raise TimeoutError(
                f"dog_06: timed out after {elapsed:.1f}s (budget {timeout_s:.1f}s) "
                f"waiting on squash for: {', '.join(unmerged) or 'all PRs'}"
            )
        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# Timeline builder — assemble the RFC §4.2-narrative-shaped timeline
# ---------------------------------------------------------------------------


def _build_timeline(
    started_iso: str,
    pr_records: list[tuple[str, int, str]],
    enqueue_comment_ids: dict[int, int],
    observations: dict[int, _PRObservation],
    tree_diff_by_pr: dict[int, str],
    ended_iso: str,
) -> tuple[tuple[str, str, dict[str, Any]], ...]:
    """Assemble the timeline.

    The timeline is ORDERING-SENSITIVE: a reader walks it top-to-bottom and
    sees the RFC §4.2 narrative. Per the worked-example beats:
      1. All five ``pr_opened`` + ``merge_command_posted`` + ``mq_queued`` events
         (the enqueue moment for each PR, in A..E order — driver posts /merge
         sequentially even though some squashes parallelize later).
      2. For each PR (in A..E narrative order), the activation +
         active-status-posted + squash events.

    The activation/squash events use the wall-clock ``ended_iso`` as a
    placeholder timestamp — the per-event precise timing is captured in the
    processor_run_urls field; the timeline's purpose is RFC-narrative
    ordering, not microsecond chronology.
    """
    events: list[tuple[str, str, dict[str, Any]]] = []
    # Phase 1: enqueue events for all 5 PRs.
    for letter, pr_number, pr_url in pr_records:
        events.append(
            (
                started_iso,
                "pr_opened",
                {"pr_number": pr_number, "pr_url": pr_url, "letter": letter},
            )
        )
        events.append(
            (
                started_iso,
                "merge_command_posted",
                {
                    "pr_number": pr_number,
                    "letter": letter,
                    "comment_id": enqueue_comment_ids[pr_number],
                    "command": "/merge",
                },
            )
        )
        events.append(
            (
                started_iso,
                "mq_queued_label_applied",
                {
                    "pr_number": pr_number,
                    "letter": letter,
                    "labels": ["mq:queued"],
                },
            )
        )
    # Phase 2: per-PR activation + squash events in RFC §4.2 narrative order.
    for letter, pr_number, _pr_url in pr_records:
        obs = observations[pr_number]
        events.append(
            (
                ended_iso,
                "activation_began",
                {
                    "pr_number": pr_number,
                    "letter": letter,
                    "processor_run_url": obs.active_run_url,
                },
            )
        )
        events.append(
            (
                ended_iso,
                "merge_queue_active_status_posted",
                {
                    "pr_number": pr_number,
                    "letter": letter,
                    "processor_run_url": obs.active_run_url,
                },
            )
        )
        events.append(
            (
                ended_iso,
                "mq_active_label_applied",
                {"pr_number": pr_number, "letter": letter, "labels": ["mq:active"]},
            )
        )
        events.append(
            (
                ended_iso,
                "squash_merge_completed",
                {
                    "pr_number": pr_number,
                    "letter": letter,
                    "processor_run_url": obs.squash_run_url,
                    "tree_diff_status": tree_diff_by_pr.get(pr_number, ""),
                },
            )
        )
        events.append(
            (
                ended_iso,
                "mq_merged_label_applied",
                {"pr_number": pr_number, "letter": letter, "labels": ["mq:merged"]},
            )
        )
    return tuple(events)


# ---------------------------------------------------------------------------
# Invariant assertions
# ---------------------------------------------------------------------------


def _assert_ordering_invariant(
    timeline: tuple[tuple[str, str, dict[str, Any]], ...],
) -> list[str]:
    """Return a list of per-PR ordering-invariant violations (empty = OK).

    Per RFC §4.3 + plan 03-14 must_haves: any ``squash_merge_completed`` MUST
    be preceded by ``merge_queue_active_status_posted`` for the SAME PR. We
    walk the timeline once, recording the FIRST index of each event_type per
    PR; any squash whose active index is missing or later is a violation.
    """
    active_at: dict[int, int] = {}
    squash_at: dict[int, int] = {}
    for idx, (_ts, evt, state) in enumerate(timeline):
        pr_n = state.get("pr_number") if isinstance(state, dict) else None
        if not isinstance(pr_n, int):
            continue
        if evt == "merge_queue_active_status_posted" and pr_n not in active_at:
            active_at[pr_n] = idx
        elif evt == "squash_merge_completed" and pr_n not in squash_at:
            squash_at[pr_n] = idx
    violations: list[str] = []
    for pr_n, squash_idx in squash_at.items():
        active_idx = active_at.get(pr_n)
        if active_idx is None:
            violations.append(
                f"PR #{pr_n}: squash event present but no "
                f"merge_queue_active_status_posted event"
            )
        elif active_idx >= squash_idx:
            violations.append(
                f"PR #{pr_n}: active index {active_idx} must precede "
                f"squash index {squash_idx}"
            )
    return violations


def _assert_tree_diff_invariant(
    timeline: tuple[tuple[str, str, dict[str, Any]], ...],
) -> list[str]:
    """Return per-PR tree_diff_status invariant violations (empty = OK).

    Per plan 03-14 must_haves: every ``squash_merge_completed`` event MUST
    carry ``observed_state.tree_diff_status == 'ahead'``. Any other value
    (empty string sentinel, ``identical``, ``behind``, ``diverged``) is the
    Apr-2026 silent-corruption signal CONTEXT.md D-05 instructed DOG-06 to
    surface.
    """
    violations: list[str] = []
    for _ts, evt, state in timeline:
        if evt != "squash_merge_completed":
            continue
        if not isinstance(state, dict):
            continue
        status = state.get("tree_diff_status", "")
        if status != "ahead":
            pr_n = state.get("pr_number", "?")
            letter = state.get("letter", "?")
            violations.append(
                f"PR_{letter} (#{pr_n}): tree_diff_status={status!r}, "
                f"expected 'ahead' (Apr-2026 silent-corruption sentinel)"
            )
    return violations


# ---------------------------------------------------------------------------
# run_scenario — orchestration body
# ---------------------------------------------------------------------------


def run_scenario(
    client: Any,
    *,
    owner: str,
    repo: str,
    output_dir: Path | None = None,
    poll_interval_s: float = 15.0,
    poll_timeout_s: float | None = None,
    on_poll: Callable[[], None] | None = None,
) -> DogfoodResult:
    """Run DOG-06 end-to-end and emit the per-run JSON.

    Args:
        client: A ``GitHubClient`` (live) or a FakeGitHub extension (tests).
        owner / repo: Target repository (e.g., ``SamuelReeder/rocm-libraries``).
        output_dir: JSON output dir; defaults to D-04 location via ``emit_result``.
        poll_interval_s: Seconds between global poll iterations; tests pass 0.
        poll_timeout_s: Polling budget override; ``None`` uses ``TIMEOUT_S``.
        on_poll: Test-only seam — invoked BEFORE each poll iteration so the
            FakeGitHub fixture can advance the simulated processor cycles.
            Live-fork callers pass ``None``.

    Returns:
        A single ``DogfoodResult`` written to disk whose ``observed_outcome``
        carries per-PR sub-outcomes (one entry per PR letter).

    Raises:
        TimeoutError: Polling budget elapsed before all 5 PRs squash-merged.
    """
    timeout = float(TIMEOUT_S if poll_timeout_s is None else poll_timeout_s)
    started = datetime.now(tz=UTC).isoformat()

    # 1. Create the 5 PRs in A..E order. Each PR's per-descriptor paths
    #    materialize the RFC §4.2 queue-membership story; the title prefix
    #    ``[dogfood dog_06 PR_X]`` is load-bearing — tests rely on it to
    #    discover the per-letter PR number.
    pr_records: list[tuple[str, int, str]] = []  # (letter, pr_number, pr_url)
    for descriptor in PR_DESCRIPTORS:
        # Open the PR at the FIRST path; subsequent paths in the descriptor
        # would require multi-file commits which the base helper does not
        # expose. The RFC §4.2 queue-membership story holds as long as the
        # PR's changed-file set intersects at least the documented opted-in
        # path — single-file is sufficient and matches the other DOG-*
        # drivers' shape.
        primary_path = descriptor.paths[0]
        pr_number, pr_url = create_dogfood_pr(
            client,
            owner,
            repo,
            SCENARIO_ID,
            primary_path,
            (
                f"dogfood dog_06 PR_{descriptor.letter} — RFC §4.2 worked "
                f"example marker file. {descriptor.queue_membership_comment}\n"
            ),
            title_prefix=f"[dogfood dog_06 PR_{descriptor.letter}]",
            title_suffix="rfc-4-2-worked-example",
        )
        pr_records.append((descriptor.letter, pr_number, pr_url))

    # 2. Post /merge on each PR in A..E order. The handler sets ``mq:queued``
    #    on each PR; the processor's next cron cycles activate-and-squash
    #    per the queue-membership ordering (A first; B+C parallel; D; E).
    enqueue_comment_ids: dict[int, int] = {}
    for _letter, pr_number, _pr_url in pr_records:
        comment_id = post_command(client, owner, repo, pr_number, "/merge")
        enqueue_comment_ids[pr_number] = comment_id

    # 3. Poll globally until all 5 PRs carry a squash status comment.
    observations = _poll_all_prs_to_squash(
        client,
        owner,
        repo,
        pr_records,
        timeout_s=timeout,
        interval_s=poll_interval_s,
        on_poll=on_poll,
    )
    ended = datetime.now(tz=UTC).isoformat()

    # 4. Extract tree_diff_status per PR by string-matching the squash
    #    comment body first, then falling back to the cycle-summary artifact
    #    if the comment did not carry the marker (RESEARCH.md Area #11).
    tree_diff_by_pr: dict[int, str] = {}
    processor_run_url_list: list[str] = []
    for _letter, pr_number, _pr_url in pr_records:
        obs = observations[pr_number]
        # Pull the artifact only if needed (best-effort).
        fallback_summary = ""
        if _TREE_DIFF_AHEAD_MARKER not in obs.squash_body and obs.squash_run_url:
            run_id = _extract_run_id_from_url(obs.squash_run_url)
            if run_id is not None:
                fallback_summary = download_cycle_summary_artifact(
                    client, owner, repo, run_id
                )
        tree_diff_by_pr[pr_number] = _extract_tree_diff_status(
            obs.squash_body, fallback_summary
        )
        for url in (obs.active_run_url, obs.squash_run_url):
            if url and url not in processor_run_url_list:
                processor_run_url_list.append(url)

    # 5. Build the timeline. Ordering: enqueue events for all 5 in A..E
    #    order; then per-PR activation + squash events in A..E narrative
    #    order. A reviewer holding the JSON next to RFC §4.2 should follow
    #    the worked example top-to-bottom.
    timeline = _build_timeline(
        started_iso=started,
        pr_records=pr_records,
        enqueue_comment_ids=enqueue_comment_ids,
        observations=observations,
        tree_diff_by_pr=tree_diff_by_pr,
        ended_iso=ended,
    )

    # 6. Assert the two invariants. Failures do NOT raise — they record
    #    against ``passed`` and surface in the per-run JSON ``notes`` field
    #    so the evidence pack carries the diagnostic context.
    ordering_violations = _assert_ordering_invariant(timeline)
    tree_diff_violations = _assert_tree_diff_invariant(timeline)

    # 7. Build the per-PR sub-outcomes dict.
    per_pr_outcomes: dict[str, dict[str, Any]] = {}
    for letter, pr_number, pr_url in pr_records:
        obs = observations[pr_number]
        per_pr_outcomes[f"PR_{letter}"] = {
            "pr_number": pr_number,
            "pr_url": pr_url,
            "state": "merged" if obs.has_squash_comment else "not_merged",
            "active_run_url": obs.active_run_url,
            "squash_run_url": obs.squash_run_url,
            "tree_diff_status": tree_diff_by_pr.get(pr_number, ""),
        }
    observed_outcome: dict[str, Any] = {
        "action": "AllMerged",
        "pr_count": len(pr_records),
        "per_pr": per_pr_outcomes,
        "ordering_invariant_violations": ordering_violations,
        "tree_diff_invariant_violations": tree_diff_violations,
    }
    expected_outcome: dict[str, Any] = {
        "action": "AllMerged",
        "pr_count": 5,
        "per_pr_state_all": "merged",
        "ordering_invariant": (
            "merge_queue_active_status_posted precedes "
            "squash_merge_completed per PR"
        ),
        "tree_diff_invariant": (
            "tree_diff_status='ahead' on every squash_merge_completed"
        ),
    }

    # passed iff: every PR squash-merged AND both invariants hold.
    all_merged = all(
        observations[pr_number].has_squash_comment
        for (_letter, pr_number, _pr_url) in pr_records
    )
    passed = (
        all_merged
        and not ordering_violations
        and not tree_diff_violations
    )

    # PR_A's number is the primary pr_number; PR_A's URL is primary pr_url
    # but notes lists all five so the JSON still names every contributing PR.
    primary_letter, primary_number, primary_url = pr_records[0]
    pr_url_list_str = "; ".join(
        f"PR_{letter}: {url}" for (letter, _n, url) in pr_records
    )

    notes_parts = [
        f"5-PR RFC §4.2 worked example replay. Primary pr_url={primary_url!r} "
        f"(PR_{primary_letter}); all 5 PR URLs: {pr_url_list_str}.",
    ]
    if ordering_violations:
        notes_parts.append(
            "ORDERING INVARIANT VIOLATIONS: " + " | ".join(ordering_violations)
        )
    if tree_diff_violations:
        notes_parts.append(
            "TREE-DIFF INVARIANT VIOLATIONS (Apr-2026 silent-corruption signal): "
            + " | ".join(tree_diff_violations)
        )
    notes_parts.append(
        "Operational note: if driver runs longer than TIMEOUT_S due to real-CI "
        "variability, re-invocation does NOT clean up half-progress; operator "
        "must manually finalize partially-progressed PRs (clear mq:* labels, "
        "comment /dequeue) before retrying. Per plan 03-14 <action>."
    )

    result = DogfoodResult(
        scenario_id=SCENARIO_ID,
        run_started_at=started,
        run_ended_at=ended,
        pr_number=primary_number,
        pr_url=primary_url,
        expected_outcome=expected_outcome,
        observed_outcome=observed_outcome,
        timeline=timeline,
        processor_run_urls=tuple(processor_run_url_list),
        step_summary_excerpt="",  # captured via processor_run_urls + per-PR cycle-summary artifacts
        passed=passed,
        notes=" ".join(notes_parts),
    )
    emit_result(result, output_dir=output_dir)
    return result


# ---------------------------------------------------------------------------
# main() — CLI entrypoint (live-fork mode)
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"rocm-mq-{SCENARIO_ID}",
        description=(
            "Dogfood driver for DOG-06 (RFC §4.2 5-PR worked example replay). "
            "Marquee scenario — produces the Phase 5 porting-prep replay "
            "reference JSON. Requires GITHUB_TOKEN. Leaves PRs in place per D-04."
        ),
    )
    parser.add_argument(
        "--owner",
        required=True,
        help="GitHub repository owner (e.g., SamuelReeder).",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repository name (e.g., rocm-libraries).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Live-fork CLI entrypoint. Returns 0 on pass, 1 on fail, 2 on usage error."""
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(
            "error: GITHUB_TOKEN environment variable is not set; this driver "
            "requires a token with App-equivalent scopes (or an installation "
            "token minted via gh api).",
            file=sys.stderr,
        )
        return 2

    # Lazy import — avoids paying the githubkit import cost in unit-test mode.
    from rocm_mq.gh import GitHubClient

    client = GitHubClient(token=token)

    try:
        result = run_scenario(client, owner=args.owner, repo=args.repo)
    except TimeoutError as exc:
        print(f"dog_06: TIMEOUT — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"dog_06: error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1

    return 0 if result.passed else 1


__all__ = [
    "PR_DESCRIPTORS",
    "SCENARIO_ID",
    "TIMEOUT_S",
    "main",
    "run_scenario",
]


if __name__ == "__main__":
    sys.exit(main())
