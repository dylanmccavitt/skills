#!/usr/bin/env python3

import json
import subprocess
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("checkpoint_hook.py")
SHIM = Path(__file__).with_name("checkpoint_state.py")


class CheckpointHookTest(unittest.TestCase):
    def run_hook(self, source: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SCRIPT)],
            input=json.dumps({
                "session_id": "019f-checkpoint-test",
                "hook_event_name": "SessionStart",
                "source": source,
            }),
            text=True,
            capture_output=True,
            check=True,
        )

    def test_compaction_requests_minimal_unarchived_handoff(self) -> None:
        output = self.run_hook("compact").stdout
        self.assertIn("$checkpoint-handoff", output)
        self.assertIn("Gepetto/Jiminy", output)
        self.assertIn("do not archive", output)

    def test_other_session_starts_are_ignored(self) -> None:
        self.assertEqual(self.run_hook("startup").stdout, "")

    def test_cached_legacy_hooks_are_safe(self) -> None:
        for command in ("hook-precompact", "hook-stop"):
            result = subprocess.run(
                ["python3", str(SHIM), command],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
