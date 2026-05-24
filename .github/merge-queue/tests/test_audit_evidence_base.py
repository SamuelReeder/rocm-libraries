"""Tests for rocm_mq.audit_evidence._base shared audit evidence helpers."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rocm_mq.audit_evidence._base import (
    AuditResult,
    activation_status_observation,
    audit_comments_matching,
    emit_result,
    poll_pr_state,
    remaining_mq_labels,
    workflow_run_urls,
)
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState


_AUDIT_KEYS: frozenset[str] = frozenset(
    {
        "scenario_id",
        "run_started_at",
        "run_ended_at",
        "pr_number",
        "pr_url",
        "expected_outcome",
        "observed_outcome",
        "timeline",
        "audit_run_urls",
        "passed",
        "notes",
        "source_path",
    }
)


def _make_result(**overrides: Any) -> AuditResult:
    base: dict[str, Any] = {
        "scenario_id": "audit_label_added",
        "run_started_at": "2026-05-24T12:00:00+00:00",
        "run_ended_at": "2026-05-24T12:01:00+00:00",
        "pr_number": 42,
        "pr_url": "https://github.test/owner/repo/pull/42",
        "expected_outcome": {
            "family": "label tamper",
            "detail": "post-open-add",
        },
        "observed_outcome": {"remaining_mq_labels": [], "activation_state": "error"},
        "timeline": (("2026-05-24T12:00:30+00:00", "audit_ejected", {}),),
        "audit_run_urls": ("https://github.test/owner/repo/actions/runs/9",),
        "passed": True,
        "notes": "",
        "source_path": "rocm_mq.audit_evidence.label_added",
    }
    base.update(overrides)
    return AuditResult(**base)


def test_audit_result_is_frozen_slots_dataclass_with_exact_fields() -> None:
    result = _make_result()

    assert dataclasses.is_dataclass(AuditResult)
    assert not hasattr(result, "__dict__")
    assert {field.name for field in dataclasses.fields(AuditResult)} == _AUDIT_KEYS
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.passed = False  # type: ignore[misc]


def test_emit_result_writes_sorted_json_to_default_audit_runs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = _make_result(run_started_at="2026-05-24T12:00:00+00:00")

    output = emit_result(result)

    assert output.parent == Path(".planning/phases/04A-audit-validator/audit-runs")
    assert output.exists()
    assert ":" not in output.name
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert list(payload) == sorted(_AUDIT_KEYS)
    assert set(payload) == _AUDIT_KEYS
    assert payload["scenario_id"] == "audit_label_added"


def test_emit_result_accepts_explicit_output_dir_and_creates_parents(tmp_path: Path) -> None:
    output_dir = tmp_path / "nested" / "audit-runs"
    output = emit_result(_make_result(), output_dir=output_dir)

    assert output.parent == output_dir
    assert output.exists()
    assert output_dir.exists()


def test_poll_pr_state_returns_first_truthy_predicate_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rocm_mq.audit_evidence import _base

    monkeypatch.setattr(_base.time, "sleep", lambda _seconds: None)
    calls = {"count": 0}

    def predicate(*_args: Any, **_kwargs: Any) -> dict[str, str] | None:
        calls["count"] += 1
        if calls["count"] == 3:
            return {"state": "ready"}
        return None

    result = poll_pr_state(
        object(),
        "owner",
        "repo",
        42,
        predicate=predicate,
        timeout_s=30,
        interval_s=1,
    )

    assert result == {"state": "ready"}
    assert calls["count"] == 3


def test_poll_pr_state_raises_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from rocm_mq.audit_evidence import _base

    monkeypatch.setattr(_base.time, "sleep", lambda _seconds: None)
    now = {"value": 0.0}

    def monotonic() -> float:
        now["value"] += 10.0
        return now["value"]

    monkeypatch.setattr(_base.time, "monotonic", monotonic)

    with pytest.raises(TimeoutError):
        poll_pr_state(
            object(),
            "owner",
            "repo",
            42,
            predicate=lambda *_args: None,
            timeout_s=25,
            interval_s=1,
        )


def test_observation_helpers_read_fake_pr_labels_comments_and_statuses() -> None:
    state = FakeRepoState()
    state.prs[42] = FakePR(
        number=42,
        head_sha="live-sha",
        labels={"mq:queued", "mq:hipdnn", "unrelated"},
    )
    state.comments_store[42] = {
        1000: "<!-- rocm-mq-status -->\nstatus",
        1001: (
            "## Merge queue audit: label tamper\n"
            "@alice triggered audit eject for detail=post-open-add.\n"
            "Re-run `/merge` after fixing the issue."
        ),
        1002: "not an audit comment",
    }
    state.status_store[("live-sha", "merge-queue/active")] = {
        "state": "error",
        "context": "merge-queue/active",
        "created_at": "2026-05-24T12:00:00Z",
        "creator_type": "app",
    }
    client = FakeGitHub(state)

    assert remaining_mq_labels(client, "owner", "repo", 42) == ("mq:hipdnn", "mq:queued")
    assert audit_comments_matching(
        client,
        "owner",
        "repo",
        42,
        family="label tamper",
        detail="post-open-add",
        actor="alice",
    ) == (state.comments_store[42][1001],)
    assert activation_status_observation(
        client,
        "owner",
        "repo",
        "live-sha",
        context="merge-queue/active",
    ) == {"context": "merge-queue/active", "state": "error"}


def test_workflow_run_urls_returns_empty_when_client_has_no_actions_namespace() -> None:
    assert workflow_run_urls(object(), "owner", "repo", pr_number=42) == ()


def test_workflow_run_urls_reads_action_runs_when_available() -> None:
    class _Actions:
        def list_workflow_runs(
            self, owner: str, repo: str, event: str | None = None, **_: Any
        ) -> SimpleNamespace:
            assert (owner, repo, event) == ("owner", "repo", "pull_request_target")
            return SimpleNamespace(
                parsed_data=SimpleNamespace(
                    workflow_runs=(
                        SimpleNamespace(
                            html_url="https://github.test/owner/repo/actions/runs/1"
                        ),
                        SimpleNamespace(
                            html_url="https://github.test/owner/repo/actions/runs/2"
                        ),
                    )
                )
            )

    client = SimpleNamespace(rest=SimpleNamespace(actions=_Actions()))

    assert workflow_run_urls(client, "owner", "repo", pr_number=42) == (
        "https://github.test/owner/repo/actions/runs/1",
        "https://github.test/owner/repo/actions/runs/2",
    )
