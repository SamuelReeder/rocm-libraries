"""Tests for RFC §8 protected-path normalization helpers."""

from __future__ import annotations

import pytest

from rocm_mq import protected_paths


def test_direct_protected_paths_match_existing_self_bootstrap_roots() -> None:
    paths = [
        ".github/workflows/foo.yml",
        ".github/merge-queue/src/rocm_mq/gh.py",
        ".github/merge-queue/path_to_queues.yml",
    ]

    assert protected_paths.find_protected_path_hits(paths) == paths


@pytest.mark.parametrize(
    "path",
    [
        ".GitHub/workflows/foo.yml",
        ".GITHUB/merge-queue/path_to_queues.yml",
        "./.github/merge-queue/src/rocm_mq/cmd_handle.py",
        "/.github/workflows/foo.yml",
        ".github//workflows/foo.yml",
        ".github/workflows/symlink-to-mq-handler.yml",
    ],
)
def test_protected_path_normalization_catches_adversarial_spellings(path: str) -> None:
    assert protected_paths.is_protected_path(path) is True


def test_diagnostic_hits_preserve_original_strings_and_order() -> None:
    paths = [
        "projects/hipdnn/src/foo.cpp",
        ".GitHub/workflows/Foo.yml",
        "./.github//merge-queue/src/rocm_mq/gh.py",
        "docs/readme.md",
        "/.github/merge-queue/path_to_queues.yml",
    ]

    assert protected_paths.find_protected_path_hits(paths) == [
        ".GitHub/workflows/Foo.yml",
        "./.github//merge-queue/src/rocm_mq/gh.py",
        "/.github/merge-queue/path_to_queues.yml",
    ]


def test_generated_protected_path_inventory_records_targeted_none_found_result() -> None:
    assert protected_paths.GENERATED_PROTECTED_PATH_PAIRS == ()
    assert protected_paths.GENERATED_PROTECTED_PATH_INVENTORY_RESULT == "none found"


def test_generated_pair_hits_when_source_or_output_crosses_protected_roots() -> None:
    pairs = (
        (".GitHub/workflows/generated.yml", "docs/rendered.yml"),
        ("tools/render-mq-workflow.py", ".github/workflows/generated.yml"),
        ("tools/render-docs.py", "docs/generated.md"),
    )

    assert protected_paths.generated_pair_hits(pairs) == [
        ".GitHub/workflows/generated.yml",
        "tools/render-mq-workflow.py",
    ]


def test_generated_source_hit_preserves_original_changed_source_path() -> None:
    pairs = (("tools/render-mq-workflow.py", ".github/workflows/generated.yml"),)
    paths = ["docs/readme.md", "./tools//render-mq-workflow.py"]

    assert protected_paths.find_generated_protected_path_hits(paths, pairs=pairs) == [
        "./tools//render-mq-workflow.py"
    ]
