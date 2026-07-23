import json
import shutil
import tempfile
import unittest
from pathlib import Path

from validate import ContractError, load_json, validate


HERE = Path(__file__).resolve().parent
LOW = "low-risk-existing-tests-v1"
REVIEW = "seeded-review-defects-v1"


class EvaluationContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "evaluation"
        shutil.copytree(HERE, self.root, ignore=shutil.ignore_patterns("__pycache__"))

    def tearDown(self):
        self.temporary.cleanup()

    def rewrite_json(self, relative, mutate):
        path = self.root / relative
        value = load_json(path)
        mutate(value)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def assert_invalid(self, message):
        with self.assertRaisesRegex(ContractError, message):
            validate(self.root)

    def test_checked_in_corpus_is_valid(self):
        result = validate(self.root)
        self.assertEqual(result["fixtures"], "2")

    def test_duplicate_json_keys_are_rejected(self):
        path = self.root / "suite-v1.json"
        path.write_text(
            '{"schema_version":1,"schema_version":1}\n', encoding="utf-8"
        )
        self.assert_invalid("duplicate JSON key")

    def test_unknown_fields_are_rejected(self):
        self.rewrite_json("suite-v1.json", lambda value: value.update({"extra": 1}))
        self.assert_invalid("unknown fields")

    def test_unsupported_versions_are_rejected(self):
        self.rewrite_json(
            "suite-v1.json", lambda value: value.update({"schema_version": 2})
        )
        self.assert_invalid("unsupported version")

    def test_blank_identifiers_and_text_are_rejected(self):
        self.rewrite_json(
            f"fixtures/{LOW}/public/manifest-v1.json",
            lambda value: value.update({"fixture_id": " "}),
        )
        self.assert_invalid("non-blank|invalid identifier")

        shutil.rmtree(self.root)
        shutil.copytree(HERE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / f"fixtures/{LOW}/public/payload/prompt.md").write_text(
            " \n", encoding="utf-8"
        )
        self.assert_invalid("must be non-blank text")

    def test_unsafe_and_escaping_paths_are_rejected(self):
        self.rewrite_json(
            f"fixtures/{LOW}/public/manifest-v1.json",
            lambda value: value.update({"prompt": "../grader/grader-v1.json"}),
        )
        self.assert_invalid("unsafe or escaping path")

    def test_duplicate_fixture_and_check_ids_are_rejected(self):
        self.rewrite_json(
            "suite-v1.json",
            lambda value: value["fixtures"].append(dict(value["fixtures"][0])),
        )
        self.assert_invalid("duplicate fixture ID")

        shutil.rmtree(self.root)
        shutil.copytree(HERE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        self.rewrite_json(
            f"fixtures/{REVIEW}/grader/grader-v1.json",
            lambda value: value["checks"].append(dict(value["checks"][0])),
        )
        self.assert_invalid("duplicate check ID")

    def test_dangling_manifest_and_grader_references_are_rejected(self):
        self.rewrite_json(
            "suite-v1.json",
            lambda value: value["fixtures"][0].update(
                {"manifest": f"fixtures/{LOW}/public/missing.json"}
            ),
        )
        self.assert_invalid("dangling manifest reference")

        shutil.rmtree(self.root)
        shutil.copytree(HERE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        self.rewrite_json(
            f"fixtures/{LOW}/public/manifest-v1.json",
            lambda value: value["grader"].update({"id": "wrong-grader"}),
        )
        self.assert_invalid("dangling grader reference")

    def test_digest_and_public_grader_binding_tampering_is_rejected(self):
        prompt = self.root / f"fixtures/{LOW}/public/payload/prompt.md"
        prompt.write_text(prompt.read_text() + "tamper\n", encoding="utf-8")
        self.assert_invalid("tampering or binding drift")

        shutil.rmtree(self.root)
        shutil.copytree(HERE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        self.rewrite_json(
            f"fixtures/{LOW}/grader/grader-v1.json",
            lambda value: value.update(
                {"public_fixture_sha256": "sha256:" + "0" * 64}
            ),
        )
        self.assert_invalid("public/grader binding drift|tampering or binding drift")

    def test_missing_and_extra_assets_are_rejected(self):
        (self.root / f"fixtures/{LOW}/public/payload/prompt.md").unlink()
        self.assert_invalid("missing prompt asset|tampering or binding drift")

        shutil.rmtree(self.root)
        shutil.copytree(HERE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / f"fixtures/{LOW}/public/unexpected.txt").write_text(
            "extra\n", encoding="utf-8"
        )
        self.assert_invalid("missing or extra public assets")

    def test_extra_fixture_directory_is_rejected(self):
        (self.root / "fixtures/unindexed-v1").mkdir()
        self.assert_invalid("missing, extra, or dangling fixture directories")

    def test_missing_or_extra_schema_documents_are_rejected(self):
        (self.root / "schemas/unversioned.schema.json").write_text(
            "{}\n", encoding="utf-8"
        )
        self.assert_invalid("missing or extra version 1 schema documents")

    def test_held_out_details_cannot_leak_into_public_content(self):
        prompt = self.root / f"fixtures/{LOW}/public/payload/prompt.md"
        prompt.write_text("Run grader_tools/private.py\n", encoding="utf-8")
        self.assert_invalid("held-out grader detail leaked")

    def test_symlink_assets_are_rejected(self):
        target = self.root / f"fixtures/{LOW}/public/payload/prompt.md"
        link = self.root / f"fixtures/{LOW}/public/payload/linked.md"
        link.symlink_to(target)
        self.assert_invalid("symlinks are not allowed")


if __name__ == "__main__":
    unittest.main()
