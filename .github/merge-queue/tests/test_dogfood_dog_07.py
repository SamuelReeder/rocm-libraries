"""Tests for rocm_mq.dogfood.dog_07 — approval-revoked eject driver.

Scenario contract (RFC §6 DOG-07, plan 03-15): a PR opted into a queue receives
a required approving review from a SECOND identity (PR author cannot self-
approve under branch protection); operator/driver posts ``/merge``; processor
applies ``mq:queued`` then ``mq:active``; the second-identity approver then
DISMISSES their review (``pulls.dismiss_review``); on the next processor cycle
the activation re-evaluation observes that the required-review gate is no
longer satisfied → the processor ejects with the documented reason
``"approval revoked"`` (RFC §5 defense-in-depth: required-review gate must
apply throughout queue lifecycle, not just at enqueue time).

Two-mode driver per CONTEXT.md D-04. Unit-test mode (this file) exercises
orchestration against a FakeGitHub extension that:
  1. Records the second-identity approval as a review on the FakePR.
  2. Applies ``mq:queued`` + ``mq:active`` labels after ``/merge`` is observed
     (simulates the processor's activation step).
  3. On ``pulls.dismiss_review``, mutates the review state from APPROVED →
     DISMISSED so the next ``pulls.list_reviews`` reflects the revocation.
  4. Once polled after the dismissal, injects the eject status-comment
     carrying the verbatim RFC §6 reason string.

Live-fork mode (``python -m rocm_mq.dogfood.dog_07 --owner SamuelReeder
--repo rocm-libraries``) is operator-initiated AFTER mq-handler.yml +
mq-processor.yml are deployed AND a second-collaborator account has been
provisioned on the fork with a PAT exported as APPROVER_TOKEN (plan 03-15
Task 1 option-a outcome). NOT exercised in CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rocm_mq.dogfood import dog_07
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

# ---------------------------------------------------------------------------
# Module-level constants — locked by plan 03-15 Task 2 behavior block.
# ---------------------------------------------------------------------------


def test_scenario_id_is_dog_07() -> None:
    assert dog_07.SCENARIO_ID == "dog_07"


def test_expected_outcome_pinned_to_rfc_eject_reason() -> None:
    """EXPECTED.reason must be the literal RFC §6 verbatim string."""
    assert dog_07.EXPECTED == {
        "action": "Eject",
        "reason": "approval revoked",
    }


def test_timeout_budget_15_minutes() -> None:
    # RESEARCH.md Area #10 — DOG-07 budget is 15 min (activation + dismissal
    # + ≥1 cron cycle for eject).
    assert dog_07.TIMEOUT_S == 15 * 60


def test_main_is_callable() -> None:
    assert callable(dog_07.main)


def test_run_scenario_is_callable() -> None:
    assert callable(dog_07.run_scenario)


# ---------------------------------------------------------------------------
# _DogfoodFake — simulates second-account approval + activation + dismissal
# + post-dismissal eject
# ---------------------------------------------------------------------------


_STATUS_MARKER = "<!-- rocm-mq-status -->"
_EJECT_REASON = "approval revoked"
_APPROVER_LOGIN = "samuel-reeder-bot"


class _DogfoodFake(FakeGitHub):
    """FakeGitHub with the git/repos/pulls/reviews extensions dog_07 needs.

    Wires:
      * ``rest.git.get_ref`` / ``create_ref`` — branch creation off develop.
      * ``rest.repos.create_or_update_file_contents`` — seed-file commit on
        the PR branch (single commit; dog_07 does NOT push a second commit,
        unlike dog_05).
      * ``rest.pulls.create`` — PR creation; seeds the FakePR.
      * ``rest.pulls.create_review`` — second-identity approval. Records the
        review on the FakePR.reviews list with state=APPROVED and returns a
        SimpleNamespace carrying a stable review id (counter-based) so the
        driver can later dismiss it.
      * ``rest.pulls.dismiss_review`` — flips the recorded review's state
        from APPROVED → DISMISSED. The driver's poll for eject runs AFTER
        this call; the eject predicate calls ``inject_eject_after_dismissal``
        which checks the review state before posting the eject comment.
      * ``rest.issues.create_comment`` wrapper — on a ``/merge`` body, adds
        the ``mq:queued`` + ``mq:active`` labels (simulates handler enqueue +
        processor activation collapsed into one step — this scenario tests
        post-activation behavior, not the activation transition itself).
      * ``inject_eject_after_dismissal`` — test-only seam invoked by the
        eject predicate; once the recorded review's state is no longer
        APPROVED, synthesizes the processor's eject status comment.
    """

    def __init__(
        self,
        state: FakeRepoState,
        *,
        eject_comment_body: str | None = None,
        auto_activate_on_merge_cmd: bool = True,
    ) -> None:
        super().__init__(state)
        self._next_pr_number = 7000
        self._refs: dict[str, str] = {"heads/develop": "develop_initial_tip"}
        self._created_files: list[dict[str, Any]] = []
        self._created_pulls: list[dict[str, Any]] = []
        self._eject_comment_body = eject_comment_body
        self._comment_posted = False
        self._auto_activate = auto_activate_on_merge_cmd
        # Map pr_number → list of (review_id, current_state) — owned by this
        # fake (not by FakePR) so we can mutate state on dismiss without
        # touching FakePR's reviews list (which mirrors the GH list_reviews
        # response shape the production gh_fake.list_reviews already returns).
        self._review_state: dict[int, dict[int, str]] = {}
        self._next_review_id = 9001
        # Branch tracking.
        self._branch_to_pr: dict[str, int] = {}
        # Wire sub-namespaces (kept LOCAL per the 03-10 pattern; do not
        # mutate gh_fake's shared classes — every dogfood test owns its
        # extensions per the dog_05 pattern).
        self.rest.git = _GitNS(self._refs)
        self.rest.repos.create_or_update_file_contents = (  # type: ignore[attr-defined]
            self._create_or_update_file
        )
        self.rest.pulls.create = self._create_pull  # type: ignore[attr-defined]
        self.rest.pulls.create_review = self._create_review  # type: ignore[attr-defined]
        self.rest.pulls.dismiss_review = self._dismiss_review  # type: ignore[attr-defined]
        # Wrap create_comment so a ``/merge`` body triggers the simulated
        # enqueue+activation (label flip).
        self._base_create_comment = self.rest.issues.create_comment
        self.rest.issues.create_comment = self._wrapped_create_comment  # type: ignore[assignment]

    # -- repo file writes (seed only — no author-push for DOG-07) ------------

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
        self._created_files.append({"path": path, "branch": branch, "message": message})
        commit_sha = f"commit_{len(self._created_files)}"
        return SimpleNamespace(parsed_data=SimpleNamespace(commit=SimpleNamespace(sha=commit_sha)))

    # -- PR creation ----------------------------------------------------------

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
        # Seed the FakePR. The mq:queued / mq:active labels are added on
        # /merge below; the approving review is added via _create_review
        # earlier in the driver flow (the second-identity client posts the
        # review BEFORE /merge so the at-enqueue gate sees the approval).
        self.state.prs[number] = FakePR(
            number=number,
            head_sha=f"head_{number}_initial",
            user_login="dogfood-author",
        )
        self._branch_to_pr[head] = number
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                number=number,
                html_url=f"https://github.test/{owner}/{repo}/pull/{number}",
                head=SimpleNamespace(ref=head, sha=f"head_{number}_initial"),
            )
        )

    # -- second-identity approval --------------------------------------------

    def _create_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        *,
        event: str = "",
        **_: Any,
    ) -> SimpleNamespace:
        """Simulate the second-identity approver posting an APPROVE review.

        Records the review on FakePR.reviews (the gh_fake list_reviews shape
        the at-enqueue gate reads) AND on this fake's _review_state map (the
        per-review-id dismissal-tracking surface dismiss_review mutates).
        """
        pr = self.state.prs.get(pull_number)
        if pr is None:
            raise RuntimeError(f"_create_review: PR #{pull_number} not in state")
        review_id = self._next_review_id
        self._next_review_id += 1
        # State maps from GitHub's review event names: APPROVE → APPROVED.
        if event == "APPROVE":
            state = "APPROVED"
        elif event == "REQUEST_CHANGES":
            state = "CHANGES_REQUESTED"
        else:
            state = "COMMENTED"
        pr.reviews.append({"state": state, "user_login": _APPROVER_LOGIN})
        self._review_state.setdefault(pull_number, {})[review_id] = state
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                id=review_id,
                state=state,
                user=SimpleNamespace(login=_APPROVER_LOGIN),
            )
        )

    # -- second-identity dismissal -------------------------------------------

    def _dismiss_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        review_id: int,
        *,
        message: str = "",
        **_: Any,
    ) -> SimpleNamespace:
        """Simulate the second-identity approver dismissing their review.

        Flips the per-id state to DISMISSED. Also updates the FakePR.reviews
        entry's state so subsequent list_reviews calls reflect the dismissal
        (mirrors the real GitHub behavior where the dismissed review's state
        becomes ``DISMISSED`` in the list_reviews response).
        """
        pr = self.state.prs.get(pull_number)
        if pr is None:
            raise RuntimeError(f"_dismiss_review: PR #{pull_number} not in state")
        prior = self._review_state.get(pull_number, {}).get(review_id)
        if prior is None:
            raise RuntimeError(
                f"_dismiss_review: no review #{review_id} on PR #{pull_number}"
            )
        self._review_state[pull_number][review_id] = "DISMISSED"
        # Mirror to FakePR.reviews — flip the FIRST APPROVED review by the
        # approver login. The fake's append-order matches the create-order,
        # so the review at index matching its position in _review_state is
        # the one we just dismissed.
        for r in pr.reviews:
            if (
                r.get("user_login") == _APPROVER_LOGIN
                and r.get("state") == "APPROVED"
            ):
                r["state"] = "DISMISSED"
                break
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                id=review_id,
                state="DISMISSED",
                user=SimpleNamespace(login=_APPROVER_LOGIN),
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
                pr.labels.add("mq:queued")
                pr.labels.add("mq:active")
        return resp

    # -- post-dismissal eject injection --------------------------------------

    def inject_eject_after_dismissal(self, pr_number: int) -> None:
        """Inject the eject comment ONLY after the approver dismissed the review.

        Mirrors the live processor's behavior: the eject is the consequence
        of observing that the at-enqueue approval gate no longer holds. The
        check is whether ANY review on the PR for this approver login is
        currently DISMISSED — if so, the activation invariant has been
        violated and the processor's next-cycle ejects.
        """
        if self._eject_comment_body is None:
            return
        if self._comment_posted:
            return
        states = self._review_state.get(pr_number, {})
        if not any(s == "DISMISSED" for s in states.values()):
            return
        self._base_create_comment("owner", "repo", pr_number, body=self._eject_comment_body)
        self._comment_posted = True


class _GitNS:
    def __init__(self, refs: dict[str, str]) -> None:
        self._refs = refs

    def get_ref(self, owner: str, repo: str, ref: str) -> SimpleNamespace:
        sha = self._refs.get(ref, "develop_initial_tip")
        return SimpleNamespace(
            parsed_data=SimpleNamespace(ref=f"refs/{ref}", object=SimpleNamespace(sha=sha))
        )

    def create_ref(self, owner: str, repo: str, *, ref: str, sha: str, **_: Any) -> SimpleNamespace:
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
    """Approval → /merge → activation → dismissal → eject with documented reason → passed=True."""
    _patch_timing(monkeypatch)
    eject_body = (
        "<!-- rocm-mq-status -->\n"
        "## Ejected: approval revoked\n"
        "Processor cycle: https://github.test/x/y/actions/runs/999\n"
    )
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)
    # Same client doubles as the "approver" client in tests — the driver
    # accepts an explicit approver_client kwarg so the test can pin a single
    # in-memory fake without needing a second instance backed by separate
    # state. A live run uses two real GitHubClient instances backed by
    # distinct PATs.
    result = dog_07.run_scenario(
        client,
        owner="owner",
        repo="repo",
        approver_client=client,
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_after_dismissal,
    )

    assert result.passed is True
    assert result.scenario_id == "dog_07"
    assert result.expected_outcome == dog_07.EXPECTED
    assert result.observed_outcome["action"] == "Eject"
    assert result.observed_outcome["reason"] == _EJECT_REASON
    assert result.pr_number == 7000
    # JSON emitted to the per-run output dir.
    written = list(tmp_path.glob("*-dog_07.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["scenario_id"] == "dog_07"


def test_run_scenario_emits_d04_schema_fields(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Per-run JSON includes every D-04 schema field."""
    _patch_timing(monkeypatch)
    eject_body = f"<!-- rocm-mq-status -->\nEjected: {_EJECT_REASON}\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    dog_07.run_scenario(
        client,
        owner="owner",
        repo="repo",
        approver_client=client,
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_after_dismissal,
    )

    written = list(tmp_path.glob("*-dog_07.json"))
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


def test_run_scenario_records_approval_via_approver_client(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """The driver must post an APPROVE review BEFORE posting /merge.

    The recorded review's user_login is the second-identity approver login
    (samuel-reeder-bot in this test). The PR must carry one APPROVED review
    (later flipped to DISMISSED) at the end of the scenario.
    """
    _patch_timing(monkeypatch)
    eject_body = f"<!-- rocm-mq-status -->\nEjected: {_EJECT_REASON}\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    result = dog_07.run_scenario(
        client,
        owner="owner",
        repo="repo",
        approver_client=client,
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_after_dismissal,
    )

    pr = fake_state.prs[result.pr_number]
    # Exactly one review recorded — the approver's, now DISMISSED.
    assert len(pr.reviews) == 1
    assert pr.reviews[0]["user_login"] == _APPROVER_LOGIN
    assert pr.reviews[0]["state"] == "DISMISSED"


def test_run_scenario_dismisses_review_after_activation(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """The driver must call dismiss_review AFTER mq:active observed.

    Guards against a regression where the driver dismisses the review BEFORE
    confirming activation — which would mean the at-enqueue gate fails
    BEFORE activation and the scenario tests a different code path (rejection
    at /merge time rather than post-activation eject).
    """
    _patch_timing(monkeypatch)
    eject_body = f"<!-- rocm-mq-status -->\nEjected: {_EJECT_REASON}\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    result = dog_07.run_scenario(
        client,
        owner="owner",
        repo="repo",
        approver_client=client,
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_after_dismissal,
    )

    # The PR carried mq:active at some point (the activation observation
    # gates the dismissal step in the driver). After the scenario, the labels
    # may or may not still carry mq:active depending on the eject simulation;
    # but the per-id review state must be DISMISSED, which only happens if
    # the dismiss call ran.
    pr_review_states = client._review_state[result.pr_number]
    assert any(s == "DISMISSED" for s in pr_review_states.values())


def test_run_scenario_failure_mode_when_reason_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Status comment posted but reason text differs → timeout (no false positive).

    The predicate must only succeed on the documented verbatim eject reason;
    a different eject reason (e.g., a merge-conflict eject) must NOT be
    accepted as DOG-07 evidence.
    """
    _patch_timing(monkeypatch)
    eject_body = "<!-- rocm-mq-status -->\n## Ejected: merge conflict with develop\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    with pytest.raises(TimeoutError):
        dog_07.run_scenario(
            client,
            owner="owner",
            repo="repo",
            approver_client=client,
            output_dir=tmp_path,
            poll_interval_s=0,
            poll_timeout_s=0.05,
            inject_eject_after=client.inject_eject_after_dismissal,
        )


def test_run_scenario_failure_mode_when_no_activation_observed(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """If activation never happens (no mq:active label), driver times out at
    the activation poll — the dismissal step never fires.

    Guards against a regression where the driver dismisses the review BEFORE
    activation is confirmed (which would not exercise the RFC §5 defense-in-
    depth path).
    """
    _patch_timing(monkeypatch)
    client = _DogfoodFake(
        fake_state,
        eject_comment_body=f"<!-- rocm-mq-status -->\nEjected: {_EJECT_REASON}\n",
        auto_activate_on_merge_cmd=False,  # processor never activates
    )

    with pytest.raises(TimeoutError):
        dog_07.run_scenario(
            client,
            owner="owner",
            repo="repo",
            approver_client=client,
            output_dir=tmp_path,
            poll_interval_s=0,
            poll_timeout_s=0.05,
            inject_eject_after=client.inject_eject_after_dismissal,
        )

    # The dismissal must NOT have fired — every recorded review remains
    # APPROVED.
    for pr_states in client._review_state.values():
        assert all(s == "APPROVED" for s in pr_states.values())


def test_run_scenario_timeline_records_approval_and_dismissal_events(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Timeline must include the approval + dismissal events per plan.

    Plan 03-15 Task 2 <action>: 'Timeline subset: pr_opened,
    approval_review_submitted, merge_command_posted, mq_queued_label_applied,
    activation_began, mq_active_label_applied, approval_review_dismissed,
    ejected.'
    """
    _patch_timing(monkeypatch)
    eject_body = f"<!-- rocm-mq-status -->\nEjected: {_EJECT_REASON}\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    result = dog_07.run_scenario(
        client,
        owner="owner",
        repo="repo",
        approver_client=client,
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_after_dismissal,
    )

    event_types = [evt for (_ts, evt, _state) in result.timeline]
    assert "pr_opened" in event_types
    assert "approval_review_submitted" in event_types
    assert "merge_command_posted" in event_types
    assert "mq_active_label_applied" in event_types
    assert "approval_review_dismissed" in event_types
    assert "ejected" in event_types
    # Ordering invariant: approval BEFORE /merge BEFORE activation BEFORE
    # dismissal BEFORE eject (RFC §5 narrative).
    order = {evt: i for (i, (_ts, evt, _state)) in enumerate(result.timeline)}
    assert order["approval_review_submitted"] < order["merge_command_posted"]
    assert order["merge_command_posted"] < order["mq_active_label_applied"]
    assert order["mq_active_label_applied"] < order["approval_review_dismissed"]
    assert order["approval_review_dismissed"] < order["ejected"]


# ---------------------------------------------------------------------------
# main() — APPROVER_TOKEN gate (option-a outcome)
# ---------------------------------------------------------------------------


def test_main_exits_2_when_github_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No GITHUB_TOKEN → exit 2 (usage error), no live API attempt."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("APPROVER_TOKEN", raising=False)
    rc = dog_07.main(["--owner", "owner", "--repo", "repo"])
    assert rc == 2


def test_main_exits_2_when_approver_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GITHUB_TOKEN set but APPROVER_TOKEN missing → exit 2.

    Per plan 03-15 Task 1 option-a (recommended): the driver requires a
    second-identity PAT; absence is a usage error explaining how to provision
    one. The exit code 2 (vs 1) signals 'misconfiguration' rather than
    'scenario failed'.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy_primary")
    monkeypatch.delenv("APPROVER_TOKEN", raising=False)
    rc = dog_07.main(["--owner", "owner", "--repo", "repo"])
    assert rc == 2
