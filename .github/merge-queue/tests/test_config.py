"""tests/test_config.py — Unit tests for rocm_mq.config.

Covers:
- SELF_BOOTSTRAP_PATHS: type (tuple of str), membership of the four required
  globs (RFC §8 self-bootstrap protection, CLAUDE.md branch-protection-as-code
  integration), and the documented future-slot comment for ``terraform/github/**``.
- load_from_develop: happy path (Contents API + base64 + safe_load returns dict),
  develop-ref pinning, and safe_load enforcement against ``!!python/object`` tags
  (T-03-02-01 mitigation per the plan's threat register).
- APP_SLUG_ENV / APP_ID_ENV: literal env-var-name constants (consumed by
  plan 03-06's handler for App identity pinning).
"""

from __future__ import annotations

import base64
import inspect
from pathlib import Path

import pytest
import yaml

import rocm_mq.config as config_module
from rocm_mq.config import (
    APP_ID_ENV,
    APP_SLUG_ENV,
    SELF_BOOTSTRAP_PATHS,
    load_from_develop,
)
from tests.gh_fake import FakeGitHub, FakeRepoState

# ---------------------------------------------------------------------------
# SELF_BOOTSTRAP_PATHS — type, membership, future-slot documentation
# ---------------------------------------------------------------------------


def test_self_bootstrap_paths_is_immutable_tuple() -> None:
    """SELF_BOOTSTRAP_PATHS is a tuple (immutable) of str entries."""
    assert isinstance(SELF_BOOTSTRAP_PATHS, tuple)
    assert all(isinstance(p, str) for p in SELF_BOOTSTRAP_PATHS)


@pytest.mark.parametrize(
    "required_glob",
    [
        ".github/workflows/**",
        ".github/merge-queue/**",
        ".github/merge-queue/path_to_queues.yml",
        ".github/workflows/mq-dogfood-canary.yml",
    ],
)
def test_self_bootstrap_paths_contains_required_globs(required_glob: str) -> None:
    """RFC §8 self-bootstrap protection: four globs MUST be present today."""
    assert required_glob in SELF_BOOTSTRAP_PATHS


def test_self_bootstrap_paths_documents_future_slot() -> None:
    """The source file must mention 'terraform' in a future-slot comment.

    Per CLAUDE.md "branch-protection-as-code integration (RFC §8)": the empty
    slot for ``terraform/github/**`` (or equivalent IaC tool) is documented in
    a source comment so the next person to add Terraform / Probot-Settings
    knows where to extend the constant.
    """
    source_path = inspect.getsourcefile(config_module)
    assert source_path is not None
    source = Path(source_path).read_text(encoding="utf-8")
    assert "terraform" in source, (
        "config.py must document the future terraform/github/** slot per "
        "CLAUDE.md branch-protection-as-code guidance"
    )


# ---------------------------------------------------------------------------
# load_from_develop — happy path + develop-ref pinning + safe_load enforcement
# ---------------------------------------------------------------------------


def _seed_contents(
    fake: FakeGitHub, *, path: str, ref: str, payload: str
) -> list[dict]:
    """Patch FakeGitHub.rest.repos with a get_content method backed by a seed table.

    Returns a call-log list the test can inspect (each call appends a dict
    of the kwargs the executor passed). The base FakeGitHub does not model
    the Contents API; this helper is the per-test extension pattern used
    elsewhere in the suite.
    """
    from types import SimpleNamespace

    call_log: list[dict] = []
    encoded = base64.b64encode(payload.encode()).decode()

    def get_content(
        owner: str,
        repo: str,
        content_path: str,
        *,
        ref: str = "",
        **_: object,
    ) -> SimpleNamespace:
        call_log.append(
            {"owner": owner, "repo": repo, "path": content_path, "ref": ref}
        )
        if content_path != path:
            raise AssertionError(
                f"unexpected path {content_path!r}; seeded {path!r}"
            )
        return SimpleNamespace(parsed_data=SimpleNamespace(content=encoded))

    fake.rest.repos.get_content = get_content  # type: ignore[attr-defined]
    return call_log


def test_load_from_develop_returns_dict() -> None:
    """Happy path: base64-encoded YAML → parsed dict."""
    fake = FakeGitHub(FakeRepoState())
    _seed_contents(
        fake,
        path=".github/merge-queue/path_to_queues.yml",
        ref="develop",
        payload="queues:\n  - hipdnn\n",
    )
    result = load_from_develop(fake, "owner", "repo")  # type: ignore[arg-type]
    assert result == {"queues": ["hipdnn"]}


def test_load_from_develop_uses_develop_ref() -> None:
    """Loader MUST pass ref='develop' to the Contents API call."""
    fake = FakeGitHub(FakeRepoState())
    call_log = _seed_contents(
        fake,
        path=".github/merge-queue/path_to_queues.yml",
        ref="develop",
        payload="queues: []\n",
    )
    load_from_develop(fake, "owner", "repo")  # type: ignore[arg-type]
    assert len(call_log) == 1
    assert call_log[0]["ref"] == "develop"
    assert call_log[0]["path"] == ".github/merge-queue/path_to_queues.yml"


def test_load_from_develop_rejects_unsafe_yaml_tags() -> None:
    """safe_load (NOT load) MUST reject !!python/object code-execution tags.

    Mitigation for T-03-02-01 per plan's threat register. The exact exception
    class varies across PyYAML minor versions; accept any YAMLError subclass.
    """
    fake = FakeGitHub(FakeRepoState())
    _seed_contents(
        fake,
        path=".github/merge-queue/path_to_queues.yml",
        ref="develop",
        payload="!!python/object/apply:os.system\n- whoami\n",
    )
    with pytest.raises(yaml.YAMLError):
        load_from_develop(fake, "owner", "repo")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# APP_SLUG_ENV / APP_ID_ENV — env-var-name constants
# ---------------------------------------------------------------------------


def test_app_slug_env_constant() -> None:
    """APP_SLUG_ENV pins the env-var NAME used by plan 03-06's handler."""
    assert APP_SLUG_ENV == "MQ_APP_SLUG"


def test_app_id_env_constant() -> None:
    """APP_ID_ENV pins the env-var NAME used by plan 03-06's handler."""
    assert APP_ID_ENV == "MQ_APP_ID"
