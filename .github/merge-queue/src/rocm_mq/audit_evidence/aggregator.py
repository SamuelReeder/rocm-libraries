"""Render AUDIT-RESULTS.md from per-row RFC §4.3.1 evidence JSONs.

Phase 04A plan 06 completes AUDIT-03 by turning the live audit evidence
emitted by ``rocm_mq.audit_evidence`` drivers into a replayable markdown
artifact.  The renderer is intentionally tolerant of partial input: malformed
or incomplete JSON files are logged and skipped, failed rows remain on disk,
and only the latest ``passed: true`` run per scenario is summarized as passing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_INPUT_DIR: Path = Path(".planning/phases/04A-audit-validator/audit-runs")
DEFAULT_OUTPUT_MD: Path = Path(".planning/phases/04A-audit-validator/AUDIT-RESULTS.md")

DEFAULT_SCENARIOS: tuple[str, ...] = (
    "audit_labels_at_open",
    "audit_label_added",
    "audit_label_removed",
    "audit_base_ref_changed",
    "audit_converted_to_draft",
    "audit_reopened_stale_labels",
)

SCENARIO_RFC_MAP: dict[str, str] = {
    "audit_labels_at_open": "RFC §4.3.1 row: Add any `mq:*` label (labels at open)",
    "audit_label_added": "RFC §4.3.1 row: Add any `mq:*` label (post-open)",
    "audit_label_removed": "RFC §4.3.1 row: Remove any `mq:*` label",
    "audit_base_ref_changed": "RFC §4.3.1 row: Change PR base branch away from `develop`",
    "audit_converted_to_draft": "RFC §4.3.1 row: Convert PR to draft",
    "audit_reopened_stale_labels": (
        "RFC §4.3.1 row: Close-then-reopen with `mq:*` labels still applied"
    ),
}


def collect_runs(input_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Load ``input_dir/*.json`` and group valid run payloads by scenario.

    Missing directories and empty directories are valid initial states and
    return ``{}``.  A malformed file or a file without a usable
    ``scenario_id`` is skipped with a warning so one bad row cannot prevent the
    remaining evidence from rendering.
    """

    if not input_dir.exists() or not input_dir.is_dir():
        return {}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(input_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"warning: skipping malformed JSON {path.name}: {exc}",
                file=sys.stderr,
            )
            continue

        if not isinstance(payload, dict):
            print(
                f"warning: skipping {path.name} — top-level JSON is not an object",
                file=sys.stderr,
            )
            continue

        scenario_id = payload.get("scenario_id")
        if not isinstance(scenario_id, str) or not scenario_id:
            print(
                f"warning: skipping {path.name} — missing or invalid scenario_id",
                file=sys.stderr,
            )
            continue

        payload["_source_path"] = str(path)
        grouped.setdefault(scenario_id, []).append(payload)

    return grouped


def latest_passing(
    by_scenario: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any] | None]:
    """Return the latest passing run for each collected scenario.

    Newer failed runs do not displace older passing evidence.  A scenario with
    collected runs but no pass maps to ``None`` so the renderer never invents a
    passing row for missing or failed evidence.
    """

    latest: dict[str, dict[str, Any] | None] = {}
    for scenario_id, runs in by_scenario.items():
        passing = [run for run in runs if run.get("passed") is True]
        if not passing:
            latest[scenario_id] = None
            continue
        latest[scenario_id] = max(
            passing,
            key=lambda run: str(run.get("run_ended_at", "")),
        )
    return latest


def render_markdown(
    latest: dict[str, dict[str, Any] | None],
    *,
    scenarios: tuple[str, ...] = DEFAULT_SCENARIOS,
) -> str:
    """Render ``AUDIT-RESULTS.md`` as deterministic markdown."""

    lines: list[str] = [
        "# AUDIT-RESULTS",
        "",
        "Phase 04A RFC §4.3.1 audit evidence artifact (CONTEXT.md D-04). "
        "Aggregates the most-recent passing run per scenario from "
        "`.planning/phases/04A-audit-validator/audit-runs/*.json`.",
        "",
        "Regenerate via `python -m rocm_mq.audit_evidence.aggregator` "
        "(run from `.github/merge-queue/`).",
        "",
        "## Scenarios",
        "",
    ]

    for scenario_id in scenarios:
        lines.append(f"- [{scenario_id}](#{_scenario_anchor(scenario_id)})")
    lines.append("")

    for scenario_id in scenarios:
        lines.append("")
        lines.extend(_render_section(scenario_id, latest.get(scenario_id)))

    body = "\n".join(lines)
    if not body.endswith("\n"):
        body += "\n"
    return body


def _scenario_anchor(scenario_id: str) -> str:
    return scenario_id.replace("_", "-")


def _render_section(scenario_id: str, run: dict[str, Any] | None) -> list[str]:
    rfc_row = SCENARIO_RFC_MAP.get(scenario_id, "RFC §4.3.1 row: (unknown)")
    lines = [
        f'<a id="{_scenario_anchor(scenario_id)}"></a>',
        f"## {scenario_id} — {rfc_row}",
        "",
    ]

    if run is None:
        lines.extend(
            (
                "**Status:** not yet run",
                "",
                f"Driver module: `rocm_mq.audit_evidence.{_driver_module_name(scenario_id)}`. "
                "Invoke per 04A-06-PLAN.md, then re-run the aggregator.",
                "",
            )
        )
        return lines

    pr_number = run.get("pr_number", "—")
    pr_url = run.get("pr_url") or "—"
    run_ended_at = run.get("run_ended_at", "—")
    expected = run.get("expected_outcome", {})
    observed = run.get("observed_outcome", {})
    notes = run.get("notes") or ""
    source_json = _format_json_link(str(run.get("_source_path", "—")))
    audit_run_urls = _audit_run_urls(run)
    source_path = run.get("source_path") or ""

    lines.append("**Status:** passed")
    lines.append("")
    lines.append(f"- **Latest pass:** `{run_ended_at}`")
    if pr_url != "—":
        lines.append(f"- **PR:** [#{pr_number}]({pr_url})")
    else:
        lines.append(f"- **PR:** #{pr_number}")
    lines.append(f"- **Expected outcome:** `{json.dumps(expected, sort_keys=True)}`")
    lines.append(f"- **Observed outcome:** `{json.dumps(observed, sort_keys=True)}`")
    if audit_run_urls:
        rendered_urls = ", ".join(
            f"[run {idx}]({url})" for idx, url in enumerate(audit_run_urls, start=1)
        )
        lines.append(f"- **Audit run URLs:** {rendered_urls}")
    else:
        lines.append("- **Audit run URLs:** —")
    if source_path:
        lines.append(f"- **Driver source:** `{source_path}`")
    lines.append(f"- **Source JSON:** {source_json}")
    if notes:
        lines.append(f"- **Notes:** {notes}")
    lines.append("")
    return lines


def _driver_module_name(scenario_id: str) -> str:
    if scenario_id.startswith("audit_"):
        return scenario_id[len("audit_") :]
    return scenario_id


def _audit_run_urls(run: dict[str, Any]) -> tuple[str, ...]:
    urls = run.get("audit_run_urls")
    if not isinstance(urls, (list, tuple)):
        return ()
    return tuple(str(url) for url in urls if url)


def _format_json_link(source_path: str) -> str:
    if source_path == "—":
        return source_path

    path = Path(source_path)
    parts = path.parts
    if "audit-runs" in parts:
        idx = parts.index("audit-runs")
        rel = Path(*parts[idx:])
    else:
        rel = path
    return f"[`{rel.as_posix()}`]({rel.as_posix()})"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m rocm_mq.audit_evidence.aggregator",
        description=(
            "Aggregate RFC §4.3.1 audit evidence JSONs into AUDIT-RESULTS.md "
            "(Phase 04A verification artifact)."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=f"Directory containing per-run *.json files. Default: {DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"Output markdown path. Default: {DEFAULT_OUTPUT_MD}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Aggregate per-run audit JSONs into ``AUDIT-RESULTS.md``."""

    try:
        args = _parse_args(argv)
        input_dir: Path = args.input if args.input is not None else DEFAULT_INPUT_DIR
        output_md: Path = args.output if args.output is not None else DEFAULT_OUTPUT_MD

        latest = latest_passing(collect_runs(input_dir))
        for scenario_id in DEFAULT_SCENARIOS:
            latest.setdefault(scenario_id, None)

        body = render_markdown(latest, scenarios=DEFAULT_SCENARIOS)
        output_md.parent.mkdir(parents=True, exist_ok=True)
        output_md.write_text(body, encoding="utf-8")

        passing = sum(1 for row in latest.values() if row is not None)
        total = len(DEFAULT_SCENARIOS)
        if passing == 0:
            print(
                "warning: no audit scenarios have a passing run yet — "
                "AUDIT-RESULTS.md rendered with all sections as 'not yet run'.",
                file=sys.stderr,
            )
        else:
            print(
                f"AUDIT-RESULTS.md written: {passing}/{total} scenarios have a passing run.",
                file=sys.stderr,
            )
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        import traceback

        print(f"error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


__all__ = [
    "DEFAULT_INPUT_DIR",
    "DEFAULT_OUTPUT_MD",
    "DEFAULT_SCENARIOS",
    "SCENARIO_RFC_MAP",
    "collect_runs",
    "latest_passing",
    "main",
    "render_markdown",
]


if __name__ == "__main__":
    raise SystemExit(main())
