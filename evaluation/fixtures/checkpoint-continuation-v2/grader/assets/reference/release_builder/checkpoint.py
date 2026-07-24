"""Persisted handoff state for the release builder."""

import hashlib
import json
import os
from pathlib import Path

from .model import normalize_records


def canonical_bytes(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def records_digest(records):
    return "sha256:" + hashlib.sha256(canonical_bytes(records)).hexdigest()


def _write_atomic(path, content):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def prepare(input_path, checkpoint_path):
    records = normalize_records(json.loads(Path(input_path).read_text(encoding="utf-8")))
    summary = {
        "blocked": sum(item["status"] == "blocked" for item in records),
        "ready": sum(item["status"] == "ready" for item in records),
    }
    checkpoint = {
        "records": records,
        "records_sha256": records_digest(records),
        "schema_version": 1,
        "summary": summary,
    }
    _write_atomic(checkpoint_path, canonical_bytes(checkpoint))
    return checkpoint


def load_checkpoint(checkpoint_path):
    value = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "records",
        "records_sha256",
        "schema_version",
        "summary",
    }:
        raise ValueError("checkpoint shape is invalid")
    if value["schema_version"] != 1:
        raise ValueError("checkpoint version is invalid")
    records = normalize_records(value["records"])
    summary = {
        "blocked": sum(item["status"] == "blocked" for item in records),
        "ready": sum(item["status"] == "ready" for item in records),
    }
    if records != value["records"] or summary != value["summary"]:
        raise ValueError("checkpoint content is inconsistent")
    if records_digest(records) != value["records_sha256"]:
        raise ValueError("checkpoint binding is invalid")
    return value
