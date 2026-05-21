"""Unit + property tests for ``parse_gh_timestamp``.

``parse_gh_timestamp`` parses ISO 8601 GitHub timestamps into tz-aware
datetimes and raises ``NaiveDatetimeError`` for naive inputs. This is the
single chokepoint for parsing untrusted timestamps into the pure decision
layer; without it, a naive datetime would silently sort earlier than every
tz-aware datetime and corrupt FIFO ordering.

Tests:
1. Round-trip property: tz-aware datetime → isoformat → parse → same datetime.
2. Parametrized unit tests for GitHub-shaped strings (Z, +00:00, microseconds).
3. Negative: naive strings raise NaiveDatetimeError.
4. Property: arbitrary text that parses as naive raises ValueError (or NaiveDatetimeError).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rocm_mq._helpers import NaiveDatetimeError, parse_gh_timestamp

# ---------------------------------------------------------------------------
# Round-trip property test
# ---------------------------------------------------------------------------


@given(st.datetimes(timezones=st.just(UTC)))
def test_parse_gh_timestamp_round_trip(dt: datetime) -> None:
    """parse_gh_timestamp(dt.isoformat()) == dt for any tz-aware UTC datetime."""
    parsed = parse_gh_timestamp(dt.isoformat())
    assert parsed == dt
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# Parametrized unit tests for GitHub-shaped timestamp strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ts_string", "expected"),
    [
        # Trailing Z (Python 3.11+ handles natively)
        (
            "2026-04-22T15:23:45Z",
            datetime(2026, 4, 22, 15, 23, 45, tzinfo=UTC),
        ),
        # Explicit +00:00 offset
        (
            "2026-04-22T15:23:45+00:00",
            datetime(2026, 4, 22, 15, 23, 45, tzinfo=UTC),
        ),
        # Microseconds with trailing Z
        (
            "2026-04-22T15:23:45.123456Z",
            datetime(2026, 4, 22, 15, 23, 45, 123456, tzinfo=UTC),
        ),
        # Microseconds with +00:00 offset
        (
            "2026-04-22T15:23:45.000001+00:00",
            datetime(2026, 4, 22, 15, 23, 45, 1, tzinfo=UTC),
        ),
    ],
    ids=["trailing_Z", "plus_00_00", "microseconds_Z", "microseconds_plus_00"],
)
def test_parse_gh_timestamp_github_shapes(ts_string: str, expected: datetime) -> None:
    """Parse GitHub-returned timestamp strings into correct tz-aware datetimes."""
    result = parse_gh_timestamp(ts_string)
    assert result == expected
    assert result.tzinfo is not None


# ---------------------------------------------------------------------------
# Negative tests: naive strings raise NaiveDatetimeError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "naive_string",
    [
        "2026-04-22T15:23:45",
        "2026-04-22T15:23:45.123",
        "2026-04-22T15:23:45.123456",
        "2026-01-01T00:00:00",
    ],
    ids=["no_tz_basic", "no_tz_ms3", "no_tz_ms6", "no_tz_midnight"],
)
def test_parse_gh_timestamp_naive_raises(naive_string: str) -> None:
    """Naive timestamp strings (no tz suffix) raise NaiveDatetimeError."""
    with pytest.raises(NaiveDatetimeError):
        parse_gh_timestamp(naive_string)


def test_naive_datetime_error_is_value_error() -> None:
    """NaiveDatetimeError is a subclass of ValueError (as documented)."""
    assert issubclass(NaiveDatetimeError, ValueError)


def test_naive_datetime_error_is_catchable_as_value_error() -> None:
    """NaiveDatetimeError can be caught as ValueError at I/O boundary."""
    with pytest.raises(ValueError):
        parse_gh_timestamp("2026-04-22T15:23:45")


# ---------------------------------------------------------------------------
# Negative property: garbage input raises ValueError (or NaiveDatetimeError)
# ---------------------------------------------------------------------------

# Build a strategy for strings that are not valid ISO 8601 (or not parseable by fromisoformat)
_NOT_ISO_STRATEGY = st.text(min_size=0, max_size=50).filter(
    lambda s: not _is_valid_iso(s)
)


def _is_valid_iso(s: str) -> bool:
    """Return True if datetime.fromisoformat can parse the string."""
    try:
        datetime.fromisoformat(s)
    except ValueError:
        return False
    else:
        return True


@given(_NOT_ISO_STRATEGY)
def test_parse_gh_timestamp_garbage_raises_value_error(s: str) -> None:
    """Non-ISO strings raise ValueError (or NaiveDatetimeError, a subclass)."""
    with pytest.raises(ValueError):
        parse_gh_timestamp(s)


# ---------------------------------------------------------------------------
# Additional correctness tests
# ---------------------------------------------------------------------------


def test_parse_gh_timestamp_returns_utc_tzinfo() -> None:
    """Parsed Z-suffix datetimes have UTC tzinfo."""
    result = parse_gh_timestamp("2026-04-22T15:23:45Z")
    assert result.tzinfo is not None
    # Should be UTC (offset = 0)
    assert result.utcoffset() is not None
    from datetime import timedelta

    assert result.utcoffset() == timedelta(0)


def test_parse_gh_timestamp_z_equals_plus_00_00() -> None:
    """Timestamps with Z and +00:00 are equal after parsing."""
    ts_z = parse_gh_timestamp("2026-04-22T15:23:45Z")
    ts_offset = parse_gh_timestamp("2026-04-22T15:23:45+00:00")
    assert ts_z == ts_offset
