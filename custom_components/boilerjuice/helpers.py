"""Small shared helpers."""

from __future__ import annotations


def normalise_email(email: str) -> str:
    """Return an email in the form used as a config entry's unique id.

    BoilerJuice treats sign-in addresses case-insensitively, so "Me@Example.com"
    and "me@example.com" are one account and must not be addable twice.
    """
    return email.strip().lower()
