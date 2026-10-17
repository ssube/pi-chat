"""Escaping for Prometheus text exposition label values."""
from __future__ import annotations


def label_value(value: object) -> str:
    """Escape a label value according to the Prometheus text format."""
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
