"""Unit tests for rocm_mq.snapshot — raw-state assembly from GitHub responses.

Covers:
- ``_make_status_creator``: SimpleUser → CommitStatusCreator bridging.
  App-authored statuses are identified by creator type + slug: only the
  App's bot login plus type=="Bot" populates ``app_slug`` and ``app_id``.
  Workflow bots ("github-actions[bot]") and User-type creators leave the
  app fields ``None``.
- ``build_snapshot``:
  - skip ``pulls.list_files`` when PR already has ``mq:<queue>`` labels
  - call ``pulls.list_files`` when PR has no ``mq:*`` labels
  - timeline-event lag scenario → ``mq_queued_label_events=()``
  - incomplete search results → raises

Mocks the client.rest.* namespaces with SimpleNamespace + MagicMock to avoid
network calls and avoid constructing real githubkit Pydantic models.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from rocm_mq.snapshot import (
    _make_raw_pr_state,
    _make_status_creator,
    build_snapshot,
)
from rocm_mq.state import (
    CommitStatusCreator,
    RawPRState,
    RawSnapshot,
)
from tests.conftest import CANONICAL_APP, canonical_merge_queue_config

# ---------------------------------------------------------------------------
# Helpers: build SimpleUser / Status / TimelineEvent stand-ins.
# We intentionally avoid constructing real githubkit Pydantic models — the
# I/O adapter consumes any object with the right attribute surface, and the
# unit tests stay decoupled from githubkit model versioning churn.
# ---------------------------------------------------------------------------


def _simple_user(*, login: str, type_: str, id_: int = 1) -> SimpleNamespace:
    return SimpleNamespace(login=login, type=type_, id=id_)


def _status(
    *,
    context: str,
    state: str,
    creator: SimpleNamespace | None,
    created_at: str = "2026-04-22T15:23:45Z",
) -> SimpleNamespace:
    return SimpleNamespace(
        context=context,
        state=state,
        creator=creator,
        created_at=created_at,
    )


def _label_event(
    *,
    label_name: str,
    actor: SimpleNamespace,
    event: str = "labeled",
    created_at: str = "2026-04-22T15:23:45Z",
) -> SimpleNamespace:
    return SimpleNamespace(
        event=event,
        actor=actor,
        label=SimpleNamespace(name=label_name),
        created_at=created_at,
    )


def _pr(*, number: int, head_sha: str, labels: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        number=number,
        head=SimpleNamespace(sha=head_sha),
        labels=[SimpleNamespace(name=name) for name in labels],
    )


def _resp(data: Any) -> SimpleNamespace:
    """Wrap data in a ``.parsed_data`` accessor matching githubkit responses."""
    return SimpleNamespace(parsed_data=data)


# ---------------------------------------------------------------------------
# 1. _make_status_creator — App-authored statuses identified by creator
# type + slug (not by login alone)
# ---------------------------------------------------------------------------


def test_make_status_creator__canonical_app__populates_app_slug_and_id() -> None:
    """Bot login matching f'{slug}[bot]' → app_slug/app_id populated from config."""
    config = canonical_merge_queue_config()
    raw = _simple_user(login="rocm-mq[bot]", type_="Bot", id_=99999)
    creator = _make_status_creator(raw, config)
    assert creator == CommitStatusCreator(
        login="rocm-mq[bot]",
        type="Bot",
        app_slug="rocm-mq",
        app_id=12345,
    )


def test_make_status_creator__wrong_login__no_app_fields() -> None:
    """Sibling workflow github-actions[bot] does NOT match → app fields None."""
    config = canonical_merge_queue_config()
    raw = _simple_user(login="github-actions[bot]", type_="Bot")
    creator = _make_status_creator(raw, config)
    assert creator.app_slug is None
    assert creator.app_id is None
    assert creator.login == "github-actions[bot]"
    assert creator.type == "Bot"


def test_make_status_creator__user_type__no_app_fields() -> None:
    """type='User' (impersonator account) → app fields None."""
    config = canonical_merge_queue_config()
    raw = _simple_user(login="rocm-mq", type_="User")
    creator = _make_status_creator(raw, config)
    assert creator.app_slug is None
    assert creator.app_id is None


def test_make_status_creator__none_creator__returns_empty() -> None:
    """A None creator (statuses occasionally lack one) → empty creator stub."""
    config = canonical_merge_queue_config()
    creator = _make_status_creator(None, config)
    assert creator.login == ""
    assert creator.type == "User"  # benign default — fails is_app_identity by type
    assert creator.app_slug is None
    assert creator.app_id is None


# ---------------------------------------------------------------------------
# 2. _map_check_run_state tests removed — required-check mapping is no
#    longer the queue's concern (branch protection is the source of truth).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3. build_snapshot — label/file optimization, search incompleteness,
# timeline lag
# ---------------------------------------------------------------------------


class _FakeRest:
    """Container exposing the .rest.* sub-namespaces as MagicMocks."""

    def __init__(self) -> None:
        self.search = MagicMock()
        self.pulls = MagicMock()
        self.repos = MagicMock()
        self.issues = MagicMock()
        self.checks = MagicMock()


class _FakeClient:
    """Minimal stand-in for GitHubClient — only .rest is exercised."""

    def __init__(self) -> None:
        self.rest = _FakeRest()


def _arm_single_pr(
    rest: _FakeRest,
    *,
    pr_number: int,
    head_sha: str,
    labels: list[str],
    queue_name: str = "hipdnn",
    timeline_events: list[SimpleNamespace] | None = None,
    statuses: list[SimpleNamespace] | None = None,
    files: list[str] | None = None,
) -> None:
    """Wire up the minimum API responses for one PR in one queue."""
    pr_obj = _pr(number=pr_number, head_sha=head_sha, labels=labels)

    # search → one issue (just need .number for the per-PR fan-out)
    search_data = SimpleNamespace(
        incomplete_results=False,
        items=[SimpleNamespace(number=pr_number)],
    )
    # Each search call (one per queue) yields the same data; the implementation
    # de-duplicates by PR number so the same PR appears only once in the snapshot.
    rest.search.issues_and_pull_requests.return_value = _resp(search_data)

    rest.pulls.get.return_value = _resp(pr_obj)
    rest.pulls.list_files.return_value = _resp(
        [SimpleNamespace(filename=f) for f in (files or [])]
    )
    rest.repos.list_commit_statuses_for_ref.return_value = _resp(statuses or [])
    rest.issues.list_events_for_timeline.return_value = _resp(timeline_events or [])

    # Make queue_name available for the caller (unused here but documents intent).
    _ = queue_name


def test_build_snapshot__skips_list_files_when_queue_labels_present() -> None:
    """PR with mq:<queue> labels → pulls.list_files NOT called; changed_paths=().

    The queue labels already encode the path-to-queue mapping result, so
    re-querying the file list every cycle is wasted API budget.
    """
    client = _FakeClient()
    config = canonical_merge_queue_config()

    _arm_single_pr(
        client.rest,
        pr_number=42,
        head_sha="abc1234",
        labels=["mq:queued", "mq:hipdnn"],
    )
    # If list_files is called, raise to fail the test loudly.
    client.rest.pulls.list_files.side_effect = AssertionError(
        "list_files must not be called when mq:<queue> labels present"
    )

    snap = build_snapshot(client, config, owner="SamuelReeder", repo="rocm-libraries")
    assert isinstance(snap, RawSnapshot)
    assert len(snap.prs) == 1
    assert snap.prs[0].number == 42
    assert snap.prs[0].changed_paths == ()


def test_build_snapshot__calls_list_files_when_no_queue_labels() -> None:
    """PR with no mq:* labels → pulls.list_files IS called; changed_paths populated."""
    client = _FakeClient()
    config = canonical_merge_queue_config()

    _arm_single_pr(
        client.rest,
        pr_number=43,
        head_sha="def4567",
        labels=[],  # no labels at all
        files=["projects/hipdnn/foo.cpp", "README.md"],
    )

    snap = build_snapshot(client, config, owner="SamuelReeder", repo="rocm-libraries")
    assert client.rest.pulls.list_files.called
    assert snap.prs[0].changed_paths == ("projects/hipdnn/foo.cpp", "README.md")


def test_build_snapshot__timeline_lag__mq_queued_events_empty_when_lag_simulated() -> None:
    """Timeline returning no events (label-event visibility lag) → mq_queued_label_events=()."""
    client = _FakeClient()
    config = canonical_merge_queue_config()

    _arm_single_pr(
        client.rest,
        pr_number=44,
        head_sha="abc0000",
        labels=["mq:queued", "mq:hipdnn"],
        timeline_events=[],  # lag: events not yet visible
    )

    snap = build_snapshot(client, config, owner="SamuelReeder", repo="rocm-libraries")
    assert snap.prs[0].mq_queued_label_events == ()


def test_build_snapshot__timeline_lag__mq_queued_event_present_populates_tuple() -> None:
    """Timeline returning a queued label event → mq_queued_label_events has it."""
    client = _FakeClient()
    config = canonical_merge_queue_config()

    bot_actor = _simple_user(login="rocm-mq[bot]", type_="Bot", id_=CANONICAL_APP.bot_user_id)
    queued_event = _label_event(
        label_name=config.queued_label,
        actor=bot_actor,
        created_at="2026-04-22T10:00:00Z",
    )
    # Add an unrelated event to confirm filtering by label name.
    unrelated = _label_event(label_name="bug", actor=bot_actor)

    _arm_single_pr(
        client.rest,
        pr_number=45,
        head_sha="bbb0001",
        labels=["mq:queued", "mq:hipdnn"],
        timeline_events=[unrelated, queued_event],
    )

    snap = build_snapshot(client, config, owner="SamuelReeder", repo="rocm-libraries")
    events = snap.prs[0].mq_queued_label_events
    assert len(events) == 1
    assert events[0].label_name == config.queued_label
    assert events[0].actor.login == "rocm-mq[bot]"
    assert events[0].actor.user_id == CANONICAL_APP.bot_user_id


def test_build_snapshot__incomplete_results_raises() -> None:
    """Search returning incomplete_results=True must abort the cycle.

    An incomplete result set could drop a head-of-queue PR from the snapshot
    and silently violate FIFO/head-of-all-queues invariants; the only safe
    response is to fail loud and let the next cycle retry.
    """
    client = _FakeClient()
    config = canonical_merge_queue_config()

    search_data = SimpleNamespace(incomplete_results=True, items=[])
    client.rest.search.issues_and_pull_requests.return_value = _resp(search_data)

    with pytest.raises(AssertionError):
        build_snapshot(client, config, owner="SamuelReeder", repo="rocm-libraries")


def test_build_snapshot__search_query_quotes_label_value() -> None:
    """The search query must wrap the mq:<queue> label in double quotes.

    GitHub search's label: qualifier needs quoted values when they contain
    colons (e.g., mq:hipdnn); without quotes the search may silently miss
    PRs and the federated merge queue would forget them (RFC §6
    head-of-all-queues violation).
    """
    client = _FakeClient()
    config = canonical_merge_queue_config()
    search_data = SimpleNamespace(
        incomplete_results=False,
        items=[],
        total_count=0,
    )
    client.rest.search.issues_and_pull_requests.return_value = _resp(search_data)

    build_snapshot(client, config, owner="SamuelReeder", repo="rocm-libraries")

    # Every call must use the quoted label form.
    assert client.rest.search.issues_and_pull_requests.called
    for call in client.rest.search.issues_and_pull_requests.call_args_list:
        q = call.kwargs.get("q") or (call.args[0] if call.args else "")
        assert 'label:"' in q, (
            f"search query missing quoted label form: q={q!r}"
        )


def test_build_snapshot__search_total_count_exceeds_items__raises() -> None:
    """total_count > len(items) implies pagination is needed.

    Full pagination support is a future follow-up; the assertion guards
    against silently dropping PRs from the snapshot when a queue grows
    past per_page=100.
    """
    client = _FakeClient()
    config = canonical_merge_queue_config()
    # Search reports 250 total but only returns 100 items → silent truncation
    # without the guard.
    items = [SimpleNamespace(number=n) for n in range(1, 101)]
    search_data = SimpleNamespace(
        incomplete_results=False,
        items=items,
        total_count=250,
    )
    client.rest.search.issues_and_pull_requests.return_value = _resp(search_data)

    with pytest.raises(AssertionError, match="pagination is required"):
        build_snapshot(client, config, owner="SamuelReeder", repo="rocm-libraries")


# (test_build_snapshot__combines_check_runs_and_commit_statuses removed:
# check_runs are no longer loaded into required_check_results; branch
# protection is the source of truth for required-check enforcement.)


def test_build_snapshot__deduplicates_pr_across_queue_searches() -> None:
    """A PR appearing in 2 queue search results is fetched once and appears once."""
    client = _FakeClient()
    config = canonical_merge_queue_config()

    # search returns the same PR for every queue → snapshot should contain 1 entry.
    search_data = SimpleNamespace(
        incomplete_results=False,
        items=[SimpleNamespace(number=47)],
    )
    client.rest.search.issues_and_pull_requests.return_value = _resp(search_data)
    pr_obj = _pr(number=47, head_sha="ddd", labels=["mq:queued", "mq:hipdnn"])
    client.rest.pulls.get.return_value = _resp(pr_obj)
    client.rest.repos.list_commit_statuses_for_ref.return_value = _resp([])
    client.rest.issues.list_events_for_timeline.return_value = _resp([])

    snap = build_snapshot(client, config, owner="SamuelReeder", repo="rocm-libraries")
    assert len(snap.prs) == 1
    assert snap.prs[0].number == 47
    # And the per-PR fan-out (.pulls.get) should be called exactly once.
    assert client.rest.pulls.get.call_count == 1


# ---------------------------------------------------------------------------
# 4. _make_raw_pr_state — direct unit test of the assembly helper
# ---------------------------------------------------------------------------


def test_make_raw_pr_state__assembles_frozen_dataclass() -> None:
    """_make_raw_pr_state returns a frozen RawPRState with all fields populated."""
    config = canonical_merge_queue_config()
    pr_obj = _pr(number=99, head_sha="head_sha_x", labels=["mq:queued", "mq:hipdnn"])
    bot_creator = _simple_user(login="rocm-mq[bot]", type_="Bot")
    statuses = [_status(context="merge-queue/active", state="success", creator=bot_creator)]
    bot_actor = _simple_user(login="rocm-mq[bot]", type_="Bot", id_=CANONICAL_APP.bot_user_id)
    timeline = [_label_event(label_name=config.queued_label, actor=bot_actor)]
    files: list[str] = []

    raw = _make_raw_pr_state(
        pr=pr_obj,
        statuses=statuses,
        timeline=timeline,
        files=files,
        config=config,
    )
    assert isinstance(raw, RawPRState)
    assert raw.number == 99
    assert raw.head_sha == "head_sha_x"
    assert raw.labels == frozenset({"mq:queued", "mq:hipdnn"})
    assert len(raw.head_statuses) == 1
    assert raw.head_statuses[0].creator.app_id == CANONICAL_APP.app_id
    assert len(raw.mq_queued_label_events) == 1
    assert raw.changed_paths == ()
