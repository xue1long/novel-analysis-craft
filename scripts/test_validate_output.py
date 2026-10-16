"""Regression checks for checkpoint integrity and bundled examples."""

import contextlib
import copy
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("validate_output", ROOT / "scripts/validate_output.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


# Schema-required fields per entity. test_passing_output_is_schema_valid removes each one
# and asserts the bundled schema rejects it, so this table cannot drift from the schema.
REQUIRED_PATH_FIELDS = (
    (("work",), ("title", "scope")),
    (("run",), ("mode", "status", "updated_at", "next_action")),
    (("coverage",), ("expected_chapter_ids", "received_chapter_ids", "processed_chapter_ids",
                     "pending_chapter_ids", "coverage_percent", "completeness",
                     "last_processed_chapter_id", "scope_note")),
    (("source_chunks", 0), ("chunk_id", "chapter_id", "sha256")),
    (("evidence", 0), ("evidence_id", "chapter_id", "chunk_id", "locator", "paraphrase")),
    (("stages", "skeleton"), ("synopsis", "one_sentence_story", "causal_beats", "structure_points")),
    (("stages", "flesh"), ("subplots", "characters")),
    (("stages", "soul"), ("themes", "emotional_arc", "strengths", "limitations")),
    (("stages", "technique"), ("technique_card_ids",)),
    (("stages", "practice"), ("selected_card_id", "sop_ids", "assignment", "draft_status",
                              "draft_evaluation")),
    (("stages", "practice", "assignment"), ("technique_card_id", "prompt", "target_length_min",
                                            "target_length_max", "rubric")),
    (("stages", "practice", "assignment", "rubric", 0), ("criterion", "score_0", "score_1", "score_2")),
    (("stages", "skeleton", "causal_beats", 0), ("beat_id", "event", "causal_link", "consequence",
                                                 "evidence_refs")),
    (("stages", "skeleton", "structure_points", 0), ("label", "event", "chapter_id", "evidence_refs")),
    (("stages", "flesh", "subplots", 0), ("name", "chapters", "characters", "function",
                                          "deletion_test", "evidence_refs")),
    (("stages", "flesh", "characters", 0), ("name", "role", "desire", "weakness_or_belief",
                                           "pressure_pattern", "arc", "evidence_refs")),
    (("stages", "soul", "themes", 0), ("proposition", "expression", "choice_or_cost", "evidence_refs")),
    (("stages", "soul", "emotional_arc", 0), ("from_state", "to_state", "turn_event",
                                              "plot_function", "evidence_refs")),
    (("claims", 0), ("claim_id", "stage", "kind", "statement", "confidence", "evidence_refs")),
    (("technique_cards", 0), ("card_id", "title", "principle", "observed_action", "reader_effect",
                              "mechanism", "counterfactual", "conditions", "adaptation", "pitfalls",
                              "evidence_refs", "confidence", "sop_id")),
    (("sops", 0), ("sop_id", "name", "technique_card_id", "goal", "use_when", "steps", "checks",
                   "failure_signals", "originality_note")),
    (("correction_log", 0), ("entity_id", "reason", "previous_statement", "revised_statement",
                             "evidence_refs")),
    (("quality_review",), ("strengths", "limitations", "unresolved_questions", "checklist")),
    (("quality_review", "checklist"), ("evidence_resolved", "coverage_honest",
                                       "five_stages_reported", "practice_status_honest")),
)

ENUM_MUTATIONS = (
    (("run", "mode"), "ultra"),
    (("run", "status"), "finished"),
    (("work", "scope"), "whatever"),
    (("coverage", "completeness"), "done"),
    (("claims", 0, "kind"), "guess"),
    (("claims", 0, "confidence"), "sure"),
    (("technique_cards", 0, "confidence"), "sure"),
    (("stages", "skeleton", "structure_points", 0, "label"), "intro"),
    (("stages", "practice", "draft_status"), "done"),
    (("stages", "skeleton", "status"), "done"),
    (("stages", "skeleton", "gate"), "maybe"),
)


def node_at(document, path):
    node = document
    for step in path:
        node = node[step]
    return node


class ValidateOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads((ROOT / "examples/quick-demo.output.json").read_text(encoding="utf-8"))

    def check(self, output, new_input=None):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "output.json"
            path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
            args = ["validate_output.py", str(path)]
            if new_input is not None:
                incoming = Path(folder) / "input.json"
                incoming.write_text(json.dumps(new_input, ensure_ascii=False), encoding="utf-8")
                args += ["--input", str(incoming)]
            stream = io.StringIO()
            with patch.object(sys, "argv", args), contextlib.redirect_stdout(stream):
                status = validator.main()
            return status, stream.getvalue()

    def clear_evidence_refs(self, node):
        """Empty every evidence_refs array in place on a copied fixture."""
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "evidence_refs":
                    node[key] = []
                else:
                    self.clear_evidence_refs(value)
        elif isinstance(node, list):
            for item in node:
                self.clear_evidence_refs(item)

    def test_bundled_outputs_pass(self):
        for path in ROOT.glob("examples/**/*.output.json"):
            with self.subTest(path=path.name):
                status, message = self.check(json.loads(path.read_text(encoding="utf-8")))
                self.assertEqual(status, 0, message)

    def test_evidence_must_match_source_chunk(self):
        output = copy.deepcopy(self.base)
        output["evidence"][0]["chapter_id"] = "ch99"
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("chapter differs", message)

    def test_zero_coverage_cannot_be_complete(self):
        output = copy.deepcopy(self.base)
        output["coverage"].update(processed_chapter_ids=[], pending_chapter_ids=["ch01", "ch02", "ch03"], coverage_percent=0, completeness="partial", last_processed_chapter_id=None)
        output["run"]["status"] = "complete"
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("no processed chapters", message)

    def partial_coverage_output(self, percent):
        """Base output narrowed to two of three processed chapters, percent as declared."""
        output = copy.deepcopy(self.base)
        output["coverage"].update(processed_chapter_ids=["ch01", "ch02"],
                                  pending_chapter_ids=["ch03"],
                                  coverage_percent=percent,
                                  completeness="partial",
                                  last_processed_chapter_id="ch02")
        output["run"]["status"] = "partial"
        return output

    def test_coverage_percent_precision_is_enforced(self):
        """Two of three chapters is 66.7%; the old ±0.1 slack accepted the wrong 66.6."""
        cases = ((66.7, True), (66.67, True), (66.6667, True), (66.6, False), (67, False))
        for percent, accepted in cases:
            with self.subTest(percent=percent):
                status, message = self.check(self.partial_coverage_output(percent))
                self.assertEqual(status, 0 if accepted else 1, message)
                if not accepted:
                    self.assertIn("coverage_percent should be 66.7", message)

    def test_title_only_scope_conflicts_with_declared_chapters(self):
        output = copy.deepcopy(self.base)
        output["work"]["scope"] = "title_only"
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("work.scope title_only", message)

    def test_title_only_analysis_can_finish_with_tentative_claims(self):
        output = copy.deepcopy(self.base)
        output["work"]["scope"] = "title_only"
        output["source_chunks"] = []
        output["evidence"] = []
        output["coverage"].update(expected_chapter_ids=[], received_chapter_ids=[], processed_chapter_ids=[], pending_chapter_ids=[], coverage_percent=None, completeness="unknown", last_processed_chapter_id=None)
        output["run"]["status"] = "complete"
        for claim in output["claims"]:
            claim.update(kind="inference", confidence="low", evidence_refs=[])
        for card in output["technique_cards"]:
            card.update(confidence="low", evidence_refs=[])
        # Without visible source there are no evidence IDs, so no stage may cite one.
        self.clear_evidence_refs(output["stages"])
        status, message = self.check(output)
        self.assertEqual(status, 0, message)
        if importlib.util.find_spec("jsonschema"):
            import jsonschema
            schema = json.loads((ROOT / "references/schemas/output.schema.json").read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(schema).validate(output)

    def test_unsourced_observation_is_rejected(self):
        output = copy.deepcopy(self.base)
        output["claims"][0]["evidence_refs"] = []
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("low-confidence inference or evaluation", message)

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "optional jsonschema package unavailable")
    def test_title_only_input_needs_no_rights_or_source(self):
        import jsonschema

        schema = json.loads((ROOT / "references/schemas/input.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate({
            "schema_version": "1.0", "work": {"title": "A Novel"},
            "analysis": {"mode": "quick", "scope": "title_only"},
        })

    def test_missing_schema_required_next_action_fails(self):
        output = copy.deepcopy(self.base)
        del output["run"]["next_action"]
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("run.next_action", message)

    def test_resume_rejects_other_work_and_changed_chunk(self):
        incoming = json.loads((ROOT / "examples/quick-demo-resume.input.json").read_text(encoding="utf-8"))
        wrong_work = copy.deepcopy(incoming)
        wrong_work["work"]["title"] = "另一部作品"
        status, message = self.check(self.base, wrong_work)
        self.assertEqual(status, 1)
        self.assertIn("work.title differs", message)

        changed_chunk = copy.deepcopy(incoming)
        changed_chunk["source"]["chunks"][0]["chunk_id"] = "c01"
        status, message = self.check(self.base, changed_chunk)
        self.assertEqual(status, 1)
        self.assertIn("reuses chunk ID", message)

    def test_nested_stage_evidence_refs_are_checked(self):
        cases = (("skeleton", "causal_beats"), ("skeleton", "structure_points"),
                 ("flesh", "subplots"), ("flesh", "characters"),
                 ("soul", "themes"), ("soul", "emotional_arc"))
        for stage, field in cases:
            with self.subTest(field=f"{stage}.{field}"):
                output = copy.deepcopy(self.base)
                output["stages"][stage][field][0]["evidence_refs"] = ["E999"]
                status, message = self.check(output)
                self.assertEqual(status, 1)
                self.assertIn(f"stages.{stage}.{field}[0].evidence_refs refers to missing evidence ID 'E999'", message)

    def test_correction_log_evidence_refs_are_checked(self):
        output = copy.deepcopy(self.base)
        output["correction_log"] = [{
            "entity_id": "C001", "reason": "new evidence", "previous_statement": "old",
            "revised_statement": "new", "evidence_refs": ["E999"],
        }]
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("correction_log[0].evidence_refs refers to missing evidence ID 'E999'", message)

    def test_correction_log_unknown_entity_only_warns(self):
        output = copy.deepcopy(self.base)
        output["correction_log"] = [{
            "entity_id": "C999", "reason": "new evidence", "previous_statement": "old",
            "revised_statement": "new", "evidence_refs": ["E001"],
        }]
        status, message = self.check(output)
        self.assertEqual(status, 0, message)
        self.assertIn("WARNING", message)
        self.assertIn("matches no claim", message)

    def test_correction_log_required_fields(self):
        output = copy.deepcopy(self.base)
        output["correction_log"] = [{"entity_id": "C001"}]
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("reason", message)
        self.assertIn("revised_statement", message)

    def test_technique_card_required_fields(self):
        output = copy.deepcopy(self.base)
        for field in ("title", "principle", "observed_action", "reader_effect",
                      "conditions", "pitfalls", "mechanism"):
            del output["technique_cards"][0][field]
        status, message = self.check(output)
        self.assertEqual(status, 1)
        for field in ("title", "principle", "observed_action", "reader_effect",
                      "conditions", "pitfalls", "mechanism"):
            self.assertIn(f"technique card TC001.{field}", message)

    def test_claim_required_fields(self):
        output = copy.deepcopy(self.base)
        output["claims"][0] = {"claim_id": "C001", "stage": "skeleton", "evidence_refs": ["E001"]}
        status, message = self.check(output)
        self.assertEqual(status, 1)
        for field in ("statement", "kind", "confidence"):
            self.assertIn(f"claim C001.{field}", message)

    def test_sop_required_fields(self):
        output = copy.deepcopy(self.base)
        for field in ("name", "goal", "use_when", "steps", "checks", "originality_note"):
            del output["sops"][0][field]
        status, message = self.check(output)
        self.assertEqual(status, 1)
        for field in ("name", "goal", "use_when", "steps", "checks", "originality_note"):
            self.assertIn(field, message)

    def test_quality_review_structure(self):
        output = copy.deepcopy(self.base)
        output["quality_review"] = []
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("quality_review must be an object", message)

        output = copy.deepcopy(self.base)
        del output["quality_review"]["checklist"]["coverage_honest"]
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("quality_review.checklist.coverage_honest", message)

    def test_practice_cross_references(self):
        output = copy.deepcopy(self.base)
        output["stages"]["practice"]["selected_card_id"] = "TC999"
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("stages.practice.selected_card_id", message)

        output = copy.deepcopy(self.base)
        output["stages"]["practice"]["sop_ids"] = ["SOP999"]
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("stages.practice.sop_ids", message)

    def test_rubric_needs_three_criteria(self):
        output = copy.deepcopy(self.base)
        output["stages"]["practice"]["assignment"]["rubric"] = \
            output["stages"]["practice"]["assignment"]["rubric"][:1]
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("rubric needs at least 3 criteria", message)

    def test_assignment_length_bounds(self):
        output = copy.deepcopy(self.base)
        output["stages"]["practice"]["assignment"]["target_length_min"] = 300
        output["stages"]["practice"]["assignment"]["target_length_max"] = 2000
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("target_length_min", message)
        self.assertIn("target_length_max", message)

    def test_enum_values_are_checked(self):
        cases = (("run", "mode", "ultra"), ("run", "status", "finished"),
                 ("work", "scope", "whatever"), ("coverage", "completeness", "done"))
        for section, field, value in cases:
            with self.subTest(field=f"{section}.{field}"):
                output = copy.deepcopy(self.base)
                output[section][field] = value
                status, message = self.check(output)
                self.assertEqual(status, 1, f"{section}.{field}={value} should fail")
                self.assertIn(f"{section}.{field}", message)

        output = copy.deepcopy(self.base)
        output["claims"][0]["confidence"] = "sure"
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("confidence", message)

    def test_stage_content_required_fields(self):
        output = copy.deepcopy(self.base)
        del output["stages"]["flesh"]["characters"][0]["pressure_pattern"]
        del output["stages"]["soul"]["themes"][0]["choice_or_cost"]
        del output["stages"]["skeleton"]["structure_points"][0]["event"]
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("stages.flesh.characters[0].pressure_pattern", message)
        self.assertIn("stages.soul.themes[0].choice_or_cost", message)
        self.assertIn("stages.skeleton.structure_points[0].event", message)

    def test_structure_point_label_must_be_known(self):
        output = copy.deepcopy(self.base)
        output["stages"]["skeleton"]["structure_points"][0]["label"] = "intro"
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("stages.skeleton.structure_points[0].label", message)

    def test_unfinished_stage_may_hold_blank_values(self):
        output = copy.deepcopy(self.base)
        output["stages"]["skeleton"].update(
            status="not_started", gate="not_checked", synopsis="", one_sentence_story="",
            causal_beats=[], structure_points=[])
        output["stages"]["practice"].update(
            status="not_started", gate="not_checked", sop_ids=[], assignment=None,
            draft_status="not_submitted", draft_evaluation=None)
        status, message = self.check(output)
        self.assertEqual(status, 0, message)

    def test_optional_lists_may_stay_empty_when_stage_is_complete(self):
        output = copy.deepcopy(self.base)
        output["stages"]["flesh"]["subplots"] = []
        output["stages"]["soul"]["strengths"] = []
        output["stages"]["soul"]["limitations"] = []
        status, message = self.check(output)
        self.assertEqual(status, 0, message)

    def test_complete_stage_cannot_report_empty_core_content(self):
        output = copy.deepcopy(self.base)
        output["stages"]["skeleton"]["causal_beats"] = []
        output["stages"]["soul"]["themes"] = []
        output["stages"]["technique"]["technique_card_ids"] = []
        status, message = self.check(output)
        self.assertEqual(status, 1)
        self.assertIn("stages.skeleton.causal_beats must not be empty in a complete stage", message)
        self.assertIn("stages.soul.themes must not be empty in a complete stage", message)
        self.assertIn("stages.technique.technique_card_ids must not be empty", message)

    def test_bundled_resume_pair_passes(self):
        checkpoint = json.loads((ROOT / "examples/quick-demo-resume.output.json").read_text(encoding="utf-8"))
        incoming = json.loads((ROOT / "examples/quick-demo-resume.input.json").read_text(encoding="utf-8"))
        status, message = self.check(checkpoint, incoming)
        self.assertEqual(status, 0, message)

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "optional jsonschema package unavailable")
    def test_passing_output_is_schema_valid(self):
        """A green run must never hide output the bundled schema rejects."""
        import jsonschema
        schema = json.loads((ROOT / "references/schemas/output.schema.json").read_text(encoding="utf-8"))
        document_validator = jsonschema.Draft202012Validator(schema)
        mutations = [(f"{'.'.join(map(str, path))}.{field} removed", path, field, None)
                     for path, fields in REQUIRED_PATH_FIELDS for field in fields]
        mutations += [(f"{'.'.join(map(str, path))} = {value!r}", path[:-1], path[-1], value)
                      for path, value in ENUM_MUTATIONS]
        for label, parent_path, key, value in mutations:
            with self.subTest(mutation=label):
                output = copy.deepcopy(self.base)
                output["correction_log"] = [{
                    "entity_id": "C001", "reason": "new evidence", "previous_statement": "old",
                    "revised_statement": "new", "evidence_refs": ["E001"],
                }]
                node = node_at(output, parent_path)
                if value is None:
                    del node[key]
                else:
                    node[key] = value
                self.assertTrue(list(document_validator.iter_errors(output)),
                                f"{label}: the bundled schema does not require this, fix the table")
                status, message = self.check(output)
                self.assertEqual(status, 1, f"{label}: validator accepted output the schema rejects\n{message}")

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "optional jsonschema package unavailable")
    def test_bundled_examples_match_json_schema(self):
        import jsonschema

        for kind in ("input", "output"):
            schema = json.loads((ROOT / f"references/schemas/{kind}.schema.json").read_text(encoding="utf-8"))
            for path in ROOT.glob(f"examples/**/*.{kind}.json"):
                with self.subTest(path=path.name):
                    jsonschema.Draft202012Validator(schema).validate(json.loads(path.read_text(encoding="utf-8")))


class DocumentationConsistencyTests(unittest.TestCase):
    """Guard the SKILL.md rules the validator and the references depend on."""

    @classmethod
    def setUpClass(cls):
        cls.skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

    def test_antipattern_table_numbers_are_contiguous(self):
        """Item 5: the blacklist rows are the skill's index into its own history."""
        cells = [line.split("|")[1].strip() for line in self.skill.splitlines()
                 if line.startswith("| ")]
        numbers = [int(cell) for cell in cells if cell.isdigit()]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_section_references_resolve_to_headings(self):
        """Item 4: a §-reference must name a heading that exists, not a remembered one."""
        headings = set(re.findall(r"^## (\d+)\.", self.skill, re.MULTILINE))
        for reference in re.findall(r"§ ?(\d+(?:\.\d+)?)", self.skill):
            with self.subTest(reference=f"§{reference}"):
                self.assertIn(reference, headings)


if __name__ == "__main__":
    unittest.main()
