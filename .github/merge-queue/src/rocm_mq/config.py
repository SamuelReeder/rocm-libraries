"""
rocm_mq.config — Self-bootstrap protection path-set + path-to-queues loader.

Two public surfaces:

  1. ``SELF_BOOTSTRAP_PATHS`` — tuple of fnmatch-style glob patterns naming the
     paths that ``cmd_handle.py`` (plan 03-06) MUST reject ``/merge`` against
     per RFC §8 self-bootstrap protection. The handler intersects this set
     with the PR's changed-file list; any intersection triggers an explanatory
     rejection comment and skips all state mutations (no label apply, no
     status comment, no eyes reaction).

  2. ``load_from_develop(client, owner, repo)`` — minimal non-validating loader
     for ``.github/merge-queue/path_to_queues.yml`` from the ``develop`` ref via
     the GitHub Contents API + ``yaml.safe_load``. Schema validation is Phase 4
     territory (``mq-config-validate.yml`` in plan 04-XX); Phase 3 only needs
     the dict shape for the handler's path-intersection check and the
     processor's read-only consumption.

Plus two trivial env-var-NAME constants (``APP_SLUG_ENV``, ``APP_ID_ENV``)
that plan 03-06's handler reads from at runtime to pin the merge-queue App's
identity per RFC §4.3.1 (App-creator filter for the activation status, slug
pinning for the audit's self-trigger exemption). The slug LITERAL (recommended
``rocm-mq-fork`` per 03-RESEARCH.md Area #1) is registered manually in plan
03-05's App-registration task and supplied to the workflow via repo variables
— it is NOT hardcoded in this module.

Layering (PURE-09): this module is I/O layer (imports ``GitHubClient`` under
``TYPE_CHECKING`` for type annotation; runtime calls hit the live API via the
client passed in). It is NOT in ``PURE_LAYER_MODULES`` and freely uses
``base64``, ``yaml``, and the client's ``.rest.repos.get_content`` method.
The decision layer never imports from here directly — the handler / processor
load the dict at entry and pass dict slices into pure-layer functions.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any, Final

import yaml

if TYPE_CHECKING:
    from rocm_mq.gh import GitHubClient
    from rocm_mq.state import MergeQueueConfig


# ---------------------------------------------------------------------------
# Self-bootstrap protection path-set (CLAUDE.md "branch-protection-as-code
# integration" + RFC §8). Phase 3 handler intersects this with PR changed
# files; intersection → reject /merge with explanatory comment. Phase 4
# config-validator further locks this surface.
# ---------------------------------------------------------------------------

SELF_BOOTSTRAP_PATHS: Final[tuple[str, ...]] = (
    ".github/workflows/**",
    ".github/merge-queue/**",
    # Explicit entries even though covered by the ** globs above — keeping
    # them visible at this layer matches the CONTEXT.md `<code_context>`
    # Established Patterns enumeration and survives a future refactor that
    # narrows the ** globs (e.g., to a more specific subtree).
    ".github/merge-queue/path_to_queues.yml",
    ".github/workflows/mq-dogfood-canary.yml",
    # ---- Future slot: "terraform/github/**" — branch-protection-as-code (RFC §8). ----
    # When the org adopts an IaC tool to manage branch protection (most likely
    # the `integrations/github` Terraform provider per CLAUDE.md, possibly the
    # GitHub Rulesets API, or `.github/settings.yml` via the Settings probot),
    # add the relevant glob here. The file modifying THIS constant goes through
    # manual maintainer merge (RFC §8 self-protection on the protection list
    # itself — `.github/merge-queue/**` already covers this file).
    #
    # Document the chosen path in the PR description so future ops know which
    # IaC tool's surface is now part of the merge-queue's trust boundary.
)


# ---------------------------------------------------------------------------
# Env-var NAME constants — consumed by plan 03-06's handler / preflight for
# App identity pinning. The VALUES (slug literal, numeric app id) live in the
# workflow's `${{ vars.MQ_APP_SLUG }}` / `${{ vars.MQ_APP_ID }}` repo
# variables (or the `mq-secrets` Environment); this module only owns the
# NAMES so a future rename ripples through one source of truth.
# ---------------------------------------------------------------------------

APP_SLUG_ENV: Final[str] = "MQ_APP_SLUG"
APP_ID_ENV: Final[str] = "MQ_APP_ID"


# ---------------------------------------------------------------------------
# Path-to-queues loader (RFC §4.8 — read from develop ref via Contents API)
# ---------------------------------------------------------------------------


_PATH_TO_QUEUES_FILE: Final[str] = ".github/merge-queue/path_to_queues.yml"
_DEVELOP_REF: Final[str] = "develop"


def load_from_develop(client: GitHubClient, owner: str, repo: str) -> dict[str, Any]:
    """Read and parse the path-to-queues config from the develop ref.

    Hits ``GET /repos/{owner}/{repo}/contents/.github/merge-queue/path_to_queues.yml?ref=develop``
    via the supplied client, base64-decodes the response body, and parses with
    ``yaml.safe_load``.

    ``safe_load`` (NOT ``load``) is non-negotiable per CLAUDE.md "What NOT to
    Use" + the plan's T-03-02-01 threat-register mitigation: it blocks
    ``!!python/object`` and similar code-execution constructs that would let
    a poisoned ``path_to_queues.yml`` trigger arbitrary Python execution
    inside the workflow runner (which holds the App installation token).

    No schema validation here — Phase 4's ``mq-config-validate.yml`` job and
    the RFC §4.8 validator (every queue name appearing in any list also
    appears as its own path entry; every upstream lists every downstream)
    own the structural checks. This loader trusts the YAML shape and returns
    whatever ``safe_load`` produces (typically a ``dict``; could be ``None``
    or a scalar if the file is malformed — callers handle that).

    Args:
        client: A ``GitHubClient`` (or any object exposing ``.rest.repos.get_content``
            with the same shape). The fake in ``tests/gh_fake.py`` is the
            unit-test substitute; the real client comes from ``rocm_mq.gh``.
        owner: Repo owner (e.g., ``"SamuelReeder"``).
        repo: Repo name (e.g., ``"rocm-libraries"``).

    Returns:
        Parsed YAML payload (typically a ``dict[str, Any]`` shaped like the
        RFC §4.8 example). May be ``None`` or a non-dict if the file is
        malformed; callers in plans 03-04 / 03-06 / 04-XX validate further.

    Raises:
        yaml.YAMLError: If the payload contains tags ``safe_load`` rejects
            (e.g., ``!!python/object``) or is otherwise un-parseable YAML.
        Any ``RequestFailed`` / network error the client may raise on the
            Contents API call (handler / processor catch at their level).
    """
    resp = client.rest.repos.get_content(
        owner, repo, _PATH_TO_QUEUES_FILE, ref=_DEVELOP_REF
    )
    raw = base64.b64decode(resp.parsed_data.content).decode("utf-8")
    parsed: dict[str, Any] = yaml.safe_load(raw)
    return parsed


# ---------------------------------------------------------------------------
# build_config_from_develop — YAML → MergeQueueConfig bridge
# ---------------------------------------------------------------------------
#
# Shared between cmd_process.run_process_cycle (processor) and
# cmd_handle._load_config (handler). Extracted here so the two entry points
# stay in lockstep on the YAML schema interpretation; a future schema change
# (Phase 4 validator) ripples through one source.


def build_config_from_develop(
    client: GitHubClient, owner: str, repo: str
) -> MergeQueueConfig:
    """Load path_to_queues.yml from develop + resolve App identity →
    MergeQueueConfig.

    Three steps:
      1. ``load_from_develop`` reads + parses the YAML (Contents API + safe_load).
      2. Translate the YAML's ``queues:`` list into ``all_queues`` and the
         ``paths:`` list-of-mappings into ``path_to_queues`` (longest-prefix-first
         per ``pathmap.queues_for_paths`` contract).
      3. ``resolve_app_identity(client)`` (env-var-trust path post-03-wr-01).

    Returns a ``MergeQueueConfig`` ready for ``process_cycle`` / handler
    consumption.

    Raises:
        ValueError: If the YAML root is not a mapping (bare list, scalar, None).
            Schema-graph validation (every queue named in a paths entry must
            exist in queues:, etc.) is Phase 4 territory.
    """
    # Late imports to keep config.py's import surface small and to avoid a
    # PURE-09 violation (state.MergeQueueConfig is in the pure layer; we
    # construct it here at the I/O boundary).
    from rocm_mq.gh import resolve_app_identity
    from rocm_mq.state import MergeQueueConfig

    raw = load_from_develop(client, owner, repo)
    if not isinstance(raw, dict):
        msg = (
            f"path_to_queues.yml at develop is not a mapping "
            f"(got {type(raw).__name__})"
        )
        raise ValueError(msg)

    queues = tuple(raw.get("queues", ()) or ())

    path_entries: list[tuple[str, frozenset[str]]] = []
    for entry in raw.get("paths", []) or []:
        path = str(entry["path"])
        path_queues = frozenset(entry.get("queues", ()) or ())
        path_entries.append((path, path_queues))
    # Longest-prefix-first per pathmap.queues_for_paths contract.
    path_entries.sort(key=lambda pair: len(pair[0]), reverse=True)

    app_identity = resolve_app_identity(client)

    # MQ_REQUIRE_APPROVAL env-var override (PORT-02 closure). Default True so
    # upstream port inherits the RFC §5 contract; the fork-dogfood workflow
    # sets MQ_REQUIRE_APPROVAL=0 (or false/no) to disable the gate. Any other
    # value (including unset) keeps the default. Parsed here at the I/O
    # boundary so the pure decision layer never reads the environment.
    import os

    require_approval = True
    raw_val = os.environ.get("MQ_REQUIRE_APPROVAL", "").strip().lower()
    if raw_val in {"0", "false", "no", "off"}:
        require_approval = False

    return MergeQueueConfig(
        all_queues=queues,
        path_to_queues=tuple(path_entries),
        app_identity=app_identity,
        require_approval_at_enqueue=require_approval,
    )
