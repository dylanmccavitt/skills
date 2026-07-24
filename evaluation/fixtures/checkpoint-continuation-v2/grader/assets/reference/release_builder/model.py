"""Input normalization for release records."""

import re


ID_RE = re.compile(r"^[A-Z]+-[0-9]+$")
FIELDS = {"id", "component", "status", "notes"}
STATUSES = {"ready", "blocked"}


def normalize_records(value):
    if not isinstance(value, list) or not value:
        raise ValueError("records must be a non-empty array")
    normalized = []
    seen = set()
    for record in value:
        if not isinstance(record, dict) or set(record) != FIELDS:
            raise ValueError("record fields are invalid")
        item = {
            key: record[key].strip() if isinstance(record[key], str) else record[key]
            for key in FIELDS
        }
        if not all(isinstance(item[key], str) for key in FIELDS):
            raise ValueError("record values must be strings")
        if not ID_RE.fullmatch(item["id"]) or item["id"] in seen:
            raise ValueError("record ID is invalid or duplicated")
        if not item["component"] or item["status"] not in STATUSES:
            raise ValueError("record component or status is invalid")
        seen.add(item["id"])
        normalized.append(item)
    return sorted(normalized, key=lambda item: item["id"])
