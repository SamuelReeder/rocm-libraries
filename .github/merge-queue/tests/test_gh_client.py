"""
tests/test_gh_client.py — Unit tests for rocm_mq.gh (IO-01).

Covers:
- GitHubClient(token=...) constructor + .rest passthrough.
- request_with_retry: retry on SecondaryRateLimitExceeded, exhaustion, non-rate-limit
  exception passthrough.
- resolve_app_identity: two-call startup pattern (apps.get_authenticated +
  users.get_by_username(slug+"[bot]")) returning AppIdentity with bot_user_id
  populated from the user lookup (OQ-1 / A1 resolution).
- CorruptSquashError is a RuntimeError subclass.

Mocks the underlying githubkit client to avoid network calls. Mock targets are at
the .rest.apps / .rest.users namespaces — these are passthroughs from githubkit.GitHub.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from githubkit.exception import RequestFailed, SecondaryRateLimitExceeded
from rocm_mq.gh import CorruptSquashError, GitHubClient, resolve_app_identity

from rocm_mq.state import AppIdentity

# ---------------------------------------------------------------------------
# Fake exception constructors (avoid the real httpx.Response dependency)
# ---------------------------------------------------------------------------


def _make_secondary_rate_limit_exc(retry_after_seconds: float = 0.0) -> SecondaryRateLimitExceeded:
    """Construct a SecondaryRateLimitExceeded with no real httpx.Response.

    We bypass __init__ because it expects a fully-formed httpx.Response.  The
    only attribute exercised by gh.py's retry loop is .retry_after.
    """
    exc = SecondaryRateLimitExceeded.__new__(SecondaryRateLimitExceeded)
    exc.retry_after = timedelta(seconds=retry_after_seconds)
    exc.response = SimpleNamespace(status_code=403)  # for str()/debug
    return exc


def _make_request_failed_exc(status_code: int = 500) -> RequestFailed:
    """Construct a RequestFailed without a real httpx.Response."""
    exc = RequestFailed.__new__(RequestFailed)
    exc.response = SimpleNamespace(status_code=status_code)
    return exc


# ---------------------------------------------------------------------------
# 1. Constructor + .rest property
# ---------------------------------------------------------------------------


def test_constructor_accepts_token() -> None:
    """GitHubClient(token=...) constructs without error and exposes .rest."""
    client = GitHubClient(token="ghs_test_token")
    assert client is not None
    assert client.rest is not None


def test_rest_property_forwards_to_githubkit() -> None:
    """client.rest forwards to the underlying githubkit.GitHub.rest namespace."""
    client = GitHubClient(token="ghs_test_token")
    # githubkit exposes .rest as a RestVersionSwitcher; we just assert it has the
    # well-known sub-namespaces.
    assert hasattr(client.rest, "apps")
    assert hasattr(client.rest, "users")


# ---------------------------------------------------------------------------
# 2. request_with_retry behaviour
# ---------------------------------------------------------------------------


def test_retry_on_secondary_rate_limit__succeeds_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First call raises SecondaryRateLimitExceeded; second call returns success."""
    # Avoid real sleep
    monkeypatch.setattr("rocm_mq.gh.time.sleep", lambda _: None)

    call_count = {"n": 0}

    def fn() -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _make_secondary_rate_limit_exc(retry_after_seconds=0)
        return "ok"

    client = GitHubClient(token="ghs_test_token")
    result = client.request_with_retry(fn)
    assert result == "ok"
    assert call_count["n"] == 2


def test_retry_exhausted_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """All max_retries=3 attempts raise SecondaryRateLimitExceeded -> propagate."""
    monkeypatch.setattr("rocm_mq.gh.time.sleep", lambda _: None)

    call_count = {"n": 0}

    def fn() -> str:
        call_count["n"] += 1
        raise _make_secondary_rate_limit_exc(retry_after_seconds=0)

    client = GitHubClient(token="ghs_test_token")
    with pytest.raises(SecondaryRateLimitExceeded):
        client.request_with_retry(fn, max_retries=3)
    assert call_count["n"] == 3


def test_retry_non_rate_limit_exception_propagates_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RequestFailed (not SecondaryRateLimitExceeded) propagates without retry."""
    monkeypatch.setattr("rocm_mq.gh.time.sleep", lambda _: None)

    call_count = {"n": 0}

    def fn() -> str:
        call_count["n"] += 1
        raise _make_request_failed_exc(status_code=500)

    client = GitHubClient(token="ghs_test_token")
    with pytest.raises(RequestFailed):
        client.request_with_retry(fn)
    assert call_count["n"] == 1  # no retry


def test_retry_forwards_args_and_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """request_with_retry passes *args and **kwargs through to fn."""
    monkeypatch.setattr("rocm_mq.gh.time.sleep", lambda _: None)

    captured: dict[str, Any] = {}

    def fn(a: int, *, b: str) -> tuple[int, str]:
        captured["a"] = a
        captured["b"] = b
        return (a, b)

    client = GitHubClient(token="ghs_test_token")
    result = client.request_with_retry(fn, 42, b="hello")
    assert result == (42, "hello")
    assert captured == {"a": 42, "b": "hello"}


# ---------------------------------------------------------------------------
# 3. resolve_app_identity
# ---------------------------------------------------------------------------


def _make_resolve_client(
    *, integration_id: int, integration_slug: str, bot_user_id: int
) -> tuple[GitHubClient, MagicMock]:
    """Construct a GitHubClient with mocked .rest.apps / .rest.users namespaces.

    Returns (client, users_mock) — the users_mock is exposed so tests can assert
    the username argument passed to get_by_username.
    """
    client = GitHubClient(token="ghs_test_token")

    apps_mock = MagicMock()
    apps_mock.get_authenticated.return_value = SimpleNamespace(
        parsed_data=SimpleNamespace(id=integration_id, slug=integration_slug)
    )
    users_mock = MagicMock()
    users_mock.get_by_username.return_value = SimpleNamespace(
        parsed_data=SimpleNamespace(id=bot_user_id)
    )

    # Replace the .rest namespace with a SimpleNamespace shim.
    rest_shim = SimpleNamespace(apps=apps_mock, users=users_mock)
    # Use object.__setattr__ in case .rest is a property — but GitHubClient
    # defines rest via a property, so we patch the underlying _gh.rest instead
    # for the test by patching the property directly on the instance.
    # Easier: patch with monkeypatch via direct attribute replacement on _gh.
    client._gh = SimpleNamespace(rest=rest_shim)  # type: ignore[attr-defined]
    return client, users_mock


def test_resolve_app_identity__returns_correct_fields() -> None:
    """resolve_app_identity wires apps.get_authenticated + users.get_by_username."""
    client, _users_mock = _make_resolve_client(
        integration_id=12345,
        integration_slug="rocm-mq",
        bot_user_id=99999,
    )
    identity = resolve_app_identity(client)
    assert identity == AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999)


def test_resolve_app_identity__bot_login_pattern() -> None:
    """resolve_app_identity must call users.get_by_username with f'{slug}[bot]'."""
    client, users_mock = _make_resolve_client(
        integration_id=12345,
        integration_slug="rocm-mq",
        bot_user_id=99999,
    )
    resolve_app_identity(client)
    # Confirm the bot login pattern (slug + '[bot]') was passed
    users_mock.get_by_username.assert_called_once()
    call_args = users_mock.get_by_username.call_args
    # The username may be passed positionally or as a keyword
    passed_username = call_args.args[0] if call_args.args else call_args.kwargs.get("username")
    assert passed_username == "rocm-mq[bot]"


def test_resolve_app_identity__handles_alternate_slug() -> None:
    """resolve_app_identity correctly uses the slug returned from the API."""
    client, users_mock = _make_resolve_client(
        integration_id=777,
        integration_slug="some-other-app",
        bot_user_id=8888,
    )
    identity = resolve_app_identity(client)
    assert identity == AppIdentity(slug="some-other-app", app_id=777, bot_user_id=8888)
    call_args = users_mock.get_by_username.call_args
    passed_username = call_args.args[0] if call_args.args else call_args.kwargs.get("username")
    assert passed_username == "some-other-app[bot]"


# ---------------------------------------------------------------------------
# 4. CorruptSquashError
# ---------------------------------------------------------------------------


def test_corrupt_squash_error_is_runtime_error() -> None:
    """CorruptSquashError is a RuntimeError subclass usable by executor.py."""
    err = CorruptSquashError("post-squash verification failed: wrong parent SHA")
    assert isinstance(err, RuntimeError)
    assert isinstance(err, CorruptSquashError)
    assert str(err) == "post-squash verification failed: wrong parent SHA"
