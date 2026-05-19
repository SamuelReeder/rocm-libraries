"""
rocm_mq.gh — Thin synchronous wrapper around githubkit.GitHub (IO-01).

PURE-09 compliance statement: this module is NOT in PURE_LAYER_MODULES; it
intentionally imports githubkit. It is the ONLY approved site for
``GitHub(token=...)`` construction in the rocm_mq package. All other Phase 2+
modules (snapshot.py, executor.py, cmd_process.py) consume this module's
``GitHubClient`` and never touch githubkit directly.

Public surface (Phase 2 contract):
- ``GitHubClient(token: str)`` — sync wrapper exposing ``.rest`` passthrough +
  ``request_with_retry`` for SecondaryRateLimitExceeded handling.
- ``resolve_app_identity(client) -> AppIdentity`` — startup call that resolves
  ``bot_user_id`` via the two-step ``apps.get_authenticated`` +
  ``users.get_by_username(slug + "[bot]")`` pattern. Addresses OQ-1 / A1 from
  RESEARCH.md: the Integration model returned by ``apps.get_authenticated`` does
  not carry the bot user id directly; the separate user lookup is required.
- ``CorruptSquashError`` — raised by the Phase 2 executor when post-squash
  parent-SHA verification fails. Lives here so executor.py + tests can import
  it from a single, stable location.

Retry policy:
- Only ``SecondaryRateLimitExceeded`` triggers retry; primary rate limits and
  other ``RequestFailed`` subclasses propagate immediately so the caller cycle
  aborts cleanly (RFC §4.6 stateless processor — next cycle catches up).
- ``max_retries=3`` cap; sleep between attempts uses
  ``e.retry_after.total_seconds()`` (githubkit populates this from the
  ``Retry-After`` header).
- Tests monkeypatch ``rocm_mq.gh.time.sleep`` to avoid real waits.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from githubkit import GitHub
from githubkit.exception import SecondaryRateLimitExceeded

from rocm_mq.state import AppIdentity

if TYPE_CHECKING:
    pass

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Custom exception — owned by gh.py so executor.py can import from one place
# ---------------------------------------------------------------------------


class CorruptSquashError(RuntimeError):
    """Post-squash verification failed — squash commit has wrong parent SHA."""


# ---------------------------------------------------------------------------
# GitHubClient — thin sync wrapper
# ---------------------------------------------------------------------------


class GitHubClient:
    """Sync wrapper around githubkit.GitHub.

    Construction takes a pre-minted installation token (typically from
    ``actions/create-github-app-token@v3`` in the GHA workflow). No App private
    key handling — that surface stays in the GHA action per CLAUDE.md.

    Exposes ``.rest`` as a passthrough to ``githubkit.GitHub.rest`` and
    ``.request_with_retry(fn, *args, **kwargs)`` for callers that need
    SecondaryRateLimitExceeded retry semantics.
    """

    def __init__(self, token: str) -> None:
        # githubkit's ``GitHub(auth=token)`` accepts a bearer-token string
        # directly. The result is a fully-configured sync client.
        self._gh = GitHub(auth=token)

    @property
    def rest(self) -> Any:
        """Forward to ``githubkit.GitHub.rest``.

        Typed as ``Any`` to avoid pulling githubkit's RestVersionSwitcher type
        into the static surface — Phase 2 callers use the namespaces directly
        (e.g., ``client.rest.apps.get_authenticated()``) and githubkit's own
        typed responses cover the per-call return shapes.
        """
        return self._gh.rest

    def request_with_retry(
        self,
        fn: Callable[..., T],
        *args: Any,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> T:
        """Call ``fn(*args, **kwargs)`` with retry on SecondaryRateLimitExceeded.

        Retry policy:
        - On ``SecondaryRateLimitExceeded``: sleep ``e.retry_after.total_seconds()``
          then retry (up to ``max_retries`` total attempts).
        - On the final attempt (or any other exception): re-raise immediately.
        - Non-``SecondaryRateLimitExceeded`` exceptions propagate without retry.

        Tests patch ``time.sleep`` (via ``monkeypatch.setattr("rocm_mq.gh.time.sleep",
        ...)``) to avoid real waits. Production code receives real sleeps.
        """
        last_exc: SecondaryRateLimitExceeded | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except SecondaryRateLimitExceeded as exc:
                last_exc = exc
                if attempt == max_retries:
                    raise
                time.sleep(exc.retry_after.total_seconds())
        # Unreachable: the loop always either returns or raises on the final
        # attempt. The explicit raise here is a defensive guard so mypy and
        # readers do not have to reason about an implicit ``None`` return.
        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# resolve_app_identity — startup-only call (OQ-1 / A1 resolution)
# ---------------------------------------------------------------------------


def resolve_app_identity(client: GitHubClient) -> AppIdentity:
    """Resolve the merge-queue App's canonical identity at startup.

    Two-call pattern (RESEARCH.md OQ-1 / A1):
    1. ``apps.get_authenticated()`` returns an ``Integration`` model carrying
       ``.id`` (the App ID) and ``.slug``.
    2. The Integration model does NOT carry the bot user id. We synthesize the
       bot login as ``f"{slug}[bot]"`` and call ``users.get_by_username(...)``
       to fetch the bot's stable numeric user id (needed by
       ``is_app_identity_actor`` for timeline-event identity checks).

    Returns a frozen ``AppIdentity`` with all three fields populated. Intended
    to be called ONCE at processor startup; the result is then plumbed through
    ``MergeQueueConfig`` for the rest of the cycle.

    Not wrapped in ``request_with_retry``: this is startup, runs at most once
    per processor invocation, and a failure here should fail the whole cycle
    rather than mask a misconfigured token.
    """
    integration_resp = client.rest.apps.get_authenticated()
    integration = integration_resp.parsed_data
    app_id: int = int(integration.id)
    slug: str = str(integration.slug)

    bot_login = f"{slug}[bot]"
    user_resp = client.rest.users.get_by_username(bot_login)
    user = user_resp.parsed_data
    bot_user_id: int = int(user.id)

    return AppIdentity(slug=slug, app_id=app_id, bot_user_id=bot_user_id)
