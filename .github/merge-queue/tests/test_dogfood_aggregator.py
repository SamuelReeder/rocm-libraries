"""Tests for rocm_mq.dogfood.aggregator — Phase 3 plan 03-16.

Coverage map (see 03-16-PLAN.md Task 1 + Task 2 behavior blocks):
  - collect_runs: globs *.json in input dir, groups by scenario_id
  - latest_passing: picks max(run_ended_at) per scenario where passed=True;
    None for scenarios with no passing run
  - render_markdown: one section per scenario, RFC §6 row label, latest
    timestamp / PR URL / expected vs observed / JSON link; missing scenarios
    rendered as 'not yet run'
  - main: --input / --output writes markdown to chosen path; default paths
    work via monkeypatch; exit code 0 with warning when no scenarios pass yet
    (PRE-CONFIRM rationale: aggregator runs before some drivers exercised)
  - tolerance: malformed JSON files are logged + skipped, not crashing

Per CONTEXT.md D-04, DOGFOOD-RESULTS.md is the Phase 3 verification artifact
for DOG-02..DOG-08; the aggregator's contract is "regenerate it idempotently
from per-run JSONs without crashing on partial input".
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from rocm_mq.dogfood import aggregator
from rocm_mq.dogfood._base import DogfoodResult

# ---------------------------------------------------------------------------
# Fixture helpers — build DogfoodResult-shaped dicts written as per-run JSONs
# ---------------------------------------------------------------------------


def _make_result(
    scenario_id: str,
    *,
    run_ended_at: str = "2026-05-19T12:00:00+00:00",
    run_started_at: str = "2026-05-19T11:55:00+00:00",
    passed: bool = True,
    pr_number: int = 1234,
    pr_url: str = "https://github.com/SamuelReeder/rocm-libraries/pull/1234",
    expected_outcome: dict[str, Any] | None = None,
    observed_outcome: dict[str, Any] | None = None,
    notes: str = "",
) -> DogfoodResult:
    """Build a minimal DogfoodResult for test fixtures."""
    return DogfoodResult(
        scenario_id=scenario_id,
        run_started_at=run_started_at,
        run_ended_at=run_ended_at,
        pr_number=pr_number,
        pr_url=pr_url,
        expected_outcome=expected_outcome or {"action": "Squash"},
        observed_outcome=observed_outcome or {"action": "Squash"},
        timeline=(),
        processor_run_urls=(),
        step_summary_excerpt="",
        passed=passed,
        notes=notes,
    )


def _write_run(target_dir: Path, result: DogfoodResult) -> Path:
    """Mimic _base.emit_result filename shape and write JSON under target_dir."""
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = result.run_started_at.replace(":", "-")
    path = target_dir / f"{safe_ts}-{result.scenario_id}.json"
    path.write_text(
        json.dumps(dataclasses.asdict(result), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# collect_runs
# ---------------------------------------------------------------------------


def test_collect_runs_empty_dir_returns_empty_dict(tmp_path: Path) -> None:
    """Empty dogfood-runs/ must not crash — returns {}."""
    result = aggregator.collect_runs(tmp_path)
    assert result == {}


def test_collect_runs_missing_dir_returns_empty_dict(tmp_path: Path) -> None:
    """Nonexistent input dir is treated as 'no runs yet'."""
    missing = tmp_path / "does_not_exist"
    result = aggregator.collect_runs(missing)
    assert result == {}


def test_collect_runs_groups_by_scenario_id(tmp_path: Path) -> None:
    """Multiple JSONs across multiple scenarios group correctly."""
    _write_run(
        tmp_path, _make_result("dog_02", run_started_at="2026-05-19T11:55:00+00:00")
    )
    _write_run(
        tmp_path, _make_result("dog_02", run_started_at="2026-05-19T12:55:00+00:00")
    )
    _write_run(
        tmp_path, _make_result("dog_06", run_started_at="2026-05-19T10:00:00+00:00")
    )

    grouped = aggregator.collect_runs(tmp_path)

    assert set(grouped.keys()) == {"dog_02", "dog_06"}
    assert len(grouped["dog_02"]) == 2
    assert len(grouped["dog_06"]) == 1


def test_collect_runs_skips_malformed_json(tmp_path: Path) -> None:
    """A malformed file must be logged + skipped, not crash the aggregator."""
    _write_run(tmp_path, _make_result("dog_02"))
    (tmp_path / "9999-broken.json").write_text("{not valid json", encoding="utf-8")

    grouped = aggregator.collect_runs(tmp_path)

    assert "dog_02" in grouped
    assert len(grouped["dog_02"]) == 1


def test_collect_runs_skips_missing_scenario_id(tmp_path: Path) -> None:
    """A JSON missing scenario_id is skipped (unknown scenario, can't group)."""
    _write_run(tmp_path, _make_result("dog_02"))
    (tmp_path / "weird.json").write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

    grouped = aggregator.collect_runs(tmp_path)

    assert list(grouped.keys()) == ["dog_02"]


# ---------------------------------------------------------------------------
# latest_passing
# ---------------------------------------------------------------------------


def test_latest_passing_picks_max_ended_at() -> None:
    """Of three passing runs, the one with the latest run_ended_at wins."""
    runs = {
        "dog_02": [
            dataclasses.asdict(
                _make_result("dog_02", run_ended_at="2026-05-19T10:00:00+00:00")
            ),
            dataclasses.asdict(
                _make_result("dog_02", run_ended_at="2026-05-19T12:00:00+00:00")
            ),
            dataclasses.asdict(
                _make_result("dog_02", run_ended_at="2026-05-19T11:00:00+00:00")
            ),
        ]
    }

    latest = aggregator.latest_passing(runs)

    assert latest["dog_02"] is not None
    assert latest["dog_02"]["run_ended_at"] == "2026-05-19T12:00:00+00:00"


def test_latest_passing_ignores_failed_runs() -> None:
    """An older passing run must be picked over a newer failed run."""
    runs = {
        "dog_03": [
            dataclasses.asdict(
                _make_result(
                    "dog_03", run_ended_at="2026-05-19T10:00:00+00:00", passed=True
                )
            ),
            dataclasses.asdict(
                _make_result(
                    "dog_03", run_ended_at="2026-05-19T12:00:00+00:00", passed=False
                )
            ),
        ]
    }

    latest = aggregator.latest_passing(runs)

    assert latest["dog_03"] is not None
    assert latest["dog_03"]["run_ended_at"] == "2026-05-19T10:00:00+00:00"


def test_latest_passing_returns_none_when_all_failed() -> None:
    """A scenario with only failed runs gets None — surfaced as 'not yet run'."""
    runs = {
        "dog_04": [
            dataclasses.asdict(_make_result("dog_04", passed=False)),
        ]
    }

    latest = aggregator.latest_passing(runs)

    assert latest["dog_04"] is None


def test_latest_passing_empty_input_returns_empty_dict() -> None:
    """No scenarios in → no scenarios out (renderer fills in 'not yet run')."""
    assert aggregator.latest_passing({}) == {}


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


_ALL_SCENARIOS = (
    "dog_02",
    "dog_03",
    "dog_04",
    "dog_05",
    "dog_06",
    "dog_07",
    "dog_08",
)


def test_render_markdown_contains_section_per_scenario() -> None:
    """One ## section per requested scenario, even when none have run yet."""
    rendered = aggregator.render_markdown({}, scenarios=_ALL_SCENARIOS)
    for scenario_id in _ALL_SCENARIOS:
        # Each scenario_id appears at least once as a heading anchor
        assert scenario_id in rendered.lower() or scenario_id.upper() in rendered


def test_render_markdown_missing_scenario_shows_not_yet_run() -> None:
    """A scenario whose latest_passing entry is None renders 'not yet run'."""
    latest: dict[str, dict[str, Any] | None] = {"dog_02": None}
    rendered = aggregator.render_markdown(latest, scenarios=("dog_02",))
    assert "not yet run" in rendered.lower()


def test_render_markdown_contains_rfc_row_references() -> None:
    """Every scenario heading includes its SCENARIO_RFC_MAP description."""
    rendered = aggregator.render_markdown({}, scenarios=_ALL_SCENARIOS)
    for scenario_id, rfc_row in aggregator.SCENARIO_RFC_MAP.items():
        if scenario_id in _ALL_SCENARIOS:
            assert rfc_row in rendered, f"missing RFC row for {scenario_id}: {rfc_row}"


def test_render_markdown_passing_run_contains_fields() -> None:
    """Rendered section for a passing run includes timestamp, PR URL, outcomes."""
    result = _make_result(
        "dog_02",
        run_ended_at="2026-05-19T12:34:56+00:00",
        pr_number=4242,
        pr_url="https://github.com/SamuelReeder/rocm-libraries/pull/4242",
        expected_outcome={"action": "Eject", "reason": "merge conflict"},
        observed_outcome={"action": "Eject", "reason": "merge conflict"},
    )
    latest = {"dog_02": dataclasses.asdict(result)}

    rendered = aggregator.render_markdown(latest, scenarios=("dog_02",))

    assert "2026-05-19T12:34:56+00:00" in rendered
    assert "https://github.com/SamuelReeder/rocm-libraries/pull/4242" in rendered
    assert "Eject" in rendered
    assert "merge conflict" in rendered


def test_render_markdown_contains_json_link() -> None:
    """Rendered section links back to the JSON file under dogfood-runs/."""
    result = _make_result("dog_02", run_started_at="2026-05-19T11-55-00+00:00")
    latest = {"dog_02": dataclasses.asdict(result)}

    rendered = aggregator.render_markdown(latest, scenarios=("dog_02",))

    assert "dogfood-runs/" in rendered


def test_render_markdown_has_top_level_header() -> None:
    """The output begins with a top-level # heading naming the artifact."""
    rendered = aggregator.render_markdown({}, scenarios=_ALL_SCENARIOS)
    assert rendered.lstrip().startswith("# ")


# ---------------------------------------------------------------------------
# main — CLI behavior
# ---------------------------------------------------------------------------


def test_main_writes_markdown_to_explicit_output(tmp_path: Path) -> None:
    """--input + --output writes the rendered markdown to the requested path."""
    input_dir = tmp_path / "runs"
    input_dir.mkdir()
    _write_run(input_dir, _make_result("dog_02"))
    output_md = tmp_path / "RESULTS.md"

    exit_code = aggregator.main(["--input", str(input_dir), "--output", str(output_md)])

    assert exit_code == 0
    assert output_md.exists()
    content = output_md.read_text(encoding="utf-8")
    assert "dog_02" in content.lower() or "DOG-02" in content


def test_main_default_paths_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No args uses the module-level DEFAULT paths (monkeypatched to tmp)."""
    default_input = tmp_path / "default-runs"
    default_input.mkdir()
    _write_run(default_input, _make_result("dog_06"))
    default_output = tmp_path / "DOGFOOD-RESULTS.md"

    monkeypatch.setattr(aggregator, "DEFAULT_INPUT_DIR", default_input)
    monkeypatch.setattr(aggregator, "DEFAULT_OUTPUT_MD", default_output)

    exit_code = aggregator.main([])

    assert exit_code == 0
    assert default_output.exists()
    body = default_output.read_text(encoding="utf-8")
    assert "dog_06" in body.lower() or "DOG-06" in body


def test_main_exits_zero_when_no_runs_yet(tmp_path: Path) -> None:
    """Per PRE-CONFIRM: 0 + warning (not failure) when no scenarios have passed.

    Rationale: aggregator can legitimately run before any driver has been
    exercised (e.g., immediately after plan 03-16 lands but before Wave 4
    drivers run on the fork). Failing exit would block CI on the empty
    initial state.
    """
    input_dir = tmp_path / "runs"
    input_dir.mkdir()
    output_md = tmp_path / "DOGFOOD-RESULTS.md"

    exit_code = aggregator.main(["--input", str(input_dir), "--output", str(output_md)])

    assert exit_code == 0
    assert output_md.exists()
    # All scenarios should render as 'not yet run'
    body = output_md.read_text(encoding="utf-8")
    assert "not yet run" in body.lower()
