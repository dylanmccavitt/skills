import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from validate import (
    ContractError,
    document_digest,
    load_json,
    public_fixture_digest,
    tree_digest,
    validate,
)


HERE = Path(__file__).resolve().parent
LOW = "low-risk-existing-tests-v1"
REVIEW = "seeded-review-defects-v1"
CHECKPOINT = "checkpoint-continuation-v2"
CORPUS_V1_BYTES_SHA256 = (
    "055de4a7412053013ba5cb629ea9a42470d461f7c07d2e341580bbb11703f1a5"
)


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

    def reset_corpus(self):
        shutil.rmtree(self.root)
        shutil.copytree(HERE, self.root, ignore=shutil.ignore_patterns("__pycache__"))

    def rebind_fixture(self, fixture_id):
        fixture_root = self.root / "fixtures" / fixture_id
        version = 2 if fixture_id == CHECKPOINT else 1
        manifest_path = fixture_root / f"public/manifest-v{version}.json"
        grader_path = fixture_root / f"grader/grader-v{version}.json"
        manifest = load_json(manifest_path)
        grader = load_json(grader_path)
        payload_digest = tree_digest(fixture_root / "public/payload")
        seed_digest = tree_digest(fixture_root / "public/payload/seed")
        grader["public_fixture_sha256"] = public_fixture_digest(
            fixture_id, version, payload_digest, seed_digest
        )
        if version == 2:
            grader["private_assets_tree_sha256"] = tree_digest(
                fixture_root / "grader/assets"
            )
        grader_path.write_text(json.dumps(grader, indent=2) + "\n", encoding="utf-8")
        grader_digest = document_digest(grader)
        manifest["grader"]["digest"] = grader_digest
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        for suite_version in (1, 2):
            suite_path = self.root / f"suite-v{suite_version}.json"
            suite = load_json(suite_path)
            entry = next(
                (
                    item
                    for item in suite["fixtures"]
                    if item["fixture_id"] == fixture_id
                ),
                None,
            )
            if entry is None:
                continue
            entry.update(
                {
                    "public_manifest_sha256": document_digest(manifest),
                    "public_payload_tree_sha256": payload_digest,
                    "seed_repository_tree_sha256": seed_digest,
                    "grader_contract_sha256": grader_digest,
                }
            )
            suite_path.write_text(
                json.dumps(suite, indent=2) + "\n", encoding="utf-8"
            )

    def test_checked_in_corpus_is_valid(self):
        result = validate(self.root)
        self.assertEqual(result["fixtures"], "3")

    def test_version_one_corpus_bytes_remain_frozen(self):
        paths = [self.root / "suite-v1.json"]
        paths.extend(sorted((self.root / "schemas").glob("*-v1.schema.json")))
        for fixture_id in (LOW, REVIEW):
            paths.extend(
                sorted(
                    path
                    for path in (self.root / "fixtures" / fixture_id).rglob("*")
                    if path.is_file()
                )
            )
        hasher = hashlib.sha256()
        hasher.update(b"evaluation-corpus-v1\0")
        hasher.update(len(paths).to_bytes(8, "big"))
        for path in sorted(paths):
            relative = path.relative_to(self.root).as_posix().encode("utf-8")
            content = path.read_bytes()
            hasher.update(len(relative).to_bytes(8, "big"))
            hasher.update(relative)
            hasher.update(len(content).to_bytes(8, "big"))
            hasher.update(content)
        self.assertEqual(hasher.hexdigest(), CORPUS_V1_BYTES_SHA256)

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

        self.reset_corpus()
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

        self.reset_corpus()
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
        self.assert_invalid("identity/version mismatch|dangling manifest reference")

        self.reset_corpus()
        self.rewrite_json(
            f"fixtures/{LOW}/public/manifest-v1.json",
            lambda value: value["grader"].update({"id": "wrong-grader"}),
        )
        self.assert_invalid("dangling grader reference")

    def test_digest_and_public_grader_binding_tampering_is_rejected(self):
        prompt = self.root / f"fixtures/{LOW}/public/payload/prompt.md"
        prompt.write_text(prompt.read_text() + "tamper\n", encoding="utf-8")
        self.assert_invalid("tampering or binding drift")

        self.reset_corpus()
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
        self.assert_invalid("missing or extra versioned schema documents")

    def test_version_two_identity_and_scenario_combinations_fail_closed(self):
        self.rewrite_json(
            "suite-v2.json",
            lambda value: value["fixtures"][2].update({"fixture_version": 1}),
        )
        self.assert_invalid("identity/version mismatch|invalid version 2 fixture")

        self.reset_corpus()
        self.rewrite_json(
            f"fixtures/{CHECKPOINT}/public/manifest-v2.json",
            lambda value: value.update({"scenario_kind": "low_risk_existing_tests"}),
        )
        self.assert_invalid("unsupported scenario kind")

    def test_private_asset_tampering_is_rejected(self):
        grader_asset = (
            self.root
            / f"fixtures/{CHECKPOINT}/grader/assets/grade_checkpoint.py"
        )
        grader_asset.write_text(
            grader_asset.read_text(encoding="utf-8") + "\n# tamper\n",
            encoding="utf-8",
        )
        self.assert_invalid("private grader asset binding drift")

    def test_checkpoint_fixture_rejects_partial_and_accepts_reference(self):
        fixture_root = self.root / "fixtures" / CHECKPOINT
        check = fixture_root / "grader/assets/grade_checkpoint.py"
        partial = fixture_root / "public/payload/seed"
        reference = fixture_root / "grader/assets/reference"
        failed = subprocess.run(
            [
                sys.executable,
                str(check),
                "--workspace",
                str(partial),
                "--check",
                "outcomes",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        for check_name in ("outcomes", "preservation", "contract", "scope"):
            with self.subTest(check=check_name):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(check),
                        "--workspace",
                        str(reference),
                        "--check",
                        check_name,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr or completed.stdout,
                )

    def run_checkpoint_check(self, workspace, check_name):
        check = (
            self.root
            / f"fixtures/{CHECKPOINT}/grader/assets/grade_checkpoint.py"
        )
        return subprocess.run(
            [
                sys.executable,
                str(check),
                "--workspace",
                str(workspace),
                "--check",
                check_name,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_checkpoint_grader_rejects_hard_coded_union_output(self):
        fixture_root = self.root / "fixtures" / CHECKPOINT
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            shutil.copytree(fixture_root / "grader/assets/reference", workspace)
            (workspace / "release_builder/render.py").write_text(
                'def render_report(checkpoint):\n'
                '    return "# Release readiness\\\\n\\\\nReady: 1\\\\nBlocked: 1'
                '\\\\nBlocked: 0\\\\nREL-2 REL-9 worker REL-7 REL-4\\\\n" '
                '+ str(checkpoint) + "\\\\n"\n',
                encoding="utf-8",
            )
            result = self.run_checkpoint_check(workspace, "outcomes")
            self.assertNotEqual(result.returncode, 0)

    def test_checkpoint_grader_rejects_symlinked_source(self):
        fixture_root = self.root / "fixtures" / CHECKPOINT
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            workspace = temporary_root / "workspace"
            shutil.copytree(fixture_root / "grader/assets/reference", workspace)
            source = workspace / "release_builder/render.py"
            external = temporary_root / "outside-render.py"
            external.write_bytes(source.read_bytes())
            source.unlink()
            source.symlink_to(external)
            result = self.run_checkpoint_check(workspace, "scope")
            self.assertNotEqual(result.returncode, 0)

    def test_checkpoint_grader_rejects_non_atomic_write_and_missing_tests(self):
        fixture_root = self.root / "fixtures" / CHECKPOINT
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            shutil.copytree(fixture_root / "grader/assets/reference", workspace)
            checkpoint = workspace / "release_builder/checkpoint.py"
            checkpoint.write_text(
                checkpoint.read_text(encoding="utf-8").replace(
                    '    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")\n'
                    "    temporary.write_bytes(content)\n"
                    "    os.replace(temporary, path)",
                    "    path.write_bytes(content)",
                ),
                encoding="utf-8",
            )
            result = self.run_checkpoint_check(workspace, "contract")
            self.assertNotEqual(result.returncode, 0)

            self.reset_corpus()
            fixture_root = self.root / "fixtures" / CHECKPOINT
            workspace = Path(temporary) / "missing-tests"
            shutil.copytree(fixture_root / "grader/assets/reference", workspace)
            (workspace / "tests/test_visible.py").unlink()
            result = self.run_checkpoint_check(workspace, "scope")
            self.assertNotEqual(result.returncode, 0)

    def test_held_out_details_cannot_leak_into_public_content(self):
        cases = (
            ("grader_tools/grade_review.py", "private command path"),
            ("blocking_finding_detected=true", "private expected result"),
            ("correctness-defect-found", "private check inventory"),
            ("private rubric: award credit", "private rubric"),
            ("seeded defect inventory", "private defect inventory"),
        )
        for leaked_text, label in cases:
            with self.subTest(label=label):
                prompt = self.root / f"fixtures/{REVIEW}/public/payload/prompt.md"
                prompt.write_text(leaked_text + "\n", encoding="utf-8")
                self.rebind_fixture(REVIEW)
                self.assert_invalid("held-out grader detail leaked")
                self.reset_corpus()

    def test_checkpoint_private_expectations_cannot_leak(self):
        for leaked_text in ("REL-9", "REL-7", "REL-4", "hold", "shipped", "migration"):
            with self.subTest(leaked_text=leaked_text):
                prompt = (
                    self.root
                    / f"fixtures/{CHECKPOINT}/public/payload/prompt.md"
                )
                prompt.write_text(
                    prompt.read_text(encoding="utf-8") + f"\n{leaked_text}\n",
                    encoding="utf-8",
                )
                self.rebind_fixture(CHECKPOINT)
                self.assert_invalid("held-out grader detail leaked")
                self.reset_corpus()

    def test_non_utf8_public_assets_are_rejected_even_when_rebound(self):
        leak = self.root / f"fixtures/{REVIEW}/public/payload/private.bin"
        leak.write_bytes(b"\xffgrader_tools/grade_review.py")
        self.rebind_fixture(REVIEW)
        self.assert_invalid("public assets must be UTF-8 text")

    def test_private_grader_paths_cannot_leak_as_public_asset_names(self):
        paths = (
            "grader_tools/grade_review.py",
            "grader-tools/grade-review.py",
        )
        for relative in paths:
            with self.subTest(relative=relative):
                leak = self.root / f"fixtures/{REVIEW}/public/payload" / relative
                leak.parent.mkdir()
                leak.write_text("# harmless content\n", encoding="utf-8")
                self.rebind_fixture(REVIEW)
                self.assert_invalid("held-out grader path leaked")
                self.reset_corpus()

    def test_sensitive_matching_uses_whole_token_boundaries(self):
        prompt = self.root / f"fixtures/{LOW}/public/payload/prompt.md"
        prompt.write_text("Make a focused change.\n", encoding="utf-8")
        self.rebind_fixture(LOW)
        self.assert_invalid("held-out grader detail leaked")

        self.reset_corpus()
        prompt = self.root / f"fixtures/{LOW}/public/payload/prompt.md"
        prompt.write_text("Avoid unfocused changes.\n", encoding="utf-8")
        self.rebind_fixture(LOW)
        validate(self.root)

        self.reset_corpus()
        notes = (
            self.root
            / f"fixtures/{REVIEW}/public/payload/upgrader_tools/notes.md"
        )
        notes.parent.mkdir()
        notes.write_text("Public upgrader documentation.\n", encoding="utf-8")
        self.rebind_fixture(REVIEW)
        validate(self.root)

    def test_nested_expected_result_strings_cannot_leak_when_rebound(self):
        private_result = "private-result-token-42"
        grader_path = self.root / f"fixtures/{REVIEW}/grader/grader-v1.json"
        grader = load_json(grader_path)
        grader["checks"][0]["expected"]["value"] = {
            "result_metadata": [{"private_token": private_result}]
        }
        grader_path.write_text(json.dumps(grader, indent=2) + "\n", encoding="utf-8")
        leak = self.root / f"fixtures/{REVIEW}/public/payload/result.txt"
        leak.write_text(private_result + "\n", encoding="utf-8")
        self.rebind_fixture(REVIEW)
        self.assert_invalid("held-out grader detail leaked")

    def test_required_grading_categories_cannot_be_diagnostic_only(self):
        grader_path = self.root / f"fixtures/{LOW}/grader/grader-v1.json"
        grader = load_json(grader_path)
        for check in grader["checks"]:
            check["required"] = False
        grader_path.write_text(json.dumps(grader, indent=2) + "\n", encoding="utf-8")
        self.rebind_fixture(LOW)
        self.assert_invalid("missing required grading categories")

        self.reset_corpus()
        grader_path = self.root / f"fixtures/{REVIEW}/grader/grader-v1.json"
        grader = load_json(grader_path)
        for check in grader["checks"]:
            if check["category"] == "seeded_defect_detection":
                check["required"] = False
        grader_path.write_text(json.dumps(grader, indent=2) + "\n", encoding="utf-8")
        self.rebind_fixture(REVIEW)
        self.assert_invalid("missing required grading categories")

    def test_non_json_numeric_constants_are_rejected(self):
        grader_path = self.root / f"fixtures/{LOW}/grader/grader-v1.json"
        text = grader_path.read_text(encoding="utf-8")
        grader_path.write_text(
            text.replace('"value": 0', '"value": NaN', 1), encoding="utf-8"
        )
        self.assert_invalid("non-JSON numeric constant")

        for constant in ("Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                self.reset_corpus()
                grader_path = self.root / f"fixtures/{LOW}/grader/grader-v1.json"
                text = grader_path.read_text(encoding="utf-8")
                grader_path.write_text(
                    text.replace('"value": 0', f'"value": {constant}', 1),
                    encoding="utf-8",
                )
                self.assert_invalid("non-JSON numeric constant")

    def test_symlink_assets_are_rejected(self):
        target = self.root / f"fixtures/{LOW}/public/payload/prompt.md"
        link = self.root / f"fixtures/{LOW}/public/payload/linked.md"
        link.symlink_to(target)
        self.assert_invalid("symlinks are not allowed")


if __name__ == "__main__":
    unittest.main()
