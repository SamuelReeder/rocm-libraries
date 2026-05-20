"""Tests for rocm_mq.dogfood.dog_04 — simultaneous-/merge idempotency driver.

Two-mode driver per CONTEXT.md D-04. Unit-test mode exercises orchestration
+ the three idempotency assertions against FakeGitHub; live-fork mode is
operator-initiated and NOT exercised here.

Scenario contract (RFC §6, RESEARCH.md Area #7):
    User issues two ``/merge`` comments back-to-back. Handler must short-circuit
    the second so:
      (a) ``mq:queued`` label present exactly once,
      (b) exactly ONE comment carries the ``<!-- rocm-mq-status -->`` marker,
      (c) BOTH trigger comments carry an ``eyes`` reaction.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rocm_mq.dogfood import dog_04
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_scenario_id_is_dog_04() -> None:
    assert dog_04.SCENARIO_ID == "dog_04"


def test_expected_outcome_pinned() -> None:
    assert dog_04.EXPECTED == {"action": "Idempotent", "no_duplicate_state": True}


def test_timeout_budget_60s() -> None:
    assert dog_04.TIMEOUT_S == 60


def test_main_is_callable() -> None:
    assert callable(dog_04.main)


# ---------------------------------------------------------------------------
# _DogfoodFake — simulates the handler processing two /merge comments
# ---------------------------------------------------------------------------


class _DogfoodFake(FakeGitHub):
    """FakeGitHub with the surface dog_04 needs.

    Simulates the handler by:
      * Applying ``mq:queued`` exactly once when ``/merge`` is posted on a PR.
      * Upserting exactly ONE ``<!-- rocm-mq-status -->`` comment on the PR.
      * Recording an ``eyes`` reaction for each ``/merge`` trigger comment.
    """

    _STATUS_MARKER = "<!-- rocm-mq-status -->"

    def __init__(self, state: FakeRepoState) -> None:
        super().__init__(state)
        self._next_pr_number = 2000
        self._refs: dict[str, str] = {"heads/develop": "develop_initial_tip"}
        self._created_files: list[dict[str, Any]] = []
        self._created_pulls: list[dict[str, Any]] = []
        # Simulated-handler bookkeeping.
        self._status_comment_id: int | None = None
        self.rest.git = _GitNS(self._refs)
        self.rest.repos.create_or_update_file_contents = (  # type: ignore[attr-defined]
            self._create_or_update_file
        )
        self.rest.pulls.create = self._create_pull  # type: ignore[attr-defined]
        # Wrap create_comment to trigger the simulated-handler side effects
        # WHEN the body is a recognized command. The base _IssuesNS.create_comment
        # remains the source of truth for the comments_store.
        self._base_create_comment = self.rest.issues.create_comment
        self.rest.issues.create_comment = self._wrapped_create_comment  # type: ignore[assignment]

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
        self._created_files.append({"path": path, "branch": branch})
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                commit=SimpleNamespace(sha=f"commit_{len(self._created_files)}")
            )
        )

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
        self.state.prs[number] = FakePR(number=number, head_sha=f"head_{number}")
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                number=number,
                html_url=f"https://github.test/{owner}/{repo}/pull/{number}",
            )
        )

    def _wrapped_create_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        body: str = "",
        **kw: Any,
    ) -> SimpleNamespace:
        """Store the comment, then simulate handler side-effects for /merge."""
        resp = self._base_create_comment(owner, repo, issue_number, body=body, **kw)
        if body.strip() == "/merge":
            # Idempotent label apply — set-semantics on FakePR.labels handles
            # the dedup automatically.
            self.rest.issues.add_labels(
                owner, repo, issue_number, labels=["mq:queued"]
            )
            # Status comment upsert: create on first /merge, no-op on subsequent.
            if self._status_comment_id is None:
                status_resp = self._base_create_comment(
                    owner,
                    repo,
                    issue_number,
                    body=f"{self._STATUS_MARKER}\n## Queued\n",
                )
                self._status_comment_id = int(status_resp.parsed_data.id)
            # Eyes reaction on the trigger comment.
            trigger_id = int(resp.parsed_data.id)
            self.rest.reactions.create_for_issue_comment(
                owner, repo, trigger_id, content="eyes"
            )
        return resp


class _GitNS:
    def __init__(self, refs: dict[str, str]) -> None:
        self._refs = refs

    def get_ref(self, owner: str, repo: str, ref: str) -> SimpleNamespace:
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                ref=f"refs/{ref}",
                object=SimpleNamespace(
                    sha=self._refs.get(ref, "develop_initial_tip")
                ),
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
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_state() -> FakeRepoState:
    return FakeRepoState()


def _patch_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rocm_mq.dogfood._base.time.sleep", lambda _s: None)


def test_run_scenario_happy_path_three_idempotency_assertions(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Two /merge comments → label once, status comment once, eyes on both."""
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)

    result = dog_04.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
    )

    assert result.passed is True
    assert result.scenario_id == "dog_04"
    assert result.observed_outcome["action"] == "Idempotent"
    assert result.observed_outcome["no_duplicate_state"] is True
    assert result.observed_outcome["mq_queued_count"] == 1
    assert result.observed_outcome["status_comment_count"] == 1
    assert result.observed_outcome["eyes_reactions_on_triggers"] == 2

    written = list(tmp_path.glob("*-dog_04.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["passed"] is True


def test_run_scenario_failure_when_label_duplicated(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """If the fake "handler" double-applies mq:queued, the driver fails.

    Verified by patching the FakeGitHub instance to bypass the set-semantics
    label dedup: we add a sibling label ``mq:queued-extra`` BEFORE the run so
    the count includes the broken duplicate-like state. Demonstrates the
    driver's assertion is real, not a no-op.
    """
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)

    # Monkey-patch list_labels-equivalent (pulls.get returns labels) to claim
    # mq:queued is present twice via two label entries with the same name.
    # We do this by overriding pulls.get only after run_scenario invokes it
    # for the assertion phase.
    real_pulls_get = client.rest.pulls.get
    call_count = {"n": 0}

    def lying_get(owner: str, repo: str, pull_number: int) -> SimpleNamespace:
        call_count["n"] += 1
        resp = real_pulls_get(owner, repo, pull_number)
        # Inflate label list with a second mq:queued entry to simulate dup.
        original_labels = list(resp.parsed_data.labels)
        original_labels.append(SimpleNamespace(name="mq:queued"))
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                number=resp.parsed_data.number,
                head=resp.parsed_data.head,
                labels=original_labels,
                user=resp.parsed_data.user,
                maintainer_can_modify=resp.parsed_data.maintainer_can_modify,
            )
        )

    client.rest.pulls.get = lying_get  # type: ignore[assignment]

    result = dog_04.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
    )

    assert result.passed is False
    assert result.observed_outcome["mq_queued_count"] == 2
    assert result.observed_outcome["no_duplicate_state"] is False
