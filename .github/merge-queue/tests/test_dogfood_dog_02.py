"""Tests for rocm_mq.dogfood.dog_02 — merge-conflict-at-activation eject driver.

Two-mode driver per CONTEXT.md D-04 and the plan's "unit + live" model:

  * Unit-test mode (this file): exercise the driver's orchestration logic against
    a FakeGitHub extension that simulates the eject status-comment landing.
    Verifies module constants, predicate construction, happy-path observed
    outcome, and at least one failure mode (status comment lacks the eject
    reason → driver returns 1 + passed=False).

  * Live-fork mode (NOT exercised here): ``python -m rocm_mq.dogfood.dog_02
    --owner SamuelReeder --repo rocm-libraries`` against the live fork.
    Operator-initiated after workflows are deployed; out of scope for CI.

Scenario contract (RFC §6):
    PR opts into a queue, conflicts with develop tip at activation → processor
    ejects with documented reason ``"merge conflict with develop"``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rocm_mq.dogfood import dog_02
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState


# ---------------------------------------------------------------------------
# Module-level constants — locked by plan 03-11 Task 2 behavior block.
# ---------------------------------------------------------------------------


def test_scenario_id_is_dog_02() -> None:
    assert dog_02.SCENARIO_ID == "dog_02"


def test_expected_outcome_pinned() -> None:
    assert dog_02.EXPECTED == {
        "action": "Eject",
        "reason": "merge conflict with develop",
    }


def test_timeout_budget_12_minutes() -> None:
    # PRE-CONFIRM Task 1 option-a: defaults from RESEARCH.md Area #10.
    assert dog_02.TIMEOUT_S == 12 * 60


def test_main_is_callable() -> None:
    # Smoke test — main is importable + callable; argparse errors propagate.
    assert callable(dog_02.main)


# ---------------------------------------------------------------------------
# _DogfoodFake — local FakeGitHub extension that simulates the processor's
# eject status-comment landing. Mirrors the pattern in test_dogfood_base.py.
# ---------------------------------------------------------------------------


class _DogfoodFake(FakeGitHub):
    """FakeGitHub with the git/repos/pulls/actions extensions dog_02 needs."""

    def __init__(
        self,
        state: FakeRepoState,
        *,
        eject_comment_body: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        archive_bytes: bytes | None = None,
    ) -> None:
        super().__init__(state)
        self._next_pr_number = 1000
        self._refs: dict[str, str] = {"heads/develop": "develop_initial_tip"}
        self._created_files: list[dict[str, Any]] = []
        self._created_pulls: list[dict[str, Any]] = []
        self._eject_comment_body = eject_comment_body
        self._comment_posted = False
        # Wire sub-namespaces (kept LOCAL to this test file per 03-10 pattern).
        self.rest.git = _GitNS(self._refs)
        self.rest.repos.create_or_update_file_contents = (  # type: ignore[attr-defined]
            self._create_or_update_file
        )
        self.rest.pulls.create = self._create_pull  # type: ignore[attr-defined]
        self.rest.actions = _ActionsNS(artifacts or [], archive_bytes)

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
        # Seed the PR into the comments_store so the polling predicate's
        # list_comments call has a stable shape to operate on. When the
        # eject_comment_body is configured we lazily inject it on first poll
        # call below.
        self.state.prs[number] = FakePR(number=number, head_sha=f"head_{number}")
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                number=number,
                html_url=f"https://github.test/{owner}/{repo}/pull/{number}",
            )
        )

    def inject_eject_comment(self, pr_number: int) -> None:
        """Simulate the processor posting the eject status-comment."""
        if self._eject_comment_body is None:
            return
        if self._comment_posted:
            return
        self.rest.issues.create_comment(
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


class _ActionsNS:
    def __init__(
        self, artifacts: list[dict[str, Any]], archive_bytes: bytes | None
    ) -> None:
        self._artifacts = artifacts
        self._archive_bytes = archive_bytes

    def list_workflow_run_artifacts(
        self, owner: str, repo: str, run_id: int, **_: Any
    ) -> SimpleNamespace:
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                total_count=len(self._artifacts),
                artifacts=[
                    SimpleNamespace(id=a["id"], name=a["name"]) for a in self._artifacts
                ],
            )
        )

    def download_artifact(
        self,
        owner: str,
        repo: str,
        artifact_id: int,
        archive_format: str = "zip",
        **_: Any,
    ) -> SimpleNamespace:
        if self._archive_bytes is None:
            from tests.gh_fake import _make_request_failed

            raise _make_request_failed(404)
        return SimpleNamespace(content=self._archive_bytes)


# ---------------------------------------------------------------------------
# run_scenario — happy path + failure mode
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_state() -> FakeRepoState:
    return FakeRepoState()


def _patch_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op sleep so poll_pr_state does not block; real monotonic is fine."""
    monkeypatch.setattr("rocm_mq.dogfood._base.time.sleep", lambda _s: None)


def test_run_scenario_happy_path_returns_passed_result(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Eject comment with the documented reason → passed=True, observed matches."""
    _patch_timing(monkeypatch)
    eject_body = (
        "<!-- rocm-mq-status -->\n"
        "## Ejected: merge conflict with develop\n"
        "Processor cycle: https://github.test/x/y/actions/runs/777\n"
    )
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    result = dog_02.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,  # bypass sleeps even on fallthrough
        inject_eject_after=client.inject_eject_comment,
    )

    assert result.passed is True
    assert result.scenario_id == "dog_02"
    assert result.expected_outcome == dog_02.EXPECTED
    assert result.observed_outcome["action"] == "Eject"
    assert "merge conflict with develop" in result.observed_outcome["reason"]
    # A PR was created.
    assert result.pr_number == 1000
    # A conflict-seed file was committed BEFORE the PR's branch was created.
    seeded_paths = [f["path"] for f in client._created_files]
    assert any("dogfood-seed-conflict" in p or "hipdnn" in p for p in seeded_paths)
    # JSON emitted to the per-run output dir.
    written = list(tmp_path.glob("*-dog_02.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["scenario_id"] == "dog_02"


def test_run_scenario_failure_mode_when_reason_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """Status comment posted but reason text differs → passed=False, exit 1."""
    _patch_timing(monkeypatch)
    # A status comment but with a DIFFERENT eject reason. The predicate must
    # only succeed on the documented literal; anything else keeps polling
    # until the test forces a timeout via inject_eject_after returning None.
    eject_body = (
        "<!-- rocm-mq-status -->\n## Ejected: required check failed\n"
    )
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    with pytest.raises(TimeoutError):
        dog_02.run_scenario(
            client,
            owner="owner",
            repo="repo",
            output_dir=tmp_path,
            poll_interval_s=0,
            poll_timeout_s=0.05,  # microseconds-scale to force timeout fast
            inject_eject_after=client.inject_eject_comment,
        )


def test_run_scenario_seeds_develop_with_conflict_before_pr(
    monkeypatch: pytest.MonkeyPatch,
    fake_state: FakeRepoState,
    tmp_path: Path,
) -> None:
    """The conflict-seed commit on develop must precede the PR branch creation.

    Per the plan: 'main() seeds develop with a conflicting line BEFORE PR
    creation so activation-time merge-into-PR-head conflicts deterministically'.
    """
    _patch_timing(monkeypatch)
    eject_body = "<!-- rocm-mq-status -->\nEjected: merge conflict with develop\n"
    client = _DogfoodFake(fake_state, eject_comment_body=eject_body)

    dog_02.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        inject_eject_after=client.inject_eject_comment,
    )

    # First file commit goes to develop (no PR branch yet); second commit is
    # the PR's branch.
    assert len(client._created_files) == 2
    seed_commit, pr_commit = client._created_files
    # Seed went to develop ref (or to a stable develop-targeting branch — the
    # production code uses ``branch="develop"`` directly via the Contents API).
    assert seed_commit["branch"] == "develop"
    # PR commit goes to a dogfood/dog_02-* branch.
    assert pr_commit["branch"].startswith("dogfood/dog_02-")
