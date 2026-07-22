#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from orchestration_packets import PACKET_TYPES, parse_packet_message, validate_packet


SHA = "a" * 40
OTHER_SHA = "b" * 40
CONTENT_REF = "sha256:" + "c" * 64
ISSUE_URL = "https://github.com/owner/repo/issues/1"
PR_URL = "https://github.com/owner/repo/pull/2"


def valid_packets() -> dict[str, dict[str, object]]:
    artifact_locator = {"locator": ISSUE_URL, "content_ref": CONTENT_REF}
    return {
        "RESEARCH_PACKET": {
            "packet_version": 1,
            "issue_url": ISSUE_URL,
            "repository": "owner/repo",
            "base_sha": SHA,
            "issue_write_authority": "persist",
            "decision": "keep",
            "delivery_issue_urls": [ISSUE_URL],
            "artifact": {
                "kind": "github_issue",
                "status": "persisted",
                "marker": "gepetto-research",
                "content_ref": CONTENT_REF,
                "locations": [{
                    "issue_url": ISSUE_URL,
                    "observed_updated_at": "2026-07-22T00:00:00Z",
                }],
            },
        },
        "IMPLEMENTATION_PACKET": {
            "packet_version": 1,
            "issue_url": ISSUE_URL,
            "task_role": "pinocchio",
            "pr_url": PR_URL,
            "pr_head_sha": SHA,
            "artifact": {
                "kind": "github_issue",
                "status": "persisted",
                "marker": "gepetto-implementation",
                "content_ref": CONTENT_REF,
                "issue_url": ISSUE_URL,
                "observed_updated_at": "2026-07-22T00:00:00Z",
            },
        },
        "REVIEW_PACKET": {
            "packet_version": 1,
            "issue_url": ISSUE_URL,
            "pr_url": PR_URL,
            "reviewed_head_sha": SHA,
            "findings": [{
                "id": "F-1", "severity": "low", "disposition": "fixed", "proof": "test",
            }],
            "local_checks": [{"command": "npm test", "result": "pass"}],
            "ci_checks": [{"name": "CI", "conclusion": "success"}],
            "pr_state": {
                "draft": False,
                "mergeable": True,
                "approvals_satisfied": True,
                "unresolved_required_threads": 0,
            },
            "blockers": [],
            "ready_for_jiminy": True,
        },
        "JIMINY_READY": {
            "packet_version": 1,
            "coordinator_thread_id": "task-1",
            "repository": "owner/repo",
            "merge_authority": "merge",
            "merge_order": [PR_URL],
            "expected_pr_urls": [PR_URL],
            "pull_requests": [{
                "issue_url": ISSUE_URL,
                "pr_url": PR_URL,
                "branch": "issue-1",
                "reviewed_head_sha": SHA,
                "reviewer_task_id": "review-1",
                "research_artifact": copy.deepcopy(artifact_locator),
                "implementation_artifact": copy.deepcopy(artifact_locator),
                "dependencies": [],
                "gates": {
                    "review_packet_verified": True,
                    "required_checks_green": True,
                    "approvals_satisfied": "unknown",
                    "unresolved_required_threads": 0,
                    "mergeable": True,
                },
            }],
            "gepetto_merged": False,
        },
        "JIMINY_PR_RESULT": {
            "packet_version": 1,
            "pr_url": PR_URL,
            "state": "MERGED",
            "reviewed_head_sha": SHA,
            "merge_commit_sha": OTHER_SHA,
            "linked_issue_url": ISSUE_URL,
            "linked_issue_state": "CLOSED",
        },
        "JIMINY_INTEGRATION_FAILED": {
            "packet_version": 1,
            "coordinator_thread_id": "task-1",
            "repository": "owner/repo",
            "default_branch": "main",
            "observed_head_sha": SHA,
            "expected_merge_commits": [OTHER_SHA],
            "failed_checks": [{
                "name": "npm test", "result": "failure", "evidence": "exit 1",
            }],
            "remediation_required": True,
        },
        "JIMINY_COMPLETE": {
            "packet_version": 1,
            "coordinator_thread_id": "task-1",
            "repository": "owner/repo",
            "default_branch": "main",
            "verified_default_head_sha": SHA,
            "pull_requests": [{
                "pr_url": PR_URL, "state": "MERGED", "merge_commit_sha": OTHER_SHA,
            }],
            "integration": {
                "expected_merges_present": True,
                "required_checks_green": True,
                "linked_issues_verified": True,
                "runtime_ready_for_completion": True,
            },
            "blockers": [],
            "private_log_path": "/tmp/jiminy.log",
        },
    }


class OrchestrationPacketTest(unittest.TestCase):
    def test_complete_valid_packets_pass(self) -> None:
        packets = valid_packets()
        self.assertEqual(set(packets), PACKET_TYPES)
        for packet_type, payload in packets.items():
            with self.subTest(packet_type=packet_type):
                self.assertIs(validate_packet(packet_type, payload), payload)

    def test_terminal_message_is_header_plus_json_payload(self) -> None:
        payload = valid_packets()["IMPLEMENTATION_PACKET"]
        message = "IMPLEMENTATION_PACKET:\n" + json.dumps(payload)
        packet_type, parsed = parse_packet_message(message, "IMPLEMENTATION_PACKET")
        self.assertEqual(packet_type, "IMPLEMENTATION_PACKET")
        self.assertEqual(parsed, payload)

    def test_header_only_duplicate_and_unknown_packets_fail(self) -> None:
        cases = (
            "IMPLEMENTATION_PACKET:",
            "UNKNOWN_PACKET:\n{}",
            "IMPLEMENTATION_PACKET:\n{}\nREVIEW_PACKET:\n{}",
        )
        for message in cases:
            with self.subTest(message=message), self.assertRaises(ValueError):
                parse_packet_message(message)

    def test_duplicate_json_keys_fail_at_every_nesting_level(self) -> None:
        payload = json.dumps(valid_packets()["REVIEW_PACKET"])
        top_level = payload.replace(
            '"ready_for_jiminy": true',
            '"ready_for_jiminy": false, "ready_for_jiminy": true',
        )
        nested = payload.replace(
            '"draft": false',
            '"draft": true, "draft": false',
        )
        for duplicate_payload in (top_level, nested):
            with self.subTest(payload=duplicate_payload), self.assertRaisesRegex(
                ValueError, "duplicate key"
            ):
                parse_packet_message(f"REVIEW_PACKET:\n{duplicate_payload}")

    def test_unknown_keys_versions_enums_and_packet_types_fail(self) -> None:
        base = valid_packets()["REVIEW_PACKET"]
        cases = []
        unknown = copy.deepcopy(base)
        unknown["surprise"] = True
        cases.append(unknown)
        version = copy.deepcopy(base)
        version["packet_version"] = 2
        cases.append(version)
        enum = copy.deepcopy(base)
        enum["findings"][0]["severity"] = "urgent"
        cases.append(enum)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_packet("REVIEW_PACKET", payload)
        with self.assertRaisesRegex(ValueError, "unknown packet type"):
            validate_packet("UNKNOWN_PACKET", {})

    def test_markdown_urls_short_shas_and_invalid_content_refs_fail(self) -> None:
        research = valid_packets()["RESEARCH_PACKET"]
        bad_url = copy.deepcopy(research)
        bad_url["issue_url"] = "[issue](https://github.com/owner/repo/issues/1)"
        short_sha = copy.deepcopy(research)
        short_sha["base_sha"] = "abc"
        bad_ref = copy.deepcopy(research)
        bad_ref["artifact"]["content_ref"] = "sha1:" + "c" * 40
        for payload in (bad_url, short_sha, bad_ref):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_packet("RESEARCH_PACKET", payload)

    def test_conditional_artifact_and_decision_shapes_fail_closed(self) -> None:
        research = valid_packets()["RESEARCH_PACKET"]
        split = copy.deepcopy(research)
        split["decision"] = "split"
        temporary = copy.deepcopy(research)
        temporary["artifact"]["kind"] = "tmp_markdown"
        for payload in (split, temporary):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_packet("RESEARCH_PACKET", payload)

    def test_temporary_artifacts_cannot_claim_persisted_status(self) -> None:
        research = valid_packets()["RESEARCH_PACKET"]
        research["artifact"] = {
            "kind": "tmp_markdown",
            "status": "persisted",
            "marker": None,
            "content_ref": CONTENT_REF,
            "locations": [{"path": "/tmp/research.md"}],
        }
        implementation = valid_packets()["IMPLEMENTATION_PACKET"]
        implementation["artifact"] = {
            "kind": "tmp_markdown",
            "status": "persisted",
            "marker": None,
            "content_ref": CONTENT_REF,
            "path": "/tmp/implementation.md",
        }
        for packet_type, payload in (
            ("RESEARCH_PACKET", research),
            ("IMPLEMENTATION_PACKET", implementation),
        ):
            with self.subTest(packet_type=packet_type), self.assertRaisesRegex(
                ValueError, "cannot satisfy persistence|must equal 'blocked'"
            ):
                validate_packet(packet_type, payload)

    def test_research_artifact_must_match_issue_write_authority(self) -> None:
        propose_only_with_github = valid_packets()["RESEARCH_PACKET"]
        propose_only_with_github["issue_write_authority"] = "propose-only"
        persist_with_proposal = valid_packets()["RESEARCH_PACKET"]
        persist_with_proposal["artifact"] = {
            "kind": "tmp_markdown",
            "status": "propose-only",
            "marker": None,
            "content_ref": CONTENT_REF,
            "locations": [{"path": "/tmp/research.md"}],
        }
        for packet in (propose_only_with_github, persist_with_proposal):
            with self.subTest(packet=packet), self.assertRaisesRegex(
                ValueError, "issue-write authority|persist authority"
            ):
                validate_packet("RESEARCH_PACKET", packet)

    def test_ready_review_requires_consistent_green_evidence(self) -> None:
        cases = []
        for mutate in (
            lambda packet: packet["blockers"].append("CI failed"),
            lambda packet: packet["findings"][0].update(disposition="blocked"),
            lambda packet: packet["local_checks"][0].update(result="fail"),
            lambda packet: packet["ci_checks"][0].update(conclusion="pending"),
            lambda packet: packet["pr_state"].update(draft=True),
            lambda packet: packet["pr_state"].update(mergeable="unknown"),
            lambda packet: packet["pr_state"].update(approvals_satisfied="unknown"),
            lambda packet: packet["pr_state"].update(unresolved_required_threads=1),
        ):
            packet = valid_packets()["REVIEW_PACKET"]
            mutate(packet)
            cases.append(packet)
        for packet in cases:
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                validate_packet("REVIEW_PACKET", packet)


if __name__ == "__main__":
    unittest.main()
