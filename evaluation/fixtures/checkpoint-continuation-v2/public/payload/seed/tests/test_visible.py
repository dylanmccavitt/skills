import json
import tempfile
import unittest
from pathlib import Path

from release_builder.checkpoint import load_checkpoint, prepare
from release_builder.model import normalize_records


class ReleaseBuilderTests(unittest.TestCase):
    def test_records_are_validated_and_sorted(self):
        records = normalize_records(
            [
                {
                    "id": "REL-2",
                    "component": "worker",
                    "status": "blocked",
                    "notes": "waiting",
                },
                {
                    "id": "REL-1",
                    "component": "api",
                    "status": "ready",
                    "notes": "green",
                },
            ]
        )
        self.assertEqual([record["id"] for record in records], ["REL-1", "REL-2"])

    def test_prepare_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "records.json"
            target = root / "state.json"
            source.write_text(
                json.dumps(
                    [
                        {
                            "id": "REL-1",
                            "component": "api",
                            "status": "ready",
                            "notes": "green",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            prepare(source, target)
            state = load_checkpoint(target)
            self.assertEqual(state["summary"], {"blocked": 0, "ready": 1})


if __name__ == "__main__":
    unittest.main()
