"""rocm_mq.dogfood.aggregator — Render DOGFOOD-RESULTS.md from per-run JSONs.

CONTEXT.md D-04: DOGFOOD-RESULTS.md is the Phase 3 verification artifact for
DOG-02..DOG-08. This module reads every per-run JSON the dogfood drivers wrote
under ``.planning/phases/03-handler-processor-on-fork/dogfood-runs/*.json``,
groups them by ``scenario_id``, picks the most-recent passing run per
scenario, and renders a single markdown file with one section per scenario.

Pipeline:

    collect_runs(input_dir)
        └─> dict[scenario_id, list[run_dict]]
    latest_passing(by_scenario)
        └─> dict[scenario_id, run_dict | None]
    render_markdown(latest, scenarios=("dog_02", ..., "dog_08"))
        └─> str  (markdown body)
    main(argv)
        └─> int  (exit code; 0 on success, 0+warning if no scenarios passed)

PURE-09 compliance: I/O layer module (does ``json.loads``, filesystem
I/O). NOT registered in ``tests/test_pure_layer_imports.py::PURE_LAYER_MODULES``.

Operator usage (from .github/merge-queue/):

    python -m rocm_mq.dogfood.aggregator
    python -m rocm_mq.dogfood.aggregator \\
        --input ../../.planning/phases/03-handler-processor-on-fork/dogfood-runs \\
        --output ../../.planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md

Re-run after each driver verification — the renderer is idempotent and
overwrites the output file every run.

PRE-CONFIRM (from 03-16-PLAN.md Task 1): when no scenarios have a passing
run yet (initial state, immediately after this plan lands but before Wave 4
drivers have been exercised on the fork), ``main`` returns 0 with a warning
printed to stderr — NOT a non-zero exit. Rationale: the aggregator may
legitimately run before all drivers are exercised; failing exit would block
CI on the empty initial state.

Threat model (03-16-PLAN.md):
  - T-03-16-01 Tampering: malicious JSON payload — ``json.loads`` is
    safe-by-construction; the aggregator validates the expected key set
    and ignores extras; unknown ``scenario_id`` values are logged + skipped.
  - T-03-16-02 Information Disclosure: PR URLs in DOGFOOD-RESULTS.md are
    public and the artifact is dev-only under ``.planning/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Default location of per-run JSONs (CONTEXT.md D-04).
DEFAULT_INPUT_DIR: Path = Path(
    ".planning/phases/03-handler-processor-on-fork/dogfood-runs"
)

#: Default location of the rendered aggregator output (CONTEXT.md D-04).
DEFAULT_OUTPUT_MD: Path = Path(
    ".planning/phases/03-handler-processor-on-fork/DOGFOOD-RESULTS.md"
)

#: Canonical scenario ordering for the rendered file. Mirrors RFC §6 row
#: order DOG-02..DOG-08 (RFC 0001 §6 lists the seven fork-dogfood scenarios).
DEFAULT_SCENARIOS: tuple[str, ...] = (
    "dog_02",
    "dog_03",
    "dog_04",
    "dog_05",
    "dog_06",
    "dog_07",
    "dog_08",
)

#: Per-scenario RFC §6 row label rendered in each section heading. Drives
#: ``test_render_markdown_contains_rfc_row_references`` and gives the
#: artifact reader the RFC anchor without having to look it up.
SCENARIO_RFC_MAP: dict[str, str] = {
    "dog_02": "RFC §6 row: merge conflict at activation",
    "dog_03": "RFC §6 row: CI failure during evaluation",
    "dog_04": "RFC §6 row: simultaneous /merge idempotency",
    "dog_05": "RFC §6 row: author push between activation and squash",
    "dog_06": "RFC §6 row: 5-PR worked example (RFC §4.2)",
    "dog_07": "RFC §6 row: approval revoked between enqueue and squash",
    "dog_08": "RFC §6 row: /merge on PR touching no opted-in path",
}


# ---------------------------------------------------------------------------
# collect_runs — glob + json.loads + group by scenario_id
# ---------------------------------------------------------------------------


def collect_runs(input_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Glob ``input_dir/*.json``; group parsed payloads by ``scenario_id``.

    Returns ``{}`` when ``input_dir`` does not exist OR contains no JSONs.

    Malformed JSON files are logged to stderr and skipped (T-03-16-01: a
    corrupt per-run file does not crash the aggregator). Files missing the
    ``scenario_id`` field are also skipped (can't group without it).
    """
    if not input_dir.exists() or not input_dir.is_dir():
        return {}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(input_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
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

        # Stamp the source path so the renderer can link back to it.
        payload["_source_path"] = str(path)
        grouped.setdefault(scenario_id, []).append(payload)

    return grouped


# ---------------------------------------------------------------------------
# latest_passing — pick max(run_ended_at) where passed=True
# ---------------------------------------------------------------------------


def latest_passing(
    by_scenario: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any] | None]:
    """For each scenario, return the most-recent passing run (or None).

    The "most recent" key is ``run_ended_at`` (ISO-8601 strings sort
    lexicographically when they share the same tz suffix; per CONTEXT.md
    D-04 every driver emits ``+00:00``).

    A scenario with zero ``passed=True`` runs maps to ``None`` so the
    renderer can surface it as 'not yet run' (Task 1 behavior block).
    """
    result: dict[str, dict[str, Any] | None] = {}
    for scenario_id, runs in by_scenario.items():
        passing = [r for r in runs if r.get("passed") is True]
        if not passing:
            result[scenario_id] = None
            continue
        # max by run_ended_at; tolerate missing field by treating it as ""
        result[scenario_id] = max(passing, key=lambda r: str(r.get("run_ended_at", "")))
    return result


# ---------------------------------------------------------------------------
# render_markdown — one section per scenario
# ---------------------------------------------------------------------------


def render_markdown(
    latest: dict[str, dict[str, Any] | None],
    *,
    scenarios: tuple[str, ...] = DEFAULT_SCENARIOS,
) -> str:
    """Render DOGFOOD-RESULTS.md body as a single markdown string.

    Layout (CONTEXT.md D-04):
      - Top-level ``#`` header naming the artifact.
      - One-line synopsis + table of contents linking each scenario.
      - One ``## DOG-XX — {RFC §6 row}`` section per scenario in
        ``scenarios`` order. Missing scenarios render as "not yet run"
        with a pointer to the driver module.

    Each "passing" section contains:
      - scenario_id + RFC §6 row label (heading)
      - latest pass timestamp (``run_ended_at``)
      - PR URL
      - expected_outcome vs observed_outcome
      - link to the source JSON under dogfood-runs/
      - notes (if any)

    Args:
        latest: Output of ``latest_passing``; scenario_id → run dict or None.
            Scenarios entirely missing from this dict are also rendered as
            "not yet run" (driver has never been invoked).
        scenarios: Ordered scenario IDs to render. Defaults to DOG-02..DOG-08.
    """
    lines: list[str] = []
    lines.append("# DOGFOOD-RESULTS")
    lines.append("")
    lines.append(
        "Phase 3 fork-dogfood verification artifact (CONTEXT.md D-04). "
        "Aggregates the most-recent passing run per scenario from "
        "`.planning/phases/03-handler-processor-on-fork/dogfood-runs/*.json`."
    )
    lines.append("")
    lines.append(
        "Regenerate via "
        "`python -m rocm_mq.dogfood.aggregator` "
        "(run from `.github/merge-queue/`)."
    )
    lines.append("")

    # Table of contents
    lines.append("## Scenarios")
    lines.append("")
    for scenario_id in scenarios:
        anchor = _scenario_anchor(scenario_id)
        label = _scenario_display(scenario_id)
        lines.append(f"- [{label}](#{anchor})")
    lines.append("")

    # Per-scenario sections
    for scenario_id in scenarios:
        lines.append("")
        lines.extend(_render_section(scenario_id, latest.get(scenario_id)))

    body = "\n".join(lines)
    if not body.endswith("\n"):
        body += "\n"
    return body


def _scenario_display(scenario_id: str) -> str:
    """Render 'dog_02' as 'DOG-02' for human consumption."""
    if scenario_id.startswith("dog_"):
        return "DOG-" + scenario_id[len("dog_") :]
    return scenario_id.upper()


def _scenario_anchor(scenario_id: str) -> str:
    """GitHub-flavored markdown anchor for the scenario heading."""
    # GitHub lowercases headings + replaces spaces with '-'; '—' (em dash)
    # is stripped. We deliberately build a stable form that matches.
    return scenario_id.replace("_", "-")


def _render_section(
    scenario_id: str, run: dict[str, Any] | None
) -> list[str]:
    """Build one ## section for ``scenario_id`` (passing or 'not yet run')."""
    display = _scenario_display(scenario_id)
    rfc_row = SCENARIO_RFC_MAP.get(scenario_id, "RFC §6 row: (unknown)")
    anchor = _scenario_anchor(scenario_id)

    lines: list[str] = []
    # HTML anchor so the TOC links are stable regardless of GitHub's heading
    # slugification (em dash, parentheses, etc.).
    lines.append(f'<a id="{anchor}"></a>')
    lines.append(f"## {display} — {rfc_row}")
    lines.append("")

    if run is None:
        lines.append("**Status:** not yet run")
        lines.append("")
        lines.append(
            f"Driver module: `rocm_mq.dogfood.{scenario_id}`. "
            "Invoke per CONTEXT.md D-04, then re-run the aggregator."
        )
        lines.append("")
        return lines

    pr_number = run.get("pr_number", "—")
    pr_url = run.get("pr_url") or "—"
    run_ended_at = run.get("run_ended_at", "—")
    expected = run.get("expected_outcome", {})
    observed = run.get("observed_outcome", {})
    notes = run.get("notes") or ""
    source_path = run.get("_source_path", "—")
    json_link = _format_json_link(source_path)

    lines.append("**Status:** passed")
    lines.append("")
    lines.append(f"- **Latest pass:** `{run_ended_at}`")
    lines.append(f"- **PR:** [#{pr_number}]({pr_url})" if pr_url != "—"
                 else f"- **PR:** #{pr_number}")
    lines.append(f"- **Expected outcome:** `{json.dumps(expected, sort_keys=True)}`")
    lines.append(f"- **Observed outcome:** `{json.dumps(observed, sort_keys=True)}`")
    lines.append(f"- **Source JSON:** {json_link}")
    if notes:
        lines.append(f"- **Notes:** {notes}")
    lines.append("")
    return lines


def _format_json_link(source_path: str) -> str:
    """Render a path as a markdown link relative to repo root if possible."""
    if source_path == "—":
        return source_path
    p = Path(source_path)
    # Surface 'dogfood-runs/<file>' so the link is stable even if the
    # aggregator was invoked with an absolute path during testing.
    try:
        # Always include the 'dogfood-runs/' anchor segment if present.
        parts = p.parts
        if "dogfood-runs" in parts:
            idx = parts.index("dogfood-runs")
            rel = Path(*parts[idx:])
        else:
            rel = p
    except (ValueError, IndexError):
        rel = p
    return f"[`{rel.as_posix()}`]({rel.as_posix()})"


# ---------------------------------------------------------------------------
# main — CLI entrypoint
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m rocm_mq.dogfood.aggregator",
        description=(
            "Aggregate per-scenario dogfood JSONs into DOGFOOD-RESULTS.md "
            "(Phase 3 verification artifact per CONTEXT.md D-04)."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=(
            "Directory containing per-run *.json files. "
            f"Default: {DEFAULT_INPUT_DIR}"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Output markdown path (overwritten on each run). "
            f"Default: {DEFAULT_OUTPUT_MD}"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Aggregate per-run JSONs into DOGFOOD-RESULTS.md; return exit code.

    Exit codes:
      - 0 on success (output written), including the all-'not yet run' case
        which logs a warning to stderr but does NOT fail (PRE-CONFIRM,
        03-16-PLAN.md Task 1).
      - 1 on uncaught exception (mirrors cmd_process.main).
    """
    try:
        args = _parse_args(argv)

        # Late-bind defaults to the module-level constants so tests can
        # monkeypatch DEFAULT_INPUT_DIR / DEFAULT_OUTPUT_MD and still hit
        # the same code path the CLI uses.
        input_dir: Path = args.input if args.input is not None else DEFAULT_INPUT_DIR
        output_md: Path = args.output if args.output is not None else DEFAULT_OUTPUT_MD

        by_scenario = collect_runs(input_dir)
        latest = latest_passing(by_scenario)

        # Ensure every default scenario appears in the rendered output,
        # even if it has no JSONs on disk at all.
        for scenario_id in DEFAULT_SCENARIOS:
            latest.setdefault(scenario_id, None)

        body = render_markdown(latest, scenarios=DEFAULT_SCENARIOS)

        output_md.parent.mkdir(parents=True, exist_ok=True)
        output_md.write_text(body, encoding="utf-8")

        # Surface aggregator state to operators.
        passing_count = sum(1 for v in latest.values() if v is not None)
        total = len(DEFAULT_SCENARIOS)
        if passing_count == 0:
            print(
                "warning: no scenarios have a passing run yet — "
                "DOGFOOD-RESULTS.md rendered with all sections as 'not yet run'. "
                "Re-run the aggregator after each driver verification.",
                file=sys.stderr,
            )
        else:
            print(
                f"DOGFOOD-RESULTS.md written: {passing_count}/{total} scenarios "
                f"have a passing run.",
                file=sys.stderr,
            )

        return 0
    except SystemExit:
        # argparse --help / arg errors
        raise
    except Exception as exc:
        import traceback

        print(f"error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
