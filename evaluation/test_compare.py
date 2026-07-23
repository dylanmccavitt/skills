from __future__ import annotations

import copy
import os
import shutil
import socket
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import compare
import replay


ROOT = Path(__file__).resolve().parent
CHECKED_BASELINE = ROOT / "baseline-v1"


class ComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "baseline"
        shutil.copytree(CHECKED_BASELINE / "raw", self.output / "raw")
        manifests = sorted((self.output / "raw").glob("run-*/manifest.json"))
        by_ref = {
            replay.load_json(path)["requested_ref"]: path.parent for path in manifests
        }
        self.runs = [
            by_ref["v0.2.0"],
            by_ref["v0.4.0"],
            by_ref["365c4a6acebd6e7d40adb23f49d0e2bec6c60fbc"],
            by_ref["5095aedcd498e868df791c122d2b8c687c9fb764"],
        ]
        self.reference = replay.load_json(self.runs[1] / "manifest.json")["run_id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _rebind(
        self,
        run_dir: Path,
        *,
        manifest_mutator=None,
        event_mutator=None,
    ) -> Path:
        manifest = replay.load_json(run_dir / "manifest.json")
        events = replay.parse_jsonl(
            (run_dir / "events.jsonl").read_bytes(), "events"
        )
        result = replay.load_json(run_dir / "result.json")
        if manifest_mutator is not None:
            manifest_mutator(manifest)
        identity = {
            "suite": manifest["suite"],
            "fixture": manifest["fixture"],
            "trace": manifest["trace"],
            "workflow": manifest["workflow"],
            "evaluator": manifest["evaluator"],
            "initial_repository_sha": manifest["initial_repository_sha"],
            "execution": manifest["execution"],
        }
        run_key = replay.framed_digest("protocol-replay-run-key-v1", identity)
        manifest["run_key_sha256"] = run_key
        manifest["run_id"] = "run-" + run_key[7:31]
        for event in events:
            event["run_id"] = manifest["run_id"]
        if event_mutator is not None:
            event_mutator(events)
        manifest_bytes = replay.canonical_bytes(manifest)
        event_bytes = replay._jsonl_bytes(events)
        counts = Counter(event["disposition"] for event in events)
        result.update(
            {
                "run_id": manifest["run_id"],
                "manifest_sha256": replay.digest_bytes(manifest_bytes),
                "event_trace_sha256": replay.digest_bytes(event_bytes),
                "event_count": len(events),
                "counts": {
                    disposition: counts[disposition]
                    for disposition in sorted(replay.DISPOSITIONS)
                },
                "initial_state_sha256": events[0]["before_state_sha256"],
                "final_state_sha256": events[-1]["after_state_sha256"],
            }
        )
        target = run_dir.with_name(manifest["run_id"])
        if target != run_dir:
            run_dir.rename(target)
        (target / "manifest.json").write_bytes(manifest_bytes)
        (target / "events.jsonl").write_bytes(event_bytes)
        (target / "result.json").write_bytes(replay.canonical_bytes(result))
        replay.validate_run_directory(target)
        return target

    def _generate(self) -> dict:
        return compare.compare_runs(self.runs, self.output, self.reference)

    def test_checked_in_baseline_is_exact_and_deterministic(self) -> None:
        first = self._generate()
        snapshot = {
            name: (self.output / name).read_bytes() for name in compare.OUTPUT_NAMES
        }
        second = self._generate()
        self.assertEqual(first, second)
        self.assertEqual(
            snapshot,
            {name: (self.output / name).read_bytes() for name in compare.OUTPUT_NAMES},
        )
        compare.compare_runs(
            self.runs, self.output, self.reference, check=True
        )

    def test_every_source_is_validated_before_any_output_is_written(self) -> None:
        result_path = self.runs[-1] / "result.json"
        result_path.write_bytes(result_path.read_bytes() + b" ")
        with self.assertRaisesRegex(compare.ComparisonError, "invalid replay evidence"):
            self._generate()
        for name in compare.OUTPUT_NAMES:
            self.assertFalse((self.output / name).exists())

    def test_incomparable_inputs_name_exact_keys_and_write_nothing(self) -> None:
        changed = self._rebind(
            self.runs[-1],
            manifest_mutator=lambda manifest: manifest["execution"].__setitem__(
                "trial_id", "different-trial"
            ),
        )
        self.runs[-1] = changed
        with self.assertRaisesRegex(
            compare.ComparisonError, r"execution\.trial_id"
        ):
            self._generate()
        for name in compare.OUTPUT_NAMES:
            self.assertFalse((self.output / name).exists())

    def test_model_rejects_unknown_duplicate_dangling_and_tampered_fields(self) -> None:
        model = self._generate()
        unknown = copy.deepcopy(model)
        unknown["surprise"] = True
        with self.assertRaisesRegex(compare.ComparisonError, "unknown fields"):
            compare.validate_model(unknown, self.output)
        unsupported = copy.deepcopy(model)
        unsupported["schema_version"] = 2
        with self.assertRaisesRegex(compare.ComparisonError, "unsupported version"):
            compare.validate_model(unsupported, self.output)
        duplicate = copy.deepcopy(model)
        duplicate["source_order"][1] = duplicate["source_order"][0]
        with self.assertRaisesRegex(compare.ComparisonError, "duplicate run IDs"):
            compare.validate_model(duplicate, self.output)
        dangling = copy.deepcopy(model)
        dangling["comparison_reference_run_id"] = "run-" + ("f" * 24)
        with self.assertRaisesRegex(compare.ComparisonError, "dangling"):
            compare.validate_model(dangling, self.output)
        tampered = copy.deepcopy(model)
        tampered["sources"][0]["manifest_sha256"] = "sha256:" + ("0" * 64)
        with self.assertRaisesRegex(compare.ComparisonError, "source digest mismatch"):
            compare.validate_model(tampered, self.output)

    def test_counts_labels_units_statuses_and_deltas_are_source_bound(self) -> None:
        model = self._generate()
        count = model["summaries"][0]["counts"][0]
        self.assertEqual(count["metric"]["raw"], "events")
        self.assertEqual(count["unit"], "event count")
        self.assertEqual(count["delta"]["state"]["raw"], "no-change")
        self.assertEqual(
            model["summaries"][1]["status"]["raw"], "comparison-reference"
        )
        self.assertEqual(
            model["summaries"][0]["duration"],
            {
                "status": {
                    "raw": "unavailable",
                    "label": "Unavailable",
                    "explanation": "Protocol replay records no observed wall-clock duration.",
                },
                "raw": None,
                "value": None,
                "unit": None,
                "delta": {
                    "state": {
                        "raw": "unknown",
                        "label": "Unknown",
                        "explanation": "A comparable numeric delta is unavailable.",
                    },
                    "value": None,
                },
            },
        )
        for field, value in (
            ("unit", "milliseconds"),
            ("value", 999),
        ):
            changed = copy.deepcopy(model)
            changed["summaries"][0]["counts"][0][field] = value
            with self.assertRaises(compare.ComparisonError):
                compare.validate_model(changed, self.output)
        changed = copy.deepcopy(model)
        changed["summaries"][0]["counts"][0]["delta"]["value"] = 2
        with self.assertRaisesRegex(compare.ComparisonError, "inconsistent delta"):
            compare.validate_model(changed, self.output)
        changed = copy.deepcopy(model)
        changed["summaries"][0]["duration"]["value"] = 5
        with self.assertRaisesRegex(compare.ComparisonError, "must remain unavailable"):
            compare.validate_model(changed, self.output)

    def test_unknown_values_remain_unknown_with_exact_raw_value(self) -> None:
        self.runs[0] = self._rebind(
            self.runs[0],
            event_mutator=lambda events: events[0].__setitem__(
                "event", "FUTURE_PROTOCOL_EVENT"
            ),
        )
        model = self._generate()
        event = model["flows"][0]["events"][0]["event"]
        self.assertEqual(
            event,
            {
                "raw": "FUTURE_PROTOCOL_EVENT",
                "label": "Unknown",
                "explanation": None,
            },
        )
        markdown = compare.render_markdown(model).decode()
        dashboard = compare.render_html(model).decode()
        self.assertIn("Unknown (`FUTURE_PROTOCOL_EVENT`)", markdown)
        self.assertIn("Unknown (FUTURE_PROTOCOL_EVENT)", dashboard)
        changed = copy.deepcopy(model)
        changed["flows"][0]["events"][0]["event"]["label"] = "Future Protocol Event"
        with self.assertRaisesRegex(
            compare.ComparisonError, "inconsistent versioned normalization"
        ):
            compare.validate_model(changed, self.output)

    def test_renderers_are_synchronized_static_and_safely_escape_content(self) -> None:
        hostile = '\"><script>alert(1)</script>&'
        self.runs[0] = self._rebind(
            self.runs[0],
            manifest_mutator=lambda manifest: manifest.__setitem__(
                "requested_ref", hostile
            ),
        )
        model = self._generate()
        markdown = (self.output / "comparison-v1.md").read_text()
        dashboard = (self.output / "dashboard-v1.html").read_text()
        self.assertIn(hostile, model["sources"][0]["workflow"]["raw"])
        self.assertIn("Unknown", markdown)
        self.assertIn("&lt;script&gt;", dashboard)
        self.assertNotIn("<script", dashboard)
        self.assertNotIn("http://", dashboard)
        self.assertNotIn("https://", dashboard)
        self.assertNotIn("<img", dashboard)
        for source in model["sources"]:
            self.assertIn(source["workflow"]["label"], markdown)
            self.assertIn(source["workflow"]["label"], dashboard)
        self.assertIn("<details>", dashboard)
        self.assertIn("Raw evidence:", dashboard)

    def test_path_traversal_symlinks_and_remote_links_fail_closed(self) -> None:
        model = self._generate()
        traversal = copy.deepcopy(model)
        traversal["sources"][0]["evidence"]["manifest"] = "../manifest.json"
        with self.assertRaisesRegex(compare.ComparisonError, "invalid relative"):
            compare.validate_model(traversal, self.output)
        remote = copy.deepcopy(model)
        remote["sources"][0]["evidence"]["manifest"] = "https://example.invalid/x"
        with self.assertRaises(compare.ComparisonError):
            compare.validate_model(remote, self.output)
        link = self.root / "linked-run"
        link.symlink_to(self.runs[0], target_is_directory=True)
        with self.assertRaisesRegex(compare.ComparisonError, "real directory"):
            compare.build_model([link], self.output, self.reference)

    def test_no_network_is_used_and_stale_check_does_not_rewrite(self) -> None:
        with mock.patch.object(
            socket, "socket", side_effect=AssertionError("network use")
        ):
            self._generate()
        markdown = self.output / "comparison-v1.md"
        original = markdown.read_bytes()
        stat = markdown.stat()
        compare.compare_runs(
            self.runs, self.output, self.reference, check=True
        )
        self.assertEqual(original, markdown.read_bytes())
        self.assertEqual(stat.st_mtime_ns, markdown.stat().st_mtime_ns)
        markdown.write_bytes(original + b"stale")
        with self.assertRaisesRegex(compare.ComparisonError, "stale"):
            compare.compare_runs(
                self.runs, self.output, self.reference, check=True
            )

    def test_output_and_evidence_paths_must_be_safe(self) -> None:
        outside = self.root / "outside"
        shutil.copytree(self.runs[0], outside)
        outside_run = replay.load_json(outside / "manifest.json")["run_id"]
        with self.assertRaisesRegex(compare.ComparisonError, "must be beneath"):
            compare.build_model([outside], self.output, outside_run)
        unsafe_output = self.root / "output-link"
        unsafe_output.symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(compare.ComparisonError, "must not be a symlink"):
            compare.compare_runs(self.runs, unsafe_output, self.reference)


if __name__ == "__main__":
    unittest.main()
