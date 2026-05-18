"""
rocm_mq._helpers — Single-chokepoint validators for the pure decision layer.

This module is the ONLY place in ``rocm_mq`` that calls ``datetime.fromisoformat``.
All other pure-layer modules receive already-parsed, tz-aware datetimes.

It is the ONLY place that performs App-identity checks. No login-equality check
(``actor.login == "rocm-mq[bot]"``) is permitted anywhere else in ``rocm_mq``
(PURE-09 CI lint enforces this via ``test_pure_layer_imports.py``).

Pitfall 2 (impersonation): ``is_app_identity`` uses AND of three conditions —
  type == "Bot" AND app_slug == expected.slug AND app_id == expected.app_id.
  The OR of any two is impersonable. All three together are load-bearing.

Pitfall 3 (naive-datetime FIFO corruption): ``parse_gh_timestamp`` raises
  ``NaiveDatetimeError`` when GitHub returns a timestamp without timezone info.
  This prevents silent FIFO ordering corruption in ``decide_cycle``.
"""

from __future__ import annotations

from datetime import datetime

from rocm_mq.state import AppIdentity, CommitStatusCreator, TimelineActor


class NaiveDatetimeError(ValueError):
    """A timestamp parsed without tzinfo — refuse to use in FIFO comparisons."""


def parse_gh_timestamp(s: str) -> datetime:
    """Parse a GitHub ISO-8601 timestamp string into a tz-aware datetime.

    Handles the following GitHub-returned formats:
    - ``"2026-04-22T15:23:45Z"``          (Python 3.11+ handles trailing Z natively)
    - ``"2026-04-22T15:23:45+00:00"``
    - ``"2026-04-22T15:23:45.123456Z"``   (microseconds)

    Raises:
        NaiveDatetimeError: If the parsed datetime has no tzinfo (naive).
        ValueError: If the string is not a valid ISO 8601 timestamp
                    (propagated from ``datetime.fromisoformat``).
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise NaiveDatetimeError(f"naive datetime from GitHub timestamp: {s!r}")
    return dt


def is_app_identity(actor: CommitStatusCreator, expected: AppIdentity) -> bool:
    """Return True only when ``actor`` is the canonical merge-queue GitHub App.

    Triple-check (Pitfall 2 + RFC §4.3.1 "Identity-check robustness"):
    1. ``actor.type == "Bot"`` — cheap pre-filter; rejects every User account.
    2. ``actor.app_slug == expected.slug`` — catches ``app_id`` typos in config.
    3. ``actor.app_id == expected.app_id`` — immutable across slug renames;
       the load-bearing field.

    The AND of all three is required. Any OR-subset is impersonable:
    - Wrong slug but matching ID → a different App (possible across orgs).
    - Matching slug but wrong ID → a renamed version of another App.
    - ``type == "Bot"`` alone → any App or GitHub Actions sibling workflow.
    """
    return (
        actor.type == "Bot"
        and actor.app_slug == expected.slug
        and actor.app_id == expected.app_id
    )


def is_app_identity_actor(actor: TimelineActor, expected: AppIdentity) -> bool:
    """Return True when a timeline ``actor`` is the canonical merge-queue App's bot user.

    Timeline label events lack the ``app`` sub-object (only ``login``, ``type``,
    ``user_id`` are available). We check ``bot_user_id`` instead of ``app_id``
    (the App's installed-bot user has a stable numeric user ID distinct from app_id).

    Raises no exceptions — a non-matching actor simply returns ``False``.
    """
    return actor.type == "Bot" and actor.user_id == expected.bot_user_id
