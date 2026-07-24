import unittest
from voice_state import StateError, claim, deliver, grant_delivery, invalidate, new_task, review, set_implemented, validate_lanes, work_class


def task():
    return new_task("task-1", {"intent": "fix", "scope": ["x"], "non_scope": ["y"], "repo": "r", "owner": "coordinator", "branch": "b", "acceptance": ["test"]})


class VoiceStateTests(unittest.TestCase):
    def test_direct_path_requires_independence_and_current_authority(self):
        state = task(); claim(state, "implementer", "b", "w"); set_implemented(state, "implementer", "a" * 40, ["test"])
        with self.assertRaises(StateError): review(state, "implementer", "a" * 40, True)
        review(state, "gate", "a" * 40, True); grant_delivery(state, "user", "r", "42", "a" * 40); deliver(state, "r", "42", "a" * 40)
        self.assertEqual(state["state"], "complete")

    def test_head_drift_invalidates_review_and_authority(self):
        state = task(); claim(state, "writer", "b", "w"); set_implemented(state, "writer", "a" * 40, [])
        review(state, "gate", "a" * 40, True); grant_delivery(state, "user", "r", "42", "a" * 40); invalidate(state, "head changed")
        with self.assertRaises(StateError): deliver(state, "r", "42", "a" * 40)

    def test_second_writer_is_rejected(self):
        state = task(); claim(state, "writer", "b", "w")
        with self.assertRaises(StateError): claim(state, "other", "b", "other")

    def test_ordinary_bypasses_state_and_complex_requires_approved_lanes(self):
        self.assertEqual(work_class(False, False, False, False), "ordinary")
        self.assertEqual(work_class(True, False, False, False), "durable")
        self.assertEqual(work_class(False, True, False, False), "complex")
        with self.assertRaises(StateError): validate_lanes([{"id": "a", "domain": "src"}], False)
        with self.assertRaises(StateError): validate_lanes([{"id": "a", "domain": "src"}, {"id": "b", "domain": "src"}], True)
        validate_lanes([{"id": "a", "domain": "src"}, {"id": "b", "domain": "docs"}], True)
