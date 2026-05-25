"""Tests for rocm_mq.audit_evidence.aggregator — Phase 04A plan 06.

Coverage map (see 04A-06-PLAN.md Task 1):
  - collect_runs: missing/empty input returns no collected runs.
  - tolerance: malformed JSON and JSON without scenario_id are logged and skipped.
  - latest_passing: latest passed run wins by run_ended_at; newer failures do
    not displace an older pass.
  - render_markdown: one stable section per required RFC §4.3.1 row with PR,
    expected/observed outcome, audit run URLs, notes, and source JSON links.
  - main: --input / --output writes the requested file and exits 0 even before
    live rows have passed.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from rocm_mq.audit_evidence import aggregator
from rocm_mq.audit_evidence._base import AuditResult

_ALL_SCENARIOS = (
    "audit_labels_at_open",
    "audit_label_added",
    "audit_label_removed",
    "audit_base_ref_changed",
    "audit_converted_to_draft",
    "audit_reopened_stale_labels",
)


def _make_result(
    scenario_id: str,
    *,
    run_started_at: str = "2026-05-24T20:20:00+00:00",
    run_ended_at: str = "2026-05-24T20:21:00+00:00",
    passed: bool = True,
    pr_number: int = 1234,
    pr_url: str = "https://github.com/SamuelReeder/rocm-libraries/pull/1234",
    expected_outcome: dict[str, Any] | None = None,
    observed_outcome: dict[str, Any] | None = None,
    audit_run_urls: tuple[str, ...] = (
        "https://github.com/SamuelReeder/rocm-libraries/actions/runs/123456",
    ),
    notes: str = "live fork evidence",
    source_path: str = "rocm_mq.audit_evidence.label_added",
) -> AuditResult:
    return AuditResult(
        scenario_id=scenario_id,
        run_started_at=run_started_at,
        run_ended_at=run_ended_at,
        pr_number=pr_number,
        pr_url=pr_url,
        expected_outcome=expected_outcome
        or {
            "action": "Eject",
            "family": "label tamper",
            "detail": "post-open-add",
        },
        observed_outcome=observed_outcome
        or {
            "remaining_mq_labels": [],
            "activation_status": {"context": "merge-queue/active", "state": "error"},
            "audit_comment_count": 1,
            "passed": True,
        },
        timeline=((run_started_at, "tamper_applied", {"operation": "add_labels"}),),
        audit_run_urls=audit_run_urls,
        passed=passed,
        notes=notes,
        source_path=source_path,
    )


def _write_run(target_dir: Path, result: AuditResult) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = result.run_started_at.replace(":", "-")
    path = target_dir / f"{safe_ts}-{result.scenario_id}.json"
    path.write_text(
        json.dumps(dataclasses.asdict(result), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return path


def test_collect_runs_empty_or_missing_dir_returns_empty_dict(tmp_path: Path) -> None:
    assert aggregator.collect_runs(tmp_path) == {}
    assert aggregator.collect_runs(tmp_path / "missing") == {}


def test_collect_runs_skips_malformed_json_and_missing_scenario_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_run(tmp_path, _make_result("audit_label_added"))
    (tmp_path / "broken.json").write_text("{not valid json", encoding="utf-8")
    (tmp_path / "missing-scenario.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8"
    )

    grouped = aggregator.collect_runs(tmp_path)

    assert list(grouped) == ["audit_label_added"]
    captured = capsys.readouterr()
    assert "malformed JSON" in captured.err
    assert "scenario_id" in captured.err


def test_latest_passing_picks_latest_pass_and_ignores_newer_failure() -> None:
    runs = {
        "audit_label_added": [
            dataclasses.asdict(
                _make_result(
                    "audit_label_added",
                    run_ended_at="2026-05-24T20:21:00+00:00",
                    passed=True,
                )
            ),
            dataclasses.asdict(
                _make_result(
                    "audit_label_added",
                    run_ended_at="2026-05-24T20:23:00+00:00",
                    passed=False,
                    observed_outcome={"passed": False},
                )
            ),
            dataclasses.asdict(
                _make_result(
                    "audit_label_added",
                    run_ended_at="2026-05-24T20:22:00+00:00",
                    passed=True,
                )
            ),
        ]
    }

    latest = aggregator.latest_passing(runs)

    assert latest["audit_label_added"] is not None
    assert latest["audit_label_added"]["run_ended_at"] == "2026-05-24T20:22:00+00:00"


def test_latest_passing_returns_none_when_only_failures_exist() -> None:
    latest = aggregator.latest_passing(
        {
            "audit_label_removed": [
                dataclasses.asdict(_make_result("audit_label_removed", passed=False))
            ]
        }
    )
    assert latest == {"audit_label_removed": None}


def test_render_markdown_contains_all_scenario_sections_and_rfc_rows() -> None:
    rendered = aggregator.render_markdown({}, scenarios=_ALL_SCENARIOS)

    assert rendered.startswith("# AUDIT-RESULTS")
    for scenario_id in _ALL_SCENARIOS:
        assert scenario_id in rendered
        assert aggregator.SCENARIO_RFC_MAP[scenario_id] in rendered
        assert "not yet run" in rendered.lower()


def test_render_markdown_passing_run_contains_required_evidence_fields() -> None:
    result = _make_result(
        "audit_label_added",
        run_ended_at="2026-05-24T20:21:30+00:00",
        pr_number=42,
        pr_url="https://github.com/SamuelReeder/rocm-libraries/pull/42",
        expected_outcome={
            "action": "Eject",
            "family": "label tamper",
            "detail": "post-open-add",
        },
        observed_outcome={
            "remaining_mq_labels": [],
            "activation_status": {"context": "merge-queue/active", "state": "error"},
            "audit_comment_count": 1,
            "passed": True,
        },
        notes="labels cleared and activation status set to error",
    )
    latest = {"audit_label_added": dataclasses.asdict(result)}

    rendered = aggregator.render_markdown(latest, scenarios=("audit_label_added",))

    assert "2026-05-24T20:21:30+00:00" in rendered
    assert "https://github.com/SamuelReeder/rocm-libraries/pull/42" in rendered
    assert "post-open-add" in rendered
    assert "remaining_mq_labels" in rendered
    assert "merge-queue/active" in rendered
    assert (
        "https://github.com/SamuelReeder/rocm-libraries/actions/runs/123456" in rendered
    )
    assert "labels cleared and activation status set to error" in rendered
    assert "audit-runs/" in rendered


def test_main_writes_requested_output_and_exits_zero_with_no_passes(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "runs"
    input_dir.mkdir()
    output_md = tmp_path / "AUDIT-RESULTS.md"

    exit_code = aggregator.main(["--input", str(input_dir), "--output", str(output_md)])

    assert exit_code == 0
    body = output_md.read_text(encoding="utf-8")
    assert "# AUDIT-RESULTS" in body
    assert "not yet run" in body.lower()


def test_main_default_paths_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_input = tmp_path / "audit-runs"
    default_output = tmp_path / "AUDIT-RESULTS.md"
    _write_run(default_input, _make_result("audit_reopened_stale_labels"))
    monkeypatch.setattr(aggregator, "DEFAULT_INPUT_DIR", default_input)
    monkeypatch.setattr(aggregator, "DEFAULT_OUTPUT_MD", default_output)

    exit_code = aggregator.main([])

    assert exit_code == 0
    body = default_output.read_text(encoding="utf-8")
    assert "audit_reopened_stale_labels" in body
