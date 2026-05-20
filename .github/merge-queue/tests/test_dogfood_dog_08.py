"""Tests for rocm_mq.dogfood.dog_08 — handler-level rejection (no-opted-in-path).

Scenario contract (RFC §6, plan 03-06): a PR whose changed files do NOT touch
any opted-in queue path (and are not under the dogfood/ canary tree, which
routes to ``dogfood-canary``) gets rejected by the handler BEFORE any state
mutation. The PR must end up with:
  (a) NO ``mq:*`` labels,
  (b) ≥1 bot comment whose body contains the rejection-reason substring
      (``no opted-in`` per cmd_handle.py's literal text),
  (c) NO comment carrying the ``<!-- rocm-mq-status -->`` marker (the
      handler short-circuits BEFORE the status-comment upsert step).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rocm_mq.dogfood import dog_08
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_scenario_id_is_dog_08() -> None:
    assert dog_08.SCENARIO_ID == "dog_08"


def test_expected_outcome_pinned() -> None:
    assert dog_08.EXPECTED == {
        "action": "Reject",
        "level": "handler",
        "reason_substring": "no opted-in path",
    }


def test_timeout_budget_60s() -> None:
    assert dog_08.TIMEOUT_S == 60


def test_main_is_callable() -> None:
    assert callable(dog_08.main)


# ---------------------------------------------------------------------------
# Path verification — the test path the driver chose really IS outside any
# opted-in queue per path_to_queues.yml. This is a guard against the YAML
# evolving to route README under a queue without the driver's path moving.
# ---------------------------------------------------------------------------


def test_pr_file_path_is_not_in_path_to_queues() -> None:
    """The driver's PR target path must not match any opted-in queue path."""
    yml = Path(
        "../merge-queue/path_to_queues.yml"
        if Path("../merge-queue/path_to_queues.yml").exists()
        else ".github/merge-queue/path_to_queues.yml"
    )
    if not yml.exists():
        # Repo-root invocation — try the absolute layout.
        yml = Path("/home/AMD/sareeder/worktrees/rocmlibs-merge-queue-rfc/.github/merge-queue/path_to_queues.yml")
    if not yml.exists():
        pytest.skip("path_to_queues.yml not present at any expected path")
    import yaml

    parsed = yaml.safe_load(yml.read_text(encoding="utf-8"))
    paths = [str(entry["path"]) for entry in parsed.get("paths") or []]
    target = dog_08.PR_FILE_PATH
    for p in paths:
        assert not target.startswith(p), (
            f"dog_08 target path {target!r} unexpectedly under opted-in "
            f"path {p!r} — driver would trip empty-queue-set vs "
            "self-bootstrap conflicts instead of the intended rejection."
        )


# ---------------------------------------------------------------------------
# _DogfoodFake — simulates the handler rejecting on no-opted-in-path
# ---------------------------------------------------------------------------


class _DogfoodFake(FakeGitHub):
    """FakeGitHub with the simulated DOG-08 handler rejection path."""

    _REJECTION_BODY = (
        "`/merge` rejected: this PR touches no opted-in queue paths "
        "(per `.github/merge-queue/path_to_queues.yml`). Only PRs whose "
        "changes land under an opted-in path tree can be enqueued. "
        "Maintainers may merge non-opted-in changes via the GitHub UI."
    )

    def __init__(
        self,
        state: FakeRepoState,
        *,
        emit_rejection: bool = True,
        bypass_short_circuit: bool = False,
    ) -> None:
        super().__init__(state)
        self._next_pr_number = 3000
        self._refs: dict[str, str] = {"heads/develop": "develop_initial_tip"}
        self._created_files: list[dict[str, Any]] = []
        self._created_pulls: list[dict[str, Any]] = []
        self._emit_rejection = emit_rejection
        self._bypass_short_circuit = bypass_short_circuit
        self.rest.git = _GitNS(self._refs)
        self.rest.repos.create_or_update_file_contents = (  # type: ignore[attr-defined]
            self._create_or_update_file
        )
        self.rest.pulls.create = self._create_pull  # type: ignore[attr-defined]
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
        pr = FakePR(number=number, head_sha=f"head_{number}")
        # Mirror the PR's file list onto the FakePR so list_files works.
        pr.files = [str(f["path"]) for f in self._created_files]
        self.state.prs[number] = pr
        self._created_pulls.append({"number": number, "title": title})
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
        """When /merge is posted, simulate the handler-level rejection."""
        resp = self._base_create_comment(owner, repo, issue_number, body=body, **kw)
        if body.strip() == "/merge" and not self._bypass_short_circuit:
            if self._emit_rejection:
                self._base_create_comment(
                    owner, repo, issue_number, body=self._REJECTION_BODY
                )
            # NO label apply, NO status-comment upsert, NO eyes-reaction —
            # rejection path in cmd_handle.py is a single comment + return.
        elif body.strip() == "/merge" and self._bypass_short_circuit:
            # Simulate the BUG path: handler applied mq:queued anyway, even
            # though the queue set is empty. The driver must catch this.
            self.rest.issues.add_labels(
                owner, repo, issue_number, labels=["mq:queued"]
            )
            self._base_create_comment(
                owner, repo, issue_number, body=self._REJECTION_BODY
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
# Tests — happy path + failure mode
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_state() -> FakeRepoState:
    return FakeRepoState()


def _patch_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rocm_mq.dogfood._base.time.sleep", lambda _s: None)


def test_run_scenario_happy_path_rejection_detected(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Handler rejects with explanatory comment → driver passes."""
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state)

    result = dog_08.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
    )

    assert result.passed is True
    assert result.scenario_id == "dog_08"
    assert result.observed_outcome["action"] == "Reject"
    assert result.observed_outcome["level"] == "handler"
    assert "no opted-in" in result.observed_outcome["reason_substring"]
    assert result.observed_outcome["mq_label_count"] == 0
    assert result.observed_outcome["status_comment_count"] == 0
    assert result.observed_outcome["rejection_comment_found"] is True

    written = list(tmp_path.glob("*-dog_08.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["passed"] is True


def test_run_scenario_failure_when_handler_applies_label_anyway(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """If the handler buggily applies mq:queued, driver detects + fails."""
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state, bypass_short_circuit=True)

    result = dog_08.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
    )

    assert result.passed is False
    assert result.observed_outcome["mq_label_count"] == 1
    # The reason text is still present, but the label-count failure flips passed=False.
    assert result.observed_outcome["rejection_comment_found"] is True


def test_run_scenario_failure_when_rejection_comment_missing(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """If the handler does NOT post a rejection comment → driver fails."""
    _patch_timing(monkeypatch)
    client = _DogfoodFake(fake_state, emit_rejection=False)

    # No rejection ever lands → predicate should time out.
    with pytest.raises(TimeoutError):
        dog_08.run_scenario(
            client,
            owner="owner",
            repo="repo",
            output_dir=tmp_path,
            poll_interval_s=0,
            poll_timeout_s=0.05,
        )


# ---------------------------------------------------------------------------
# .gitkeep placeholder — verified via filesystem assertion in this test suite.
# ---------------------------------------------------------------------------


def test_dogfood_runs_directory_has_gitkeep() -> None:
    """The dogfood-runs/ directory placeholder must exist for plan 03-11."""
    p = Path(
        "/home/AMD/sareeder/worktrees/rocmlibs-merge-queue-rfc/.planning/phases/03-handler-processor-on-fork/dogfood-runs/.gitkeep"
    )
    assert p.exists(), f"missing .gitkeep at {p}"
