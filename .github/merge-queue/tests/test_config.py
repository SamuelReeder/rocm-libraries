"""Unit tests for rocm_mq.config — self-bootstrap globs, RFC §4.8 YAML loader,
and App-identity env-var names.

Covers:
- SELF_BOOTSTRAP_PATHS: type (tuple of str), membership of the required
  globs (RFC §8 self-bootstrap protection), and the documented future-slot
  comment for ``terraform/github/**``.
- load_from_develop: happy path (Contents API + base64 + safe_load returns
  dict), develop-ref pinning, and safe_load enforcement against
  ``!!python/object`` tags (YAML deserialization-RCE defence).
- APP_SLUG_ENV / APP_ID_ENV: literal env-var-name constants for App identity
  pinning.
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
    ],
)
def test_self_bootstrap_paths_contains_required_globs(required_glob: str) -> None:
    """RFC §8 self-bootstrap protection: required globs MUST be present today."""
    assert required_glob in SELF_BOOTSTRAP_PATHS


def test_self_bootstrap_paths_documents_future_slot() -> None:
    """The source file must mention 'terraform' in a future-slot comment.

    Branch-protection-as-code integration (RFC §8): the empty slot for
    ``terraform/github/**`` (or equivalent IaC tool) is documented in
    a source comment so the next person to add Terraform / Probot-Settings
    knows where to extend the constant.
    """
    source_path = inspect.getsourcefile(config_module)
    assert source_path is not None
    source = Path(source_path).read_text(encoding="utf-8")
    assert "terraform" in source, (
        "config.py must document the future terraform/github/** slot for "
        "branch-protection-as-code integration"
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

    YAML deserialization-RCE defence: ``yaml.load`` with the default loader
    will execute arbitrary Python from a ``!!python/object/apply:`` tag.
    ``safe_load`` raises instead. The exact exception class varies across
    PyYAML minor versions; accept any YAMLError subclass.
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
    """APP_SLUG_ENV pins the env-var name read by the handler for App identity."""
    assert APP_SLUG_ENV == "MQ_APP_SLUG"


def test_app_id_env_constant() -> None:
    """APP_ID_ENV pins the env-var name read by the handler for App identity."""
    assert APP_ID_ENV == "MQ_APP_ID"


# ---------------------------------------------------------------------------
# build_config_from_develop — shared YAML → MergeQueueConfig bridge
# ---------------------------------------------------------------------------


def test_build_config_from_develop__parses_six_queue_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-shape path_to_queues.yml maps to a MergeQueueConfig."""
    from rocm_mq.config import build_config_from_develop
    from rocm_mq.state import AppIdentity

    monkeypatch.setenv("MQ_APP_SLUG", "merge-clanker")
    monkeypatch.setenv("MQ_APP_ID", "3776213")

    fake = FakeGitHub(FakeRepoState())
    yaml_payload = """\
queues:
  - hipdnn
  - miopen-provider
  - hipblaslt-provider
  - hip-kernel-provider
  - fusilli-provider
  - integration-tests
paths:
  - path: projects/hipdnn/
    queues: [hipdnn]
  - path: dnn-providers/miopen-provider/
    queues: [miopen-provider]
  - path: dnn-providers/hipblaslt-provider/
    queues: [hipblaslt-provider]
  - path: dnn-providers/hip-kernel-provider/
    queues: [hip-kernel-provider]
  - path: dnn-providers/fusilli-provider/
    queues: [fusilli-provider]
  - path: dnn-providers/integration-tests/
    queues: [integration-tests]
"""
    _seed_contents(
        fake, path=".github/merge-queue/path_to_queues.yml",
        ref="develop", payload=yaml_payload,
    )

    # Stub resolve_app_identity's bot_user_id lookup via users.get_by_username
    from types import SimpleNamespace
    users_mock_resp = SimpleNamespace(parsed_data=SimpleNamespace(id=999))
    fake.rest.users.get_by_username = lambda *a, **kw: users_mock_resp  # type: ignore[attr-defined]

    config = build_config_from_develop(fake, "owner", "repo")  # type: ignore[arg-type]

    assert set(config.all_queues) == {
        "hipdnn", "miopen-provider", "hipblaslt-provider",
        "hip-kernel-provider", "fusilli-provider", "integration-tests",
    }
    assert len(config.path_to_queues) == 6
    # Longest-prefix-first sort: dnn-providers/hip-kernel-provider/ (38 chars)
    # comes before projects/hipdnn/ (17 chars).
    path_strings = [p for p, _ in config.path_to_queues]
    assert all(
        len(path_strings[i]) >= len(path_strings[i + 1])
        for i in range(len(path_strings) - 1)
    ), f"path_to_queues not sorted longest-first: {path_strings}"
    # App identity wired through
    assert config.app_identity == AppIdentity(
        slug="merge-clanker", app_id=3776213, bot_user_id=999
    )


def test_build_config_from_develop__rejects_non_mapping_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare list / scalar / None payloads must raise ValueError loud."""
    from rocm_mq.config import build_config_from_develop

    monkeypatch.setenv("MQ_APP_SLUG", "merge-clanker")
    monkeypatch.setenv("MQ_APP_ID", "3776213")

    fake = FakeGitHub(FakeRepoState())
    _seed_contents(
        fake, path=".github/merge-queue/path_to_queues.yml",
        ref="develop", payload="- just-a-list\n- not-a-mapping\n",
    )
    from types import SimpleNamespace
    fake.rest.users.get_by_username = lambda *a, **kw: SimpleNamespace(  # type: ignore[attr-defined]
        parsed_data=SimpleNamespace(id=1)
    )

    with pytest.raises(ValueError, match="not a mapping"):
        build_config_from_develop(fake, "owner", "repo")  # type: ignore[arg-type]


def test_build_config_from_develop__empty_queues_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing queues / paths sections produce empty tuples (not crashes)."""
    from rocm_mq.config import build_config_from_develop

    monkeypatch.setenv("MQ_APP_SLUG", "merge-clanker")
    monkeypatch.setenv("MQ_APP_ID", "3776213")

    fake = FakeGitHub(FakeRepoState())
    _seed_contents(
        fake, path=".github/merge-queue/path_to_queues.yml",
        ref="develop", payload="# empty config\n{}\n",
    )
    from types import SimpleNamespace
    fake.rest.users.get_by_username = lambda *a, **kw: SimpleNamespace(  # type: ignore[attr-defined]
        parsed_data=SimpleNamespace(id=1)
    )

    config = build_config_from_develop(fake, "owner", "repo")  # type: ignore[arg-type]
    assert config.all_queues == ()
    assert config.path_to_queues == ()
