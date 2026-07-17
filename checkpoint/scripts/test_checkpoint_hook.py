#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("checkpoint_hook.py")
SHIM = Path(__file__).with_name("checkpoint_state.py")
STATE = SCRIPT.resolve().parents[2] / "hooks/orchestration_state.py"


class CheckpointHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, CODEX_ORCHESTRATION_STATE_DIR=self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(self) -> None:
        subprocess.run(
            ["python3", str(STATE), "register", "--session-id", "019f-checkpoint-test", "--role", "gepetto"],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )

    def run_hook(self, source: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SCRIPT)],
            input=json.dumps({
                "session_id": "019f-checkpoint-test",
                "hook_event_name": "SessionStart",
                "source": source,
            }),
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )

    def test_compaction_requests_minimal_unarchived_handoff(self) -> None:
        self.register()
        output = self.run_hook("compact").stdout
        self.assertIn("$checkpoint", output)
        self.assertIn("gepetto", output)
        self.assertIn("unarchived", output)

    def test_unregistered_compaction_is_ignored(self) -> None:
        self.assertEqual(self.run_hook("compact").stdout, "")

    def test_other_session_starts_are_ignored(self) -> None:
        self.assertEqual(self.run_hook("startup").stdout, "")

    def test_cached_legacy_hooks_are_safe(self) -> None:
        for command in ("hook-precompact", "hook-stop"):
            result = subprocess.run(
                ["python3", str(SHIM), command],
                env=self.env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
