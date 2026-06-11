"""Sample Python module for golden snapshot testing."""
from __future__ import annotations

import os
from typing import Optional


class DataProcessor:
    """Processes raw data records."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._cache: dict[str, list] = {}

    def load(self, key: str) -> Optional[list]:
        """Load records for the given key."""
        if key in self._cache:
            return self._cache[key]
        return None

    def process(self, records: list) -> dict:
        """Transform records into a summary."""
        result: dict = {}
        for item in records:
            result[item["id"]] = item
        return result


def format_output(data: dict, indent: int = 2) -> str:
    """Render data as a formatted string."""
    import json
    return json.dumps(data, indent=indent)
