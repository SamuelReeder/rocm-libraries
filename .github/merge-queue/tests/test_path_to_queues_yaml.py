"""Validator-backed tests for the production path_to_queues.yml.

The existing mq-test.yml package test path is the config validation gate: PRs
that edit this YAML run these tests and fail on shape, graph-closure, queue
reference, or trust-boundary errors.
"""

from __future__ import annotations

import pathlib

from rocm_mq.config_validator import load_local, validate_path_to_queues

# .github/merge-queue/tests/test_path_to_queues_yaml.py
#   parents[0] = tests/
#   parents[1] = .github/merge-queue/
_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
_YAML_PATH = _PACKAGE_ROOT / "path_to_queues.yml"


def test_production_yaml_validates_cleanly() -> None:
    """Production YAML must satisfy the semantic validator, not just parse."""
    payload = load_local(_YAML_PATH)

    errors = validate_path_to_queues(payload)

    assert errors == (), [error.code for error in errors]


def test_top_level_keys_remain_queue_routing_only() -> None:
    """Branch protection remains the source of truth for required checks."""
    payload = load_local(_YAML_PATH)

    assert set(payload.keys()) == {"queues", "paths"}


def test_production_queues_present() -> None:
    """All six RFC §4.1 production queues are present in the queues list."""
    payload = load_local(_YAML_PATH)
    expected = {
        "hipdnn",
        "miopen-provider",
        "hipblaslt-provider",
        "hip-kernel-provider",
        "fusilli-provider",
        "integration-tests",
    }

    assert set(payload["queues"]) == expected


def test_comments_do_not_point_to_a_dedicated_validator_workflow() -> None:
    """D-07 binds validation to mq-test.yml, not mq-config-validate.yml."""
    yaml_text = _YAML_PATH.read_text(encoding="utf-8")
    assert "mq-config-validate.yml" not in yaml_text
    assert "mq-config-validate workflow" not in yaml_text.lower()