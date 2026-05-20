"""Tests for rocm_mq.dogfood.dog_05 — author-push-after-activation eject driver.

Scenario contract (RFC §6 DOG-05, plan 03-13): a PR opted into a queue gets
``/merge``d, transitions through ``mq:queued`` → ``mq:active`` (activation
succeeds, ``merge-queue/active`` commit status created on the activation-time
head SHA), then the author pushes a NEW commit to the PR branch. The new head
SHA does not carry the ``merge-queue/active`` status → at the next processor
cycle ``pr.is_validly_active`` is False → the processor ejects with the
documented reason ``"activation invalid (branch updated or label tampered)"``
(decision.py § activation invariant, RFC §4.6 step 5b).

Two-mode driver per CONTEXT.md D-04. Unit-test mode (this file) exercises
orchestration against a FakeGitHub extension that:
  1. Applies the ``mq:active`` label after ``/merge`` is observed (simulates
     the processor's activation step).
  2. On the author-push call, mutates the FakePR's ``head_sha``.
  3. Once polled after the push, injects the eject status-comment carrying the
     verbatim RFC §6 reason string.

Live-fork mode (``python -m rocm_mq.dogfood.dog_05 --owner SamuelReeder --repo
rocm-libraries``) is operator-initiated AFTER mq-handler.yml + mq-processor.yml
are deployed; NOT exercised in CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rocm_mq.dogfood import dog_05
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

# ---------------------------------------------------------------------------
# Module-level constants — locked by plan 03-13 Task 1 behavior block.
# ---------------------------------------------------------------------------


def test_scenario_id_is_dog_05() -> None:
    assert dog_05.SCENARIO_ID == "dog_05"


def test_expected_outcome_pinned_to_rfc_eject_reason() -> None:
    """EXPECTED.reason must be the literal RFC §6 / decision.py verbatim string."""
    assert dog_05.EXPECTED == {
        "action": "Eject",
        "reason": "activation invalid (branch updated or label tampered)",
    }


def test_timeout_budget_15_minutes() -> None:
    # RESEARCH.md Area #10 / PATTERNS.md timeout-budget table.
    assert dog_05.TIMEOUT_S == 15 * 60


def test_main_is_callable() -> None:
    assert callable(dog_05.main)


def test_run_scenario_is_callable() -> None:
    assert callable(dog_05.run_scenario)


# ---------------------------------------------------------------------------
# _DogfoodFake — simulates activation (mq:active label) + post-push eject
# ---------------------------------------------------------------------------


_STATUS_MARKER = "<!-- rocm-mq-status -->"
_EJECT_REASON = "activation invalid (branch updated or label tampered)"


class _DogfoodFake(FakeGitHub):
    """FakeGitHub with the git/repos/pulls extensions dog_05 needs.

    Wires:
      * ``rest.git.get_ref`` / ``create_ref`` — branch creation off develop.
      * ``rest.repos.create_or_update_file_contents`` — seed-file commit AND
        author-push commit on the PR branch. The SECOND commit on the
        already-existing PR branch is taken as the author-push event and
        mutates the FakePR's ``head_sha`` so subsequent ``pulls.get`` reflects
        the new head (simulates the processor seeing a head SHA without the
        ``merge-queue/active`` status).
      * ``rest.pulls.create`` — PR creation; also seeds the FakePR with the
        ``mq:queued`` label (simulating the handler's enqueue step).
      * ``rest.issues.create_comment`` wrapper — on a ``/merge`` body, adds
        the ``mq:active`` label so the driver's activation-poll observes it
        immediately (no separate cycle is modeled here — activation is the
        deterministic prerequisite for this scenario).
      * ``inject_eject_after_push`` — test-only seam invoked by the eject
        predicate; once the head SHA has mutated past the activation SHA,
        synthesizes the processor's eject status comment.
    """

    def __init__(
        self,
        state: FakeRepoState,
        *,
        eject_comment_body: str | None = None,
        auto_activate_on_merge_cmd: bool = True,
        mutate_head_on_second_branch_commit: bool = True,
    ) -> None:
        super().__init__(state)
        self._next_pr_number = 5000
        self._refs: dict[str, str] = {"heads/develop": "develop_initial_tip"}
        self._created_files: list[dict[str, Any]] = []
        self._created_pulls: list[dict[str, Any]] = []
        self._eject_comment_body = eject_comment_body
        self._comment_posted = False
        self._auto_activate = auto_activate_on_merge_cmd
        self._mutate_head = mutate_head_on_second_branch_commit
        # Per-branch commit counter — keyed by branch name; the SECOND commit
        # to an already-known PR branch is the "author push" event.
        self._branch_commit_count: dict[str, int] = {}
        # Map branch name → PR number, populated on _create_pull.
        self._branch_to_pr: dict[str, int] = {}
        # Wire sub-namespaces (kept LOCAL per the 03-10 pattern).
        self.rest.git = _GitNS(self._refs)
        self.rest.repos.create_or_update_file_contents = (  # type: ignore[attr-defined]
            self._create_or_update_file
        )
        self.rest.pulls.create = self._create_pull  # type: ignore[attr-defined]
        # Wrap create_comment so a ``/merge`` body triggers the simulated
        # activation (label flip queued → active).
        self._base_create_comment = self.rest.issues.create_comment
        self.rest.issues.create_comment = self._wrapped_create_comment  # type: ignore[assignment]

    # -- repo file writes (seed + author-push) -------------------------------

    def _create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        message: str,
        content: str,
        branch: str = "",
        **_: Any,
    ) -> SimpleNamespace:
        self._created_files.append(
            {"path": path, "branch": branch, "message": message}
        )
        # Count commits per branch; the SECOND commit to a PR-owned branch
        # is the simulated author-push event.
        prior = self._branch_commit_count.get(branch, 0)
        self._branch_commit_count[branch] = prior + 1
        if self._mutate_head and prior >= 1 and branch in self._branch_to_pr:
            pr_number = self._branch_to_pr[branch]
            pr = self.state.prs.get(pr_number)
            if pr is not None:
                # Mutate the head SHA — the processor's next cycle will see
                # a head SHA that does NOT carry the merge-queue/active status.
                pr.head_sha = f"head_{pr_number}_after_author_push"
        commit_sha = f"commit_{len(self._created_files)}"
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                commit=SimpleNamespace(sha=commit_sha)
            )
        )

    # -- PR creation (seeds mq:queued label) ---------------------------------

    def _create_pull(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str = "",
        **_: Any,
    ) -> SimpleNamespace:
        number = self._next_pr_number
        self._next_pr_number += 1
        self._created_pulls.append({"number": number, "head": head, "title": title})
        # Seed the FakePR with the initial activation-time head SHA. The
        # ``mq:queued`` label is added on ``/merge`` below; ``mq:active`` is
        # added on the same /merge call by the auto-activate path so the
        # driver's polling-for-active step observes it deterministically.
        self.state.prs[number] = FakePR(
            number=number, head_sha=f"head_{number}_at_activation"
        )
        self._branch_to_pr[head] = number
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                number=number,
                html_url=f"https://github.test/{owner}/{repo}/pull/{number}",
            )
        )

    # -- comment wrapper (auto-activate on /merge) ---------------------------

    def _wrapped_create_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        body: str = "",
        **kwargs: Any,
    ) -> SimpleNamespace:
        resp = self._base_create_comment(owner, repo, issue_number, body=body, **kwargs)
        if self._auto_activate and body.strip() == "/merge":
            pr = self.state.prs.get(issue_number)
            if pr is not None:
                # Simulate the handler enqueue + processor activation
                # collapsed into a single step (the scenario tests post-
                # activation behavior, not the activation transition itself).
                pr.labels.add("mq:queued")
                pr.labels.add("mq:active")
        return resp

    # -- post-push eject injection -------------------------------------------

    def inject_eject_after_push(self, pr_number: int) -> None:
        """Inject the eject status comment ONLY after the author-push happened.

        Mirrors the live processor's behavior: the eject is the consequence
        of observing a head SHA without the ``merge-queue/active`` status.
        Until the head SHA mutates (which only happens on the second branch
        commit), no eject comment is posted — the predicate keeps polling.
        """
        if self._eject_comment_body is None:
            return
        if self._comment_posted:
            return
        pr = self.state.prs.get(pr_number)
        if pr is None:
            return
        if "after_author_push" not in pr.head_sha:
            return
        self._base_create_comment(
            "owner", "repo", pr_number, body=self._eject_comment_body
        )
        self._comment_posted = True


class _GitNS:
    def __init__(self, refs: dict[str, str]) -> None:
        self._refs = refs

    def get_ref(self, owner: str, repo: str, ref: str) -> SimpleNamespace:
        sha = self._refs.get(ref, "develop_initial_tip")
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                ref=f"refs/{ref}", object=SimpleNamespace(sha=sha)
            )
        )

    def create_ref(
        self, owner: str, repo: str, *, ref: str, sha: str, **_: Any
    ) -> SimpleNamespace:
        short = ref[len("refs/") :] if ref.startswith("refs/") else ref
        self._refs[short] = sha
        return SimpleNamespace(
            parsed_data=SimpleNamespace(ref=ref, object=SimpleNamespace(sha=sha))
        )


# ---------------------------------------------------------------------------
# run_scenario — happy path + failure modes
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_state() -> FakeRepoState:
    return FakeRepoState()


def _patch_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op sleep so poll_pr_state does not block."""
    monkeypatch.setattr("rocm_mq.dogfood._base.time.sleep", lambda _s: None)


def test_run_scenario_happy_path_returns_passed_result(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Activation observed → author push → eject with documented reason → passed=True."""
    _patch_timing(monkeypatch)
    eject_body = (
        "<!-- rocm-mq-status -->\n"
        "## Ejected: activation invalid (branch updated or label tampered)\n"
        "Processor cycle: https://github.test/x/y/actions/runs/999\n"
    )
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    result = dog_05.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_after_push,
    )

    assert result.passed is True
    assert result.scenario_id == "dog_05"
    assert result.expected_outcome == dog_05.EXPECTED
    assert result.observed_outcome["action"] == "Eject"
    assert result.observed_outcome["reason"] == _EJECT_REASON
    # A PR was created.
    assert result.pr_number == 5000
    # JSON emitted to the per-run output dir.
    written = list(tmp_path.glob("*-dog_05.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["scenario_id"] == "dog_05"


def test_run_scenario_emits_d04_schema_fields(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Per-run JSON includes every D-04 schema field."""
    _patch_timing(monkeypatch)
    eject_body = f"<!-- rocm-mq-status -->\nEjected: {_EJECT_REASON}\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    dog_05.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_after_push,
    )

    written = list(tmp_path.glob("*-dog_05.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    for field in (
        "scenario_id",
        "run_started_at",
        "run_ended_at",
        "pr_number",
        "pr_url",
        "expected_outcome",
        "observed_outcome",
        "timeline",
        "processor_run_urls",
        "step_summary_excerpt",
        "passed",
        "notes",
    ):
        assert field in payload, f"missing D-04 field: {field}"


def test_run_scenario_pushes_a_second_commit_to_the_pr_branch(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """The driver must write a SECOND commit to the PR branch (the author-push).

    Per plan 03-13 must_haves: 'pushes a new commit to the PR branch
    (simulating author push between activation and squash)'. The seed-file
    commit goes to the branch on PR open; the author-push commit goes to the
    SAME branch at a different path.
    """
    _patch_timing(monkeypatch)
    eject_body = f"<!-- rocm-mq-status -->\nEjected: {_EJECT_REASON}\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    dog_05.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_after_push,
    )

    # Exactly two file commits: the seed (on PR open) and the author-push.
    assert len(client._created_files) == 2
    seed_commit, push_commit = client._created_files
    # Both commits go to the SAME PR branch (the dogfood/dog_05-* branch).
    assert seed_commit["branch"] == push_commit["branch"]
    assert seed_commit["branch"].startswith("dogfood/dog_05-")
    # Files are at different paths (a fresh new path for the author-push so
    # the contents API write does not collide with the seed file's sha).
    assert seed_commit["path"] != push_commit["path"]


def test_run_scenario_mutates_head_sha_after_push(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """After the author-push, the PR's head_sha must differ from the activation SHA."""
    _patch_timing(monkeypatch)
    eject_body = f"<!-- rocm-mq-status -->\nEjected: {_EJECT_REASON}\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    result = dog_05.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_after_push,
    )

    pr = fake_state.prs[result.pr_number]
    # The head SHA must reflect the post-push mutation — the activation-time
    # SHA was ``head_5000_at_activation``; the post-push SHA carries the
    # author-push marker.
    assert "after_author_push" in pr.head_sha
    assert pr.head_sha != f"head_{result.pr_number}_at_activation"


def test_run_scenario_failure_mode_when_reason_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Status comment posted but reason text differs → timeout (no false positive).

    The predicate must only succeed on the documented verbatim eject reason;
    a different eject reason landing on the same PR (e.g., a merge-conflict
    eject) must NOT be accepted as DOG-05 evidence.
    """
    _patch_timing(monkeypatch)
    # An eject comment naming the WRONG reason — must keep polling and
    # eventually time out.
    eject_body = (
        "<!-- rocm-mq-status -->\n## Ejected: merge conflict with develop\n"
    )
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    with pytest.raises(TimeoutError):
        dog_05.run_scenario(
            client,
            owner="owner",
            repo="repo",
            output_dir=tmp_path,
            poll_interval_s=0,
            poll_timeout_s=0.05,
            inject_eject_after=client.inject_eject_after_push,
        )


def test_run_scenario_failure_mode_when_no_activation_observed(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """If activation never happens (no mq:active label), driver times out at
    the activation poll — the author-push step never fires.

    Guards against a regression where the driver pushes a commit BEFORE
    activation is confirmed (which would not exercise the WF-12 path).
    """
    _patch_timing(monkeypatch)
    client = _DogfoodFake(
        fake_state,
        eject_comment_body=f"<!-- rocm-mq-status -->\nEjected: {_EJECT_REASON}\n",
        auto_activate_on_merge_cmd=False,  # processor never activates
    )

    with pytest.raises(TimeoutError):
        dog_05.run_scenario(
            client,
            owner="owner",
            repo="repo",
            output_dir=tmp_path,
            poll_interval_s=0,
            poll_timeout_s=0.05,
            inject_eject_after=client.inject_eject_after_push,
        )

    # The author-push must NOT have fired (only the seed commit exists).
    assert len(client._created_files) == 1


def test_run_scenario_timeline_records_author_push_event(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Timeline must include an ``author_push_after_activation`` event per plan."""
    _patch_timing(monkeypatch)
    eject_body = f"<!-- rocm-mq-status -->\nEjected: {_EJECT_REASON}\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    result = dog_05.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_after_push,
    )

    event_types = [evt for (_ts, evt, _state) in result.timeline]
    # The plan's <action> calls out a custom ``author_push_after_activation``
    # event_type in the timeline subset.
    assert "author_push_after_activation" in event_types
    # The mq_active_label_applied event marks the activation observation.
    assert "mq_active_label_applied" in event_types
    # And the terminal ejected event records the documented reason.
    assert "ejected" in event_types
    push_evt = next(
        s for (_ts, evt, s) in result.timeline if evt == "author_push_after_activation"
    )
    # The recorded event must carry the post-push head SHA so the audit
    # trail in the per-run JSON shows the SHA the processor rejected.
    assert "after_author_push" in json.dumps(push_evt)
