import unittest

from voice_state import StateError, handle_hook


def bash(command):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


class VoiceHookTests(unittest.TestCase):
    def test_direct_delivery_forms_are_blocked_in_favor_of_typed_runner(self):
        payloads = [
            bash("gh pr merge 42 --repo owner/repo"),
            bash("gh api --method PUT repos/owner/repo/pulls/42/merge"),
            bash("bash -lc 'gh pr merge 42 --repo owner/repo'"),
            bash('"gh" pr merge 42 --repo owner/repo'),
            bash("git push origin HEAD:main"),
            bash("bash -lc 'npm publish'"),
            bash("gh pr merge 42 --repo owner/repo && gh issue close 9"),
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "mcp__github__merge_pull_request",
                "tool_input": {
                    "owner": "owner",
                    "repo": "repo",
                    "pull_number": 42,
                },
            },
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "mcp__codex_apps__github__close_issue",
                "tool_input": {"repo": "owner/repo", "issue_number": 9},
            },
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(StateError, "typed voice_state.py deliver"):
                    handle_hook(payload)

    def test_safe_branch_push_is_not_blocked(self):
        handle_hook(bash("git push origin feat/voice-first-control-plane"))


if __name__ == "__main__":
    unittest.main()
