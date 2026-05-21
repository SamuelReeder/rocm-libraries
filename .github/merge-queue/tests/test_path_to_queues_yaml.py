"""Smoke tests for path_to_queues.yml.

These are PARSE-LEVEL tripwires only: yaml.safe_load succeeds, the top-level
shape matches the handler/processor's expectations, every queue named in a
path entry exists in the queues list, and the dogfood-canary routing is
present.

Full schema validation (graph-closure of upstream/downstream queue
relationships, queue-name lexical rules, etc.) is a future follow-up — a
dedicated mq-config-validate workflow.

Locate the YAML via pathlib so the test runs regardless of CWD (e.g., from
the repo root, from .github/merge-queue, or via pytest-xdist worker).
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

# .github/merge-queue/tests/test_path_to_queues_yaml.py
#   parents[0] = tests/
#   parents[1] = .github/merge-queue/
_YAML_PATH = pathlib.Path(__file__).resolve().parents[1] / "path_to_queues.yml"


# ---------------------------------------------------------------------------
# Module-level fixture: parse the YAML once per session.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parsed() -> dict[str, object]:
    """Return the parsed YAML payload (yaml.safe_load result)."""
    with _YAML_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Parse-level tripwire.
# ---------------------------------------------------------------------------


def test_yaml_parseable() -> None:
    """yaml.safe_load succeeds and returns a dict."""
    assert _YAML_PATH.exists(), f"Expected file at {_YAML_PATH}"
    with _YAML_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), f"Expected dict at top level, got {type(data).__name__}"


# ---------------------------------------------------------------------------
# Structural assertions (expanded smoke coverage).
# ---------------------------------------------------------------------------


def test_top_level_keys(parsed: dict[str, object]) -> None:
    """Top-level keys are exactly {queues, paths}: required-check evaluation
    is delegated to branch protection, so no required_checks section ships
    in this YAML."""
    assert set(parsed.keys()) == {"queues", "paths"}, (
        f"Unexpected top-level keys: {set(parsed.keys())}"
    )


def test_queues_is_list_of_seven_strings(parsed: dict[str, object]) -> None:
    """`queues` is a list of length 7, all strings (six production + dogfood-canary)."""
    queues = parsed["queues"]
    assert isinstance(queues, list)
    assert len(queues) == 7, f"Expected 7 queues, got {len(queues)}: {queues}"
    assert all(isinstance(q, str) for q in queues), f"Non-string queue entry in {queues}"


def test_dogfood_canary_present(parsed: dict[str, object]) -> None:
    """The synthetic `dogfood-canary` queue is in the queues list."""
    assert "dogfood-canary" in parsed["queues"]


def test_no_required_checks_section(parsed: dict[str, object]) -> None:
    """No required_checks section: branch protection is the single source of
    truth for which checks gate a merge."""
    assert "required_checks" not in parsed, (
        "required_checks must not be present — branch protection is the "
        "source of truth. Configure required checks via Settings → Branches "
        "→ Branch protection rules instead."
    )


def test_paths_reference_only_known_queues(parsed: dict[str, object]) -> None:
    """Every queue referenced in any `paths[*].queues` list exists in the top-level queues list."""
    known = set(parsed["queues"])
    for entry in parsed["paths"]:
        assert isinstance(entry, dict), f"paths entry not a dict: {entry}"
        assert "path" in entry and "queues" in entry, f"paths entry missing keys: {entry}"
        unknown = set(entry["queues"]) - known
        assert not unknown, (
            f"paths entry {entry['path']!r} references unknown queues: {unknown}"
        )


def test_dogfood_path_routes_to_canary_only(parsed: dict[str, object]) -> None:
    """A paths entry exists whose path starts with `dogfood/` and routes ONLY to dogfood-canary.

    This guarantees the canary's required check never affects opted-in
    real-code paths — dogfood-canary is the ONLY non-real-code queue.
    """
    matching = [
        e for e in parsed["paths"]
        if isinstance(e.get("path"), str) and e["path"].startswith("dogfood/")
    ]
    assert matching, "No paths entry starting with 'dogfood/' found"
    assert any(e["queues"] == ["dogfood-canary"] for e in matching), (
        f"Expected a dogfood/* entry with queues == ['dogfood-canary']; got {matching}"
    )


def test_production_queues_present(parsed: dict[str, object]) -> None:
    """All six RFC §4.1 production queues are present in the queues list."""
    expected = {
        "hipdnn",
        "miopen-provider",
        "hipblaslt-provider",
        "hip-kernel-provider",
        "fusilli-provider",
        "integration-tests",
    }
    assert expected.issubset(set(parsed["queues"])), (
        f"Missing production queues: {expected - set(parsed['queues'])}"
    )
