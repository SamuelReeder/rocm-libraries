"""Tests for scripted RFC §4.3.1 audit evidence scenario drivers."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

SCENARIOS: tuple[tuple[str, str, str, str], ...] = (
    (
        "rocm_mq.audit_evidence.labels_at_open",
        "audit_labels_at_open",
        "label tamper",
        "labels-at-open",
    ),
    (
        "rocm_mq.audit_evidence.label_added",
        "audit_label_added",
        "label tamper",
        "post-open-add",
    ),
    (
        "rocm_mq.audit_evidence.label_removed",
        "audit_label_removed",
        "label tamper",
        "post-open-remove",
    ),
    (
        "rocm_mq.audit_evidence.base_ref_changed",
        "audit_base_ref_changed",
        "lifecycle invalidation",
        "base-ref-change",
    ),
    (
        "rocm_mq.audit_evidence.converted_to_draft",
        "audit_converted_to_draft",
        "lifecycle invalidation",
        "draft-conversion",
    ),
    (
        "rocm_mq.audit_evidence.reopened_stale_labels",
        "audit_reopened_stale_labels",
        "lifecycle invalidation",
        "stale-label-reopen",
    ),
)


def _seed_pr(state: FakeRepoState) -> None:
    state.prs[42] = FakePR(
        number=42,
        head_sha="live-sha",
        labels={"mq:queued", "mq:hipdnn"},
    )


def _prepare(_client: Any, _owner: str, _repo: str) -> tuple[int, str, str]:
    return 42, "https://github.test/owner/repo/pull/42", "live-sha"


def _tamper(_client: Any, _owner: str, _repo: str, _pr_number: int) -> dict[str, Any]:
    return {"operation": "tamper-applied"}


def _inject_success(
    state: FakeRepoState, *, family: str, detail: str, actor: str = "alice"
):
    def inject(pr_number: int) -> None:
        pr = state.prs[pr_number]
        pr.labels.clear()
        state.status_store[(pr.head_sha, "merge-queue/active")] = {
            "state": "error",
            "context": "merge-queue/active",
            "created_at": "2026-05-24T12:00:00Z",
            "creator_type": "app",
        }
        state.comments_store.setdefault(pr_number, {})[1001] = (
            f"## Merge queue audit: {family}\n"
            f"@{actor} triggered audit eject for detail={detail}.\n"
            "Re-run `/merge` after fixing the issue."
        )

    return inject


@pytest.mark.parametrize(("module_name", "scenario_id", "family", "detail"), SCENARIOS)
def test_driver_modules_expose_stable_contract(
    module_name: str, scenario_id: str, family: str, detail: str
) -> None:
    module = importlib.import_module(module_name)

    assert scenario_id == module.SCENARIO_ID
    assert module.EXPECTED["family"] == family
    assert module.EXPECTED["detail"] == detail
    assert module.EXPECTED["activation_state"] == "error"
    assert module.EXPECTED["remaining_mq_labels"] == 0
    assert isinstance(module.TIMEOUT_S, int)
    assert module.TIMEOUT_S > 0
    assert callable(module.run_scenario)
    assert callable(module.main)


@pytest.mark.parametrize(("module_name", "scenario_id", "family", "detail"), SCENARIOS)
def test_run_scenario_emits_passing_json_when_audit_cleanup_is_observed(
    tmp_path: Path,
    module_name: str,
    scenario_id: str,
    family: str,
    detail: str,
) -> None:
    module = importlib.import_module(module_name)
    state = FakeRepoState()
    _seed_pr(state)
    client = FakeGitHub(state)

    result = module.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        poll_timeout_s=1,
        actor="alice",
        prepare_pr=_prepare,
        tamper=_tamper,
        inject_observed_after=_inject_success(state, family=family, detail=detail),
    )

    assert result.scenario_id == scenario_id
    assert result.expected_outcome["family"] == family
    assert result.expected_outcome["detail"] == detail
    assert result.observed_outcome["remaining_mq_labels"] == []
    assert result.observed_outcome["activation_status"] == {
        "context": "merge-queue/active",
        "state": "error",
    }
    assert result.observed_outcome["audit_comment_count"] == 1
    assert result.passed is True
    json_files = tuple(tmp_path.glob(f"*-{scenario_id}.json"))
    assert len(json_files) == 1
    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert payload["scenario_id"] == scenario_id
    assert payload["passed"] is True


@pytest.mark.parametrize(("module_name", "scenario_id", "family", "detail"), SCENARIOS)
def test_run_scenario_fails_when_required_audit_observation_never_appears(
    tmp_path: Path,
    module_name: str,
    scenario_id: str,
    family: str,
    detail: str,
) -> None:
    del scenario_id, family, detail
    module = importlib.import_module(module_name)
    state = FakeRepoState()
    _seed_pr(state)
    client = FakeGitHub(state)

    result = module.run_scenario(
        client,
        owner="owner",
        repo="repo",
        output_dir=tmp_path,
        poll_interval_s=0,
        poll_timeout_s=0,
        actor="alice",
        prepare_pr=_prepare,
        tamper=_tamper,
    )

    assert result.passed is False
    assert result.observed_outcome["remaining_mq_labels"] == ["mq:hipdnn", "mq:queued"]
    assert result.observed_outcome["activation_status"] is None
    assert result.observed_outcome["audit_comment_count"] == 0
    assert "timeout" in result.notes.lower()


@pytest.mark.parametrize(("module_name", "scenario_id", "family", "detail"), SCENARIOS)
def test_cli_returns_pass_fail_and_usage_codes(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    scenario_id: str,
    family: str,
    detail: str,
) -> None:
    del scenario_id, family, detail
    module = importlib.import_module(module_name)

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert module.main(["--owner=owner", "--repo=repo"]) == 2

    class _Result:
        def __init__(self, passed: bool) -> None:
            self.passed = passed

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_dummy")
    monkeypatch.setattr(module, "_build_client", lambda token: object())
    monkeypatch.setattr(
        module,
        "run_scenario",
        lambda client, *, owner, repo: _Result(True),
    )
    assert module.main(["--owner=owner", "--repo=repo"]) == 0

    monkeypatch.setattr(
        module,
        "run_scenario",
        lambda client, *, owner, repo: _Result(False),
    )
    assert module.main(["--owner=owner", "--repo=repo"]) == 1
