"""
rocm_mq.gh — Thin synchronous wrapper around githubkit.GitHub (IO-01).

PURE-09 compliance statement: this module is NOT in PURE_LAYER_MODULES; it
intentionally imports githubkit. It is the ONLY approved site for
``GitHub(token=...)`` construction in the rocm_mq package. All other Phase 2+
modules (snapshot.py, executor.py, cmd_process.py) consume this module's
``GitHubClient`` and never touch githubkit directly.

Public surface (Phase 2 contract):
- ``GitHubClient(token: str)`` — sync wrapper exposing ``.rest`` (a
  ``_RetryProxy`` over the githubkit rest switcher; every method call
  automatically goes through ``request_with_retry``, WR-01) +
  ``request_with_retry`` for callers needing explicit control.
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

import os
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


class _RetryProxy:
    """Wraps a githubkit namespace so every callable goes through retry (WR-01).

    Attribute lookup on the wrapped object returns:
    - For callables (the actual API methods like ``repos.merge``,
      ``pulls.get``): a function that runs the call through
      ``GitHubClient.request_with_retry`` so transient
      ``SecondaryRateLimitExceeded`` is absorbed transparently.
    - For non-callables (sub-namespaces like ``repos``, ``pulls``,
      ``issues``): another ``_RetryProxy`` wrapping the sub-namespace,
      so chained access (``client.rest.repos.merge(...)``) is also
      retry-wrapped.

    This lets callers write plain method calls
    (``client.rest.pulls.get(owner, repo, n)``) without sprinkling
    ``request_with_retry`` lambdas at every site. The previous
    ``request_with_retry(fn, *args, **kwargs)`` method is kept for direct
    use, but callers that go through ``client.rest`` get retry for free.

    The proxy does NOT intercept attribute assignment: tests that drive
    the fake (``FakeGitHub``) never see this proxy because they replace
    ``client`` entirely with the fake. The proxy only sits on top of the
    real githubkit client.
    """

    def __init__(self, wrapped: Any, retry_call: Callable[..., Any]) -> None:
        # Use object.__setattr__ to avoid triggering __setattr__ recursion if
        # we ever add custom behaviour there.
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_retry_call", retry_call)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._wrapped, name)
        # Always wrap in another proxy. The proxy is also callable
        # (see __call__) so leaf method invocation goes through
        # request_with_retry, and chained access (.repos.merge) still
        # works because each intermediate proxy is itself a _RetryProxy.
        # This deliberately handles both the production case (githubkit
        # bound methods and namespace classes) and the test case
        # (MagicMock / SimpleNamespace stand-ins where the type
        # discrimination between namespace vs method is unavailable).
        return _RetryProxy(attr, self._retry_call)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Leaf-method invocation: run the wrapped callable through the
        # retry helper. If the wrapped object is not callable, the
        # underlying call below will raise TypeError naturally — matching
        # the behaviour the caller would have seen against the raw
        # githubkit object.
        return self._retry_call(self._wrapped, *args, **kwargs)


class GitHubClient:
    """Sync wrapper around githubkit.GitHub.

    Construction takes a pre-minted installation token (typically from
    ``actions/create-github-app-token@v3`` in the GHA workflow). No App private
    key handling — that surface stays in the GHA action per CLAUDE.md.

    Exposes ``.rest`` as a passthrough to ``githubkit.GitHub.rest`` WRAPPED in
    a ``_RetryProxy`` so every API method call is automatically run through
    ``request_with_retry`` (WR-01). ``request_with_retry`` is still exposed for
    callers that need explicit control (e.g., to override ``max_retries``).
    """

    def __init__(self, token: str) -> None:
        # githubkit's ``GitHub(auth=token)`` accepts a bearer-token string
        # directly. The result is a fully-configured sync client.
        self._gh = GitHub(auth=token)

    @property
    def rest(self) -> Any:
        """Forward to ``githubkit.GitHub.rest`` wrapped in ``_RetryProxy``.

        Typed as ``Any`` to avoid pulling githubkit's RestVersionSwitcher type
        into the static surface — Phase 2 callers use the namespaces directly
        (e.g., ``client.rest.apps.get_authenticated()``) and githubkit's own
        typed responses cover the per-call return shapes.

        The returned object is a fresh ``_RetryProxy`` over the current
        ``self._gh.rest``; building per-access (rather than caching in
        ``__init__``) keeps tests that swap ``self._gh`` post-construction
        working transparently (test_gh_client._make_resolve_client).
        """
        return _RetryProxy(self._gh.rest, self.request_with_retry)

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

        WR-01: the ``_RetryProxy`` on ``self.rest`` calls this method
        automatically for every API method, so callers normally do not need
        to invoke it directly.
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

    Two source modes:

    1. **Env-var-trust path (runtime / production)** — when BOTH ``MQ_APP_SLUG``
       and ``MQ_APP_ID`` are set, trust them. The workflow gets the slug from
       ``actions/create-github-app-token@v3``'s ``app-slug`` output (the action
       has JWT-attested it), and the numeric ``MQ_APP_ID`` from a repo variable
       set during plan 03-05's operator setup. ``bot_user_id`` still comes from
       a live ``users.get_by_username`` lookup (works with installation tokens,
       fails closed if the slug is wrong because the bot login won't resolve).

       This path is required because the installation token minted by the
       action **cannot** call ``/app`` endpoints — those require App JWT
       authentication. Calling ``apps.get_authenticated`` from the runtime
       client returns HTTP 401 (live-fork dispatch run 26173804944 on
       2026-05-20 confirmed this).

    2. **JWT fallback (local-dev)** — when env vars are absent OR malformed,
       fall back to the original ``apps.get_authenticated`` + ``users.get_by_username``
       two-call pattern. Preserves the local-dev workflow where the operator
       holds the App private key and has minted a JWT directly.

    Half-trust is not trust: if only one of the two env vars is set, fall back
    rather than mixing trust sources. ``MQ_APP_ID`` that fails to parse as int
    raises ``ValueError`` immediately rather than silently falling back —
    misconfiguration must fail loud.

    Returns a frozen ``AppIdentity`` with all three fields populated. Intended
    to be called ONCE at processor startup; the result is then plumbed through
    ``MergeQueueConfig`` for the rest of the cycle.

    Not wrapped in ``request_with_retry``: this is startup, runs at most once
    per processor invocation, and a failure here should fail the whole cycle
    rather than mask a misconfigured token.
    """
    slug_env = os.environ.get("MQ_APP_SLUG")
    app_id_env = os.environ.get("MQ_APP_ID")

    if slug_env and app_id_env:
        try:
            app_id = int(app_id_env)
        except ValueError as exc:
            msg = (
                f"MQ_APP_ID env var must parse as int, got {app_id_env!r}. "
                "Check the repo variable set during plan 03-05."
            )
            raise ValueError(msg) from exc
        slug = slug_env
    else:
        # JWT fallback: local-dev path where the client has App JWT auth.
        # The installation-token path (runtime workflow) MUST go through the
        # env-var branch above; this branch hits /app which returns 401 with
        # an installation token.
        integration_resp = client.rest.apps.get_authenticated()
        integration = integration_resp.parsed_data
        app_id = int(integration.id)
        slug = str(integration.slug)

    bot_login = f"{slug}[bot]"
    user_resp = client.rest.users.get_by_username(bot_login)
    user = user_resp.parsed_data
    bot_user_id: int = int(user.id)

    return AppIdentity(slug=slug, app_id=app_id, bot_user_id=bot_user_id)
