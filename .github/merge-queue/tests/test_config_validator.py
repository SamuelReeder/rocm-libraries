from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from rocm_mq.config_validator import load_local, validate_path_to_queues

_YAML_PATH = Path(__file__).resolve().parents[1] / "path_to_queues.yml"

_PRODUCTION_QUEUES = {
    "hipdnn",
    "miopen-provider",
    "hipblaslt-provider",
    "hip-kernel-provider",
    "fusilli-provider",
    "integration-tests",
}

_VALID_PAYLOAD: dict[str, object] = {
    "queues": sorted(_PRODUCTION_QUEUES),
    "paths": [
        {
            "path": "projects/hipdnn/",
            "queues": sorted(_PRODUCTION_QUEUES),
        },
        {"path": "dnn-providers/miopen-provider/", "queues": ["miopen-provider"]},
        {
            "path": "dnn-providers/hipblaslt-provider/",
            "queues": ["hipblaslt-provider"],
        },
        {
            "path": "dnn-providers/hip-kernel-provider/",
            "queues": ["hip-kernel-provider"],
        },
        {"path": "dnn-providers/fusilli-provider/", "queues": ["fusilli-provider"]},
        {
            "path": "dnn-providers/integration-tests/",
            "queues": [
                "miopen-provider",
                "hipblaslt-provider",
                "hip-kernel-provider",
                "fusilli-provider",
                "integration-tests",
            ],
        },
    ],
}


def _codes(payload: Mapping[str, Any], **kwargs: Any) -> set[str]:
    return {error.code for error in validate_path_to_queues(payload, **kwargs)}


def test_production_yaml_loads_and_validates_cleanly() -> None:
    payload = load_local(_YAML_PATH)

    errors = validate_path_to_queues(payload)

    assert errors == ()


@pytest.mark.parametrize(
    ("payload", "expected_codes"),
    [
        ({}, {"shape.missing_queues", "shape.missing_paths"}),
        ({"queues": "hipdnn", "paths": []}, {"shape.queues_not_list"}),
        ({"queues": ["hipdnn", 7], "paths": []}, {"shape.queue_not_string"}),
        ({"queues": [], "paths": "projects/hipdnn/"}, {"shape.paths_not_list"}),
        ({"queues": [], "paths": ["projects/hipdnn/"]}, {"shape.path_entry_not_mapping"}),
        ({"queues": [], "paths": [{"queues": []}]}, {"shape.path_missing"}),
        ({"queues": [], "paths": [{"path": 7, "queues": []}]}, {"shape.path_not_string"}),
        ({"queues": [], "paths": [{"path": "projects/hipdnn/"}]}, {"shape.entry_queues_missing"}),
        (
            {"queues": [], "paths": [{"path": "projects/hipdnn/", "queues": "hipdnn"}]},
            {"shape.entry_queues_not_list"},
        ),
        (
            {"queues": [], "paths": [{"path": "projects/hipdnn/", "queues": [7]}]},
            {"shape.entry_queue_not_string"},
        ),
    ],
)
def test_shape_errors_have_stable_codes(
    payload: Mapping[str, Any], expected_codes: set[str]
) -> None:
    assert expected_codes <= _codes(payload)


def test_load_local_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    config_path = tmp_path / "path_to_queues.yml"
    config_path.write_text("- not\n- a mapping\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not a mapping"):
        load_local(config_path)


def test_unknown_queue_reference_is_reported() -> None:
    payload = {
        "queues": ["hipdnn"],
        "paths": [{"path": "projects/hipdnn/", "queues": ["hipdnn", "unknown"]}],
    }

    assert "ref.unknown_queue" in _codes(payload)


def test_declared_queue_with_no_path_reference_is_reported() -> None:
    payload = {"queues": ["hipdnn", "miopen-provider"], "paths": []}

    assert "ref.unrouted_queue" in _codes(payload)


def test_valid_payload_matches_exact_stage_one_graph_closure() -> None:
    assert validate_path_to_queues(_VALID_PAYLOAD) == ()


@pytest.mark.parametrize(
    ("path", "queues"),
    [
        ("projects/hipdnn/", ["hipdnn"]),
        ("dnn-providers/integration-tests/", ["integration-tests"]),
        ("dnn-providers/miopen-provider/", ["miopen-provider", "integration-tests"]),
    ],
)
def test_missing_or_extra_stage_one_closure_is_reported(
    path: str, queues: list[str]
) -> None:
    payload = {
        "queues": list(_PRODUCTION_QUEUES),
        "paths": [
            entry if entry["path"] != path else {"path": path, "queues": queues}
            for entry in _VALID_PAYLOAD["paths"]  # type: ignore[index]
        ],
    }

    assert "graph.path_queues_mismatch" in _codes(payload)


def test_missing_stage_one_path_is_reported() -> None:
    payload = {
        "queues": list(_PRODUCTION_QUEUES),
        "paths": [
            entry
            for entry in _VALID_PAYLOAD["paths"]  # type: ignore[index]
            if entry["path"] != "dnn-providers/fusilli-provider/"
        ],
    }

    assert "graph.missing_path" in _codes(payload)


def test_extra_stage_one_path_is_reported() -> None:
    payload = {
        "queues": list(_PRODUCTION_QUEUES),
        "paths": [
            *_VALID_PAYLOAD["paths"],  # type: ignore[misc]
            {"path": "projects/not-opted-in/", "queues": ["hipdnn"]},
        ],
    }

    assert "graph.unexpected_path" in _codes(payload)


@pytest.mark.parametrize(
    "path",
    [
        ".github/labels.yml",
        ".GitHub/workflows/mq-handler.yml",
        ".github/workflows/mq-handler.yml",
        ".github/merge-queue/src/rocm_mq/config.py",
        ".github/merge-queue/path_to_queues.yml",
    ],
)
def test_protected_route_entries_are_rejected(path: str) -> None:
    payload = {
        "queues": list(_PRODUCTION_QUEUES),
        "paths": [{"path": path, "queues": ["hipdnn"]}],
    }

    assert "trust.protected_path" in _codes(payload)


def test_generated_protected_source_mapping_is_consumed_from_shared_helper() -> None:
    payload = {
        "queues": list(_PRODUCTION_QUEUES),
        "paths": [{"path": "tools/generate-mq-workflow.py", "queues": ["hipdnn"]}],
    }

    codes = _codes(
        payload,
        generated_protected_path_pairs=(
            ("tools/generate-mq-workflow.py", ".github/workflows/mq-handler.yml"),
        ),
    )

    assert "trust.generated_protected_path" in codes


def test_validation_error_objects_are_structured_and_stable() -> None:
    errors = validate_path_to_queues(
        {"queues": [], "paths": [{"path": ".github/workflows/x.yml", "queues": []}]}
    )

    assert errors
    error = errors[0]
    assert isinstance(error.code, str)
    assert isinstance(error.path, str)
    assert isinstance(error.message, str)
    assert isinstance(error.details, tuple)
