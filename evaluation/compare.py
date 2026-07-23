#!/usr/bin/env python3
"""Validate, normalize, and render deterministic protocol replay comparisons."""

from __future__ import annotations

import argparse
import copy
import html
import os
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import replay


MODEL_VERSION = 1
VOCABULARY_VERSION = 1
DISCLAIMER = (
    "Deterministic protocol behavior only. This comparison does not measure "
    "live-agent delivery quality, cost, latency, model performance, production "
    "safety, or identify a winning workflow."
)
OUTPUT_NAMES = ("comparison-v1.json", "comparison-v1.md", "dashboard-v1.html")
COUNT_ORDER = ("events", "accepted", "rejected", "unsupported", "error")
MISSING = object()

WORKFLOW_VOCABULARY = {
    "v0.2.0": (
        "v0.2.0",
        "Published workflow v0.2.0.",
    ),
    "v0.4.0": (
        "v0.4.0",
        "Published workflow v0.4.0.",
    ),
    "365c4a6acebd6e7d40adb23f49d0e2bec6c60fbc": (
        "Main comparison anchor",
        "Frozen mainline workflow comparison anchor.",
    ),
    "5095aedcd498e868df791c122d2b8c687c9fb764": (
        "Frozen ownership candidate",
        "Frozen candidate workflow used only as deterministic replay input.",
    ),
}
EVENT_VOCABULARY = {
    "ACTIONABLE_FINDINGS": (
        "Actionable findings",
        "Review reported changes that require an implementation response.",
    ),
    "FIX_PUSHED": (
        "Fix pushed",
        "An implementation fix was reported as pushed for review.",
    ),
    "FLOW_BLOCKED": (
        "Flow blocked",
        "The workflow entered its resumable blocked state.",
    ),
    "IMPLEMENTATION_PACKET": (
        "Implementation packet",
        "Implementation evidence was submitted for an exact pull request head.",
    ),
    "MERGES_VERIFIED": (
        "Merges verified",
        "Expected merges were reported as verified.",
    ),
    "UNKNOWN_EVENT": (
        "Unknown trace event",
        "The frozen trace intentionally supplied an event absent from the graph.",
    ),
    "RESEARCH_PACKET": (
        "Research packet",
        "A persisted research contract was submitted to the workflow.",
    ),
    "RESUME_AUTHORIZED": (
        "Resume authorized",
        "The blocked workflow was authorized to resume at a supplied node.",
    ),
}
NODE_VOCABULARY = {
    "blocked": ("Blocked", "Resumable blocked workflow state."),
    "complete": ("Complete", "Terminal workflow state."),
    "fixer": ("Fixer", "Implementation-remediation workflow state."),
    "implementation": ("Implementation", "Approved leaf delivery state."),
    "merge": ("Merge", "Exact-head merge integration state."),
    "research": ("Research", "Repository-grounded contract research state."),
    "review": ("Review", "Exact-head review state."),
}
DISPOSITION_VOCABULARY = {
    "accepted": ("Accepted", "The event matched and applied one transition."),
    "rejected": ("Rejected", "A matching transition failed its guard."),
    "unsupported": ("Unsupported", "No workflow transition supports the event."),
    "error": ("Error", "The event could not be evaluated as a valid transition."),
}
ERROR_VOCABULARY = {
    "ambiguous-transition": (
        "Ambiguous transition",
        "More than one workflow transition matched the event.",
    ),
    "current-node-mismatch": (
        "Current node mismatch",
        "The event declared a node different from replay state.",
    ),
    "guard-rejected": (
        "Guard rejected",
        "A transition was found but its declared guard did not pass.",
    ),
    "invalid-target": (
        "Invalid target",
        "A dynamic transition target was not a declared workflow node.",
    ),
    "state-mutation-error": (
        "State mutation error",
        "A declared state mutation could not be applied.",
    ),
    "unsupported-event": (
        "Unsupported event",
        "No transition supports the event from the current node.",
    ),
}
TRANSITION_VOCABULARY = {
    "lane-blocked": (
        "Lane blocked",
        "Move an active delivery lane into the resumable blocked state.",
    ),
    "research-approved": (
        "Research approved",
        "Advance an approved research contract to implementation.",
    ),
    "resume": (
        "Resume",
        "Return a blocked flow to its authorized continuation state.",
    ),
}
COUNT_VOCABULARY = {
    "events": ("Events", "Total chronological protocol events.", "event count"),
    "accepted": ("Accepted", "Events that applied a transition.", "event count"),
    "rejected": ("Rejected", "Events rejected by a transition guard.", "event count"),
    "unsupported": (
        "Unsupported",
        "Events absent from the workflow transition graph.",
        "event count",
    ),
    "error": ("Errors", "Events that produced an evaluator error.", "event count"),
}
STATUS_VOCABULARY = {
    "comparison-reference": (
        "Comparison reference",
        "Designated reference for signed count deltas; not a winner.",
    ),
    "observed": (
        "Observed workflow",
        "A comparable deterministic replay observation.",
    ),
}
DELTA_VOCABULARY = {
    "increase": ("Increase", "Count is higher than the comparison reference."),
    "decrease": ("Decrease", "Count is lower than the comparison reference."),
    "no-change": ("No change", "Count equals the comparison reference."),
    "unknown": ("Unknown", "A comparable numeric delta is unavailable."),
}
DURATION_STATUS_VOCABULARY = {
    "unavailable": (
        "Unavailable",
        "Protocol replay records no observed wall-clock duration.",
    )
}


class ComparisonError(ValueError):
    """A comparison contract, safety, or rendering failure."""


def _exact_keys(
    value: Any,
    label: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ComparisonError(f"{label}: must be an object")
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ComparisonError(f"{label}: missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ComparisonError(f"{label}: unknown fields: {', '.join(sorted(unknown))}")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComparisonError(f"{label}: must be non-blank text")
    return value


def _integer(value: Any, label: str, minimum: int | None = None) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or (minimum is not None and value < minimum)
    ):
        suffix = "" if minimum is None else f" >= {minimum}"
        raise ComparisonError(f"{label}: must be an integer{suffix}")
    return value


def _safe_relative(value: Any, label: str) -> str:
    text = _text(value, label)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or "\\" in text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ComparisonError(f"{label}: invalid relative evidence path")
    return text


def _label(raw: str | None, vocabulary: dict[str, tuple[str, str]]) -> dict[str, Any]:
    if raw is None:
        return {"raw": None, "label": "Unknown", "explanation": None}
    known = vocabulary.get(raw)
    if known is None:
        return {"raw": raw, "label": "Unknown", "explanation": None}
    return {"raw": raw, "label": known[0], "explanation": known[1]}


def _validate_label(
    value: Any,
    label: str,
    vocabulary: dict[str, tuple[str, str]],
    raw: str | None | object = MISSING,
) -> dict[str, Any]:
    item = _exact_keys(value, label, {"raw", "label", "explanation"})
    if item["raw"] is not None:
        _text(item["raw"], f"{label}.raw")
    if raw is not MISSING and item["raw"] != raw:
        raise ComparisonError(f"{label}.raw: inconsistent raw value")
    expected = _label(item["raw"], vocabulary)
    if item != expected:
        raise ComparisonError(f"{label}: inconsistent versioned normalization")
    return item


def _artifact_paths(output_root: Path, run_dir: Path) -> dict[str, str]:
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ComparisonError(f"{run_dir}: run directory must be a real directory")
    try:
        relative = run_dir.resolve().relative_to(output_root.resolve())
    except ValueError as error:
        raise ComparisonError(
            f"{run_dir}: evidence must be beneath the comparison output directory"
        ) from error
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ComparisonError(f"{run_dir}: invalid evidence path")
    paths = {}
    for key, name in (
        ("manifest", "manifest.json"),
        ("events", "events.jsonl"),
        ("result", "result.json"),
    ):
        path = run_dir / name
        if path.is_symlink() or not path.is_file():
            raise ComparisonError(f"{path}: evidence must be a regular file")
        paths[key] = (relative / name).as_posix()
    return paths


def _load_run(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    try:
        replay.validate_run_directory(run_dir)
        manifest = replay.load_json(run_dir / "manifest.json")
        event_bytes = (run_dir / "events.jsonl").read_bytes()
        events = replay.parse_jsonl(event_bytes, str(run_dir / "events.jsonl"))
        result = replay.load_json(run_dir / "result.json")
    except (OSError, replay.ReplayError) as error:
        raise ComparisonError(f"{run_dir}: invalid replay evidence: {error}") from error
    return manifest, events, result


def _source(
    output_root: Path,
    run_dir: Path,
    manifest: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": manifest["run_id"],
        "workflow": _label(manifest["requested_ref"], WORKFLOW_VOCABULARY),
        "workflow_commit_sha": manifest["workflow"]["commit_sha"],
        "workflow_content_sha256": manifest["workflow"]["content_sha256"],
        "manifest_sha256": replay.digest_bytes((run_dir / "manifest.json").read_bytes()),
        "event_trace_sha256": replay.digest_bytes((run_dir / "events.jsonl").read_bytes()),
        "result_sha256": replay.digest_bytes((run_dir / "result.json").read_bytes()),
        "evidence": _artifact_paths(output_root, run_dir),
        "event_count": result["event_count"],
    }


def _delta(value: int, reference: int) -> dict[str, Any]:
    difference = value - reference
    if difference > 0:
        state = "increase"
    elif difference < 0:
        state = "decrease"
    else:
        state = "no-change"
    return {
        "state": _label(state, DELTA_VOCABULARY),
        "value": difference,
    }


def _unknown_delta() -> dict[str, Any]:
    return {
        "state": _label("unknown", DELTA_VOCABULARY),
        "value": None,
    }


def _summary(
    manifest: dict[str, Any],
    result: dict[str, Any],
    reference_result: dict[str, Any],
    reference_run_id: str,
) -> dict[str, Any]:
    values = {"events": result["event_count"], **result["counts"]}
    reference_values = {
        "events": reference_result["event_count"],
        **reference_result["counts"],
    }
    status = (
        "comparison-reference"
        if manifest["run_id"] == reference_run_id
        else "observed"
    )
    return {
        "run_id": manifest["run_id"],
        "status": _label(status, STATUS_VOCABULARY),
        "counts": [
            {
                "metric": _label(name, {
                    key: (item[0], item[1]) for key, item in COUNT_VOCABULARY.items()
                }),
                "unit": COUNT_VOCABULARY[name][2],
                "value": values[name],
                "delta": _delta(values[name], reference_values[name]),
            }
            for name in COUNT_ORDER
        ],
        "duration": {
            "status": _label("unavailable", DURATION_STATUS_VOCABULARY),
            "raw": None,
            "value": None,
            "unit": None,
            "delta": _unknown_delta(),
        },
    }


def _flow(manifest: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for event in events:
        rows.append(
            {
                "sequence": event["sequence"],
                "event": _label(event["event"], EVENT_VOCABULARY),
                "source_node": _label(event["source_node"], NODE_VOCABULARY),
                "target_node": _label(event["target_node"], NODE_VOCABULARY),
                "disposition": _label(
                    event["disposition"], DISPOSITION_VOCABULARY
                ),
                "transition": _label(event["transition_id"], TRANSITION_VOCABULARY),
                "error": _label(event["error_code"], ERROR_VOCABULARY),
            }
        )
    return {"run_id": manifest["run_id"], "events": rows}


def build_model(
    run_dirs: list[Path],
    output_root: Path,
    baseline_run: str,
) -> dict[str, Any]:
    if not run_dirs:
        raise ComparisonError("at least one replay run is required")
    if len({path.resolve() for path in run_dirs}) != len(run_dirs):
        raise ComparisonError("replay run directories must not contain duplicates")
    loaded = [(path, *_load_run(path)) for path in run_dirs]
    manifests = [item[1] for item in loaded]
    try:
        replay.require_comparable(manifests)
    except replay.ReplayError as error:
        raise ComparisonError(str(error)) from error
    run_ids = [manifest["run_id"] for manifest in manifests]
    if len(run_ids) != len(set(run_ids)):
        raise ComparisonError("comparison contains duplicate run identities")
    if baseline_run not in run_ids:
        raise ComparisonError("comparison reference is not one of the source runs")
    reference_result = next(
        result for _, manifest, _, result in loaded if manifest["run_id"] == baseline_run
    )
    first = manifests[0]
    model = {
        "schema_version": MODEL_VERSION,
        "vocabulary_version": VOCABULARY_VERSION,
        "interpretation": DISCLAIMER,
        "comparison_reference_run_id": baseline_run,
        "comparability": {
            path: copy.deepcopy(replay._lookup_required(first, path))
            for path in replay.COMPARABILITY_PATHS
        },
        "source_order": run_ids,
        "sources": [
            _source(output_root, run_dir, manifest, result)
            for run_dir, manifest, _, result in loaded
        ],
        "summaries": [
            _summary(manifest, result, reference_result, baseline_run)
            for _, manifest, _, result in loaded
        ],
        "flows": [
            _flow(manifest, events) for _, manifest, events, _ in loaded
        ],
    }
    validate_model(model, output_root)
    return model


def validate_model(model: dict[str, Any], output_root: Path) -> dict[str, Any]:
    _exact_keys(
        model,
        "comparison",
        {
            "schema_version",
            "vocabulary_version",
            "interpretation",
            "comparison_reference_run_id",
            "comparability",
            "source_order",
            "sources",
            "summaries",
            "flows",
        },
    )
    if model["schema_version"] != MODEL_VERSION:
        raise ComparisonError("comparison.schema_version: unsupported version")
    if model["vocabulary_version"] != VOCABULARY_VERSION:
        raise ComparisonError("comparison.vocabulary_version: unsupported version")
    if model["interpretation"] != DISCLAIMER:
        raise ComparisonError("comparison.interpretation: required disclaimer drift")
    comparability = _exact_keys(
        model["comparability"],
        "comparison.comparability",
        set(replay.COMPARABILITY_PATHS),
    )
    for path in replay.COMPARABILITY_PATHS:
        if comparability[path] is None:
            raise ComparisonError(f"comparison.comparability.{path}: missing value")
    order = model["source_order"]
    if (
        not isinstance(order, list)
        or not order
        or not all(isinstance(item, str) for item in order)
        or len(order) != len(set(order))
    ):
        raise ComparisonError("comparison.source_order: invalid or duplicate run IDs")
    if model["comparison_reference_run_id"] not in order:
        raise ComparisonError("comparison reference is dangling")
    sources = model["sources"]
    summaries = model["summaries"]
    flows = model["flows"]
    if not all(isinstance(items, list) for items in (sources, summaries, flows)):
        raise ComparisonError("comparison collections must be arrays")
    for label, items in (
        ("sources", sources),
        ("summaries", summaries),
        ("flows", flows),
    ):
        if [item.get("run_id") if isinstance(item, dict) else None for item in items] != order:
            raise ComparisonError(f"comparison.{label}: inconsistent source ordering")
    source_by_id: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(sources):
        label = f"comparison.sources[{index}]"
        item = _exact_keys(
            source,
            label,
            {
                "run_id",
                "workflow",
                "workflow_commit_sha",
                "workflow_content_sha256",
                "manifest_sha256",
                "event_trace_sha256",
                "result_sha256",
                "evidence",
                "event_count",
            },
        )
        if not re.fullmatch(r"run-[0-9a-f]{24}", item["run_id"]):
            raise ComparisonError(f"{label}.run_id: invalid run ID")
        _validate_label(item["workflow"], f"{label}.workflow", WORKFLOW_VOCABULARY)
        replay._sha(item["workflow_commit_sha"], f"{label}.workflow_commit_sha")
        for field in (
            "workflow_content_sha256",
            "manifest_sha256",
            "event_trace_sha256",
            "result_sha256",
        ):
            replay._digest(item[field], f"{label}.{field}")
        evidence = _exact_keys(
            item["evidence"], f"{label}.evidence", {"manifest", "events", "result"}
        )
        for field, expected_digest in (
            ("manifest", item["manifest_sha256"]),
            ("events", item["event_trace_sha256"]),
            ("result", item["result_sha256"]),
        ):
            relative = _safe_relative(evidence[field], f"{label}.evidence.{field}")
            path = output_root / PurePosixPath(relative)
            if path.is_symlink() or not path.is_file():
                raise ComparisonError(f"{label}.evidence.{field}: dangling evidence link")
            try:
                path.resolve().relative_to(output_root.resolve())
            except ValueError as error:
                raise ComparisonError(
                    f"{label}.evidence.{field}: evidence escapes output directory"
                ) from error
            if replay.digest_bytes(path.read_bytes()) != expected_digest:
                raise ComparisonError(f"{label}.{field}_sha256: source digest mismatch")
        manifest = replay.load_json(output_root / evidence["manifest"])
        result = replay.load_json(output_root / evidence["result"])
        events = replay.parse_jsonl(
            (output_root / evidence["events"]).read_bytes(),
            evidence["events"],
        )
        if manifest["run_id"] != item["run_id"] or result["run_id"] != item["run_id"]:
            raise ComparisonError(f"{label}: dangling run identity")
        if (
            manifest["requested_ref"] != item["workflow"]["raw"]
            or manifest["workflow"]["commit_sha"] != item["workflow_commit_sha"]
            or manifest["workflow"]["content_sha256"] != item["workflow_content_sha256"]
            or result["event_count"] != item["event_count"]
            or len(events) != item["event_count"]
        ):
            raise ComparisonError(f"{label}: source artifact binding mismatch")
        replay.validate_run_directory((output_root / evidence["manifest"]).parent)
        for path in replay.COMPARABILITY_PATHS:
            if replay._lookup_required(manifest, path) != comparability[path]:
                raise ComparisonError(f"{label}: comparability binding mismatch: {path}")
        source_by_id[item["run_id"]] = item
    reference_summary = summaries[order.index(model["comparison_reference_run_id"])]
    reference_counts: dict[str, int] = {}
    for count in reference_summary.get("counts", []):
        if isinstance(count, dict):
            metric = count.get("metric")
            if isinstance(metric, dict) and isinstance(metric.get("raw"), str):
                reference_counts[metric["raw"]] = count.get("value")
    for index, summary in enumerate(summaries):
        label = f"comparison.summaries[{index}]"
        item = _exact_keys(
            summary, label, {"run_id", "status", "counts", "duration"}
        )
        expected_status = (
            "comparison-reference"
            if item["run_id"] == model["comparison_reference_run_id"]
            else "observed"
        )
        _validate_label(
            item["status"], f"{label}.status", STATUS_VOCABULARY, expected_status
        )
        counts = item["counts"]
        if not isinstance(counts, list) or len(counts) != len(COUNT_ORDER):
            raise ComparisonError(f"{label}.counts: invalid count rows")
        if [row.get("metric", {}).get("raw") for row in counts] != list(COUNT_ORDER):
            raise ComparisonError(f"{label}.counts: inconsistent metric ordering")
        result = replay.load_json(
            output_root / source_by_id[item["run_id"]]["evidence"]["result"]
        )
        expected_values = {"events": result["event_count"], **result["counts"]}
        for count_index, row in enumerate(counts):
            row_label = f"{label}.counts[{count_index}]"
            count = _exact_keys(
                row, row_label, {"metric", "unit", "value", "delta"}
            )
            raw = COUNT_ORDER[count_index]
            _validate_label(
                count["metric"],
                f"{row_label}.metric",
                {key: (value[0], value[1]) for key, value in COUNT_VOCABULARY.items()},
                raw,
            )
            if count["unit"] != COUNT_VOCABULARY[raw][2]:
                raise ComparisonError(f"{row_label}.unit: inconsistent unit")
            value = _integer(count["value"], f"{row_label}.value", 0)
            if value != expected_values[raw]:
                raise ComparisonError(f"{row_label}.value: inconsistent source count")
            delta = _exact_keys(count["delta"], f"{row_label}.delta", {"state", "value"})
            difference = value - reference_counts[raw]
            expected_state = (
                "increase" if difference > 0 else "decrease" if difference < 0 else "no-change"
            )
            _validate_label(
                delta["state"],
                f"{row_label}.delta.state",
                DELTA_VOCABULARY,
                expected_state,
            )
            if _integer(delta["value"], f"{row_label}.delta.value") != difference:
                raise ComparisonError(f"{row_label}.delta.value: inconsistent delta")
        duration = _exact_keys(
            item["duration"],
            f"{label}.duration",
            {"status", "raw", "value", "unit", "delta"},
        )
        _validate_label(
            duration["status"],
            f"{label}.duration.status",
            DURATION_STATUS_VOCABULARY,
            "unavailable",
        )
        if any(duration[field] is not None for field in ("raw", "value", "unit")):
            raise ComparisonError(
                f"{label}.duration: replay duration must remain unavailable"
            )
        duration_delta = _exact_keys(
            duration["delta"], f"{label}.duration.delta", {"state", "value"}
        )
        _validate_label(
            duration_delta["state"],
            f"{label}.duration.delta.state",
            DELTA_VOCABULARY,
            "unknown",
        )
        if duration_delta["value"] is not None:
            raise ComparisonError(
                f"{label}.duration.delta.value: unavailable delta must be null"
            )
    for index, flow in enumerate(flows):
        label = f"comparison.flows[{index}]"
        item = _exact_keys(flow, label, {"run_id", "events"})
        events = item["events"]
        source = source_by_id[item["run_id"]]
        raw_events = replay.parse_jsonl(
            (output_root / source["evidence"]["events"]).read_bytes(),
            source["evidence"]["events"],
        )
        if not isinstance(events, list) or len(events) != len(raw_events):
            raise ComparisonError(f"{label}.events: inconsistent event count")
        for sequence, (row, raw_event) in enumerate(zip(events, raw_events)):
            row_label = f"{label}.events[{sequence}]"
            event = _exact_keys(
                row,
                row_label,
                {
                    "sequence",
                    "event",
                    "source_node",
                    "target_node",
                    "disposition",
                    "transition",
                    "error",
                },
            )
            if event["sequence"] != sequence or sequence != raw_event["sequence"]:
                raise ComparisonError(f"{row_label}.sequence: inconsistent ordering")
            for field, vocabulary in (
                ("event", EVENT_VOCABULARY),
                ("source_node", NODE_VOCABULARY),
                ("target_node", NODE_VOCABULARY),
                ("disposition", DISPOSITION_VOCABULARY),
                ("transition", TRANSITION_VOCABULARY),
                ("error", ERROR_VOCABULARY),
            ):
                raw_field = "transition_id" if field == "transition" else (
                    "error_code" if field == "error" else field
                )
                _validate_label(
                    event[field],
                    f"{row_label}.{field}",
                    vocabulary,
                    raw_event[raw_field],
                )
    return model


def _delta_text(delta: dict[str, Any]) -> str:
    value = delta["value"]
    if delta["state"]["raw"] == "no-change":
        return "no change"
    return f"{value:+d}"


def _display_markdown(value: dict[str, Any]) -> str:
    if value["label"] == "Unknown" and value["raw"] is not None:
        raw = str(value["raw"]).replace("`", "\\`").replace("|", "\\|")
        return f"Unknown (`{raw}`)"
    return value["label"]


def _display_html(value: dict[str, Any]) -> str:
    if value["label"] == "Unknown" and value["raw"] is not None:
        return f"Unknown ({value['raw']})"
    return value["label"]


def render_markdown(model: dict[str, Any]) -> bytes:
    lines = [
        "# Frozen protocol-replay baseline",
        "",
        f"> {model['interpretation']}",
        "",
        "## Comparison summary",
        "",
    ]
    sources = {item["run_id"]: item for item in model["sources"]}
    headers = [
        "Workflow",
        "Status",
        *[COUNT_VOCABULARY[key][0] for key in COUNT_ORDER],
        "Duration",
    ]
    lines.extend(
        [
            "| " + " | ".join(headers) + " |",
            "|" + "|".join("---" for _ in headers) + "|",
        ]
    )
    for summary in model["summaries"]:
        source = sources[summary["run_id"]]
        values = [
            f"{row['value']} ({_delta_text(row['delta'])})"
            for row in summary["counts"]
        ]
        lines.append(
            "| "
            + " | ".join(
                [
                    _display_markdown(source["workflow"]),
                    _display_markdown(summary["status"]),
                    *values,
                    "Unknown",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Deltas are signed event-count differences from the explicitly designated "
            "comparison reference. They are not quality scores or rankings.",
            "",
            "## Chronological event flow",
            "",
        ]
    )
    flows = {item["run_id"]: item for item in model["flows"]}
    for run_id in model["source_order"]:
        source = sources[run_id]
        lines.extend(
            [
                f"### {_display_markdown(source['workflow'])}",
                "",
                f"- Commit: `{source['workflow_commit_sha']}`",
                f"- Workflow digest: `{source['workflow_content_sha256']}`",
                f"- Raw evidence: [manifest]({source['evidence']['manifest']}), "
                f"[events]({source['evidence']['events']}), "
                f"[result]({source['evidence']['result']})",
                "",
                "| Seq | Event | From | To | Disposition | Technical detail |",
                "|---:|---|---|---|---|---|",
            ]
        )
        for event in flows[run_id]["events"]:
            target = (
                _display_markdown(event["target_node"])
                if event["target_node"]["raw"]
                else "—"
            )
            technical = (
                _display_markdown(event["transition"])
                if event["transition"]["raw"]
                else _display_markdown(event["error"])
                + (
                    f" (`{event['error']['raw']}`)"
                    if event["error"]["raw"] and event["error"]["label"] != "Unknown"
                    else ""
                )
            )
            lines.append(
                f"| {event['sequence']} | {_display_markdown(event['event'])} | "
                f"{_display_markdown(event['source_node'])} | {target} | "
                f"{_display_markdown(event['disposition'])} | {technical or '—'} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Audit note",
            "",
            "Canonical manifest JSON, event JSONL, and result JSON remain the source of "
            "truth. Human labels come only from vocabulary version "
            f"{model['vocabulary_version']}; unknown values retain their exact raw form.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _href(value: str) -> str:
    safe = _safe_relative(value, "HTML evidence link")
    return "/".join(quote(part, safe="") for part in PurePosixPath(safe).parts)


def render_html(model: dict[str, Any]) -> bytes:
    sources = {item["run_id"]: item for item in model["sources"]}
    flows = {item["run_id"]: item for item in model["flows"]}
    summary_rows = []
    for summary in model["summaries"]:
        source = sources[summary["run_id"]]
        cells = "".join(
            f"<td>{row['value']} <span class=\"delta\">"
            f"({html.escape(_delta_text(row['delta']))})</span></td>"
            for row in summary["counts"]
        )
        summary_rows.append(
            "<tr><th scope=\"row\">"
            + html.escape(_display_html(source["workflow"]))
            + "</th><td>"
            + html.escape(summary["status"]["label"])
            + "</td>"
            + cells
            + "<td>Unknown</td>"
            + "</tr>"
        )
    sections = []
    for run_id in model["source_order"]:
        source = sources[run_id]
        rows = []
        for event in flows[run_id]["events"]:
            target = (
                _display_html(event["target_node"])
                if event["target_node"]["raw"]
                else "—"
            )
            rows.append(
                "<tr><td>"
                + str(event["sequence"])
                + "</td><td>"
                + html.escape(_display_html(event["event"]))
                + "</td><td>"
                + html.escape(_display_html(event["source_node"]))
                + "</td><td>"
                + html.escape(target)
                + "</td><td>"
                + html.escape(_display_html(event["disposition"]))
                + "</td></tr>"
            )
        technical = html.escape(
            replay.canonical_bytes(
                {
                    "run_id": run_id,
                    "workflow": source["workflow"],
                    "workflow_commit_sha": source["workflow_commit_sha"],
                    "workflow_content_sha256": source["workflow_content_sha256"],
                    "manifest_sha256": source["manifest_sha256"],
                    "event_trace_sha256": source["event_trace_sha256"],
                    "result_sha256": source["result_sha256"],
                    "events": flows[run_id]["events"],
                }
            ).decode("utf-8")
        )
        links = " · ".join(
            f'<a href="{html.escape(_href(path), quote=True)}">{html.escape(name)}</a>'
            for name, path in source["evidence"].items()
        )
        sections.append(
            "<section><h2>"
            + html.escape(_display_html(source["workflow"]))
            + "</h2><p>"
            + html.escape(source["workflow"]["explanation"] or "")
            + "</p><p class=\"evidence\">Raw evidence: "
            + links
            + "</p><table><thead><tr><th>Seq</th><th>Event</th><th>From</th>"
            + "<th>To</th><th>Disposition</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table><details><summary>Technical identifiers and digests"
            + "</summary><pre>"
            + technical
            + "</pre></details></section>"
        )
    document = (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Frozen protocol-replay baseline</title><style>"
        ":root{color-scheme:light dark;font-family:system-ui,sans-serif}"
        "body{max-width:76rem;margin:auto;padding:2rem;line-height:1.5}"
        ".warning{border-left:.35rem solid #b7791f;padding:.75rem 1rem;background:#fff8dc;color:#2d2416}"
        "table{border-collapse:collapse;width:100%;margin:1rem 0 2rem}"
        "th,td{border:1px solid #888;padding:.45rem;text-align:left}"
        "thead th{background:rgba(127,127,127,.18)}"
        ".delta{white-space:nowrap}pre{overflow:auto;padding:1rem;background:rgba(127,127,127,.12)}"
        "a{color:inherit}section{margin-top:2.5rem}details{margin:1rem 0}"
        "</style></head><body><main><h1>Frozen protocol-replay baseline</h1>"
        "<p class=\"warning\"><strong>Interpretation boundary:</strong> "
        + html.escape(model["interpretation"])
        + "</p><h2>Comparison summary</h2><p>Deltas are signed event-count "
        "differences from the comparison reference, not scores or rankings.</p>"
        "<table><thead><tr><th>Workflow</th><th>Status</th>"
        + "".join(f"<th>{html.escape(COUNT_VOCABULARY[key][0])}</th>" for key in COUNT_ORDER)
        + "<th>Duration</th>"
        + "</tr></thead><tbody>"
        + "".join(summary_rows)
        + "</tbody></table>"
        + "".join(sections)
        + "<section><h2>Audit note</h2><p>Canonical JSON and JSONL evidence remains "
        "the source of truth. Labels come only from vocabulary version "
        + str(model["vocabulary_version"])
        + "; unknown values retain their exact raw form.</p></section></main></body></html>\n"
    )
    return document.encode("utf-8")


def _validate_output_root(path: Path) -> Path:
    if path.exists() and path.is_symlink():
        raise ComparisonError(f"output path must not be a symlink: {path}")
    resolved = path.resolve()
    if resolved == Path(resolved.anchor) or resolved.name in {"", ".", "..", ".git"}:
        raise ComparisonError(f"unsafe output path: {path}")
    if resolved.exists() and not resolved.is_dir():
        raise ComparisonError(f"output path is not a directory: {path}")
    return resolved


def _atomic_replace(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def compare_runs(
    run_dirs: list[Path],
    output_path: Path,
    baseline_run: str,
    *,
    check: bool = False,
) -> dict[str, Any]:
    output_root = _validate_output_root(output_path)
    model = build_model(run_dirs, output_root, baseline_run)
    outputs = {
        "comparison-v1.json": replay.canonical_bytes(model),
        "comparison-v1.md": render_markdown(model),
        "dashboard-v1.html": render_html(model),
    }
    if check:
        for name, content in outputs.items():
            path = output_root / name
            if not path.is_file() or path.is_symlink() or path.read_bytes() != content:
                raise ComparisonError(f"{path}: generated output is missing or stale")
        return model
    output_root.mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_NAMES:
        path = output_root / name
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ComparisonError(f"unsafe generated output path: {path}")
    for name in OUTPUT_NAMES:
        _atomic_replace(output_root / name, outputs[name])
    validate_model(replay.loads_json(outputs["comparison-v1.json"], "comparison"), output_root)
    return model


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare validated deterministic protocol replay run directories."
    )
    parser.add_argument("--run", action="append", dest="runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-run", required=True)
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        model = compare_runs(
            arguments.runs,
            arguments.output,
            arguments.baseline_run,
            check=arguments.check,
        )
    except (ComparisonError, replay.ReplayError) as error:
        print(f"comparison error: {error}", file=sys.stderr)
        return 2
    print(
        f"comparison passed: {len(model['sources'])} runs, "
        f"reference {model['comparison_reference_run_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
