"""Catch obvious accidental disclosure in agent-authored shared text."""

from __future__ import annotations

import re


class PrivacyError(ValueError):
    pass


PATTERNS = {
    "street address": re.compile(
        r"\b\d{1,6}\s+(?:[A-Za-z0-9.-]+\s+){0,4}"
        r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Drive|Dr\.?|Lane|Ln\.?|"
        r"Court|Ct\.?|Boulevard|Blvd\.?)\b", re.I
    ),
    "credential": re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*[^\s,;]{8,}"),
    "private key": re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|PRIVATE) PRIVATE KEY-----"),
}


def check_shared(text: str) -> None:
    for label, pattern in PATTERNS.items():
        if pattern.search(text):
            raise PrivacyError(f"Possible {label} in shared text; generalize before sharing")
