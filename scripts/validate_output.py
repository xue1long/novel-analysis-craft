#!/usr/bin/env python3
"""Check novel-analysis output structure, coverage, and evidence links."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


REQUIRED_TOP = {
    "schema_version", "work", "run", "coverage", "evidence", "stages",
    "claims", "technique_cards", "sops", "correction_log", "quality_review",
    "source_chunks",
}
STAGE_NAMES = {"skeleton", "flesh", "soul", "technique", "practice"}
VALID_STAGE_STATUS = {"not_started", "partial", "complete", "blocked"}
VALID_GATES = {"not_checked", "pass", "revise", "blocked"}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")

# Controlled vocabularies, mirroring references/schemas/output.schema.json.
WORK_SCOPES = {"complete_work", "selected_chapters", "excerpt", "notes", "title_only"}
RUN_MODES = {"quick", "standard", "deep"}
RUN_STATUSES = {"in_progress", "partial", "complete", "blocked"}
COMPLETENESS_VALUES = {"complete", "partial", "unknown"}
CLAIM_KINDS = {"observation", "inference", "evaluation"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
DRAFT_STATUSES = {"not_submitted", "submitted_not_evaluated", "evaluated"}
STRUCTURE_LABELS = {"opening", "setup", "climax", "ending", "unknown"}

ASSIGNMENT_MIN_LENGTH = 500
ASSIGNMENT_MAX_LENGTH = 1500
RUBRIC_MIN_CRITERIA = 3
COVERAGE_PERCENT_DECIMALS = 1

# Required stage payloads. Presence and type are checked for every stage; the kinds
# "string", "string_array", and "required_array" additionally demand substance once a
# stage is marked complete. "array" and "optional_string_array" stay empty-able because
# the workflow allows "none evidenced" (e.g. no subplot, no limitation worth listing).
STAGE_FIELD_KINDS = {
    "skeleton": {
        "synopsis": "string",
        "one_sentence_story": "string",
        "causal_beats": "required_array",
        "structure_points": "required_array",
    },
    "flesh": {"subplots": "array", "characters": "required_array"},
    "soul": {
        "themes": "required_array",
        "emotional_arc": "required_array",
        "strengths": "optional_string_array",
        "limitations": "optional_string_array",
    },
    "technique": {"technique_card_ids": "string_array"},
    "practice": {
        "selected_card_id": "nullable_string",
        "sop_ids": "string_array",
        "assignment": "object_or_null",
        "draft_status": "string",
        "draft_evaluation": "object_or_null",
    },
}
STAGE_ITEM_STRING_FIELDS = {
    ("skeleton", "causal_beats"): ("beat_id", "event", "causal_link", "consequence"),
    ("skeleton", "structure_points"): ("event",),
    ("flesh", "subplots"): ("name", "function", "deletion_test"),
    ("flesh", "characters"): ("name", "role", "desire", "weakness_or_belief",
                              "pressure_pattern", "arc"),
    ("soul", "themes"): ("proposition", "expression", "choice_or_cost"),
    ("soul", "emotional_arc"): ("from_state", "to_state", "turn_event", "plot_function"),
}
STAGE_ITEM_ARRAY_FIELDS = {
    ("flesh", "subplots"): ("chapters", "characters"),
}
STAGE_ITEM_NULLABLE_FIELDS = {
    ("skeleton", "structure_points"): ("chapter_id",),
}
CLAIM_STRING_FIELDS = ("statement",)
CARD_STRING_FIELDS = ("title", "principle", "observed_action", "reader_effect",
                      "counterfactual", "conditions", "adaptation", "pitfalls")
CARD_ARRAY_FIELDS = ("mechanism",)
SOP_STRING_FIELDS = ("name", "technique_card_id", "goal", "use_when", "originality_note")
SOP_ARRAY_FIELDS = ("steps", "checks", "failure_signals")
QUALITY_REVIEW_ARRAY_FIELDS = ("strengths", "limitations", "unresolved_questions")
QUALITY_CHECKLIST_FIELDS = ("evidence_resolved", "coverage_honest",
                            "five_stages_reported", "practice_status_honest")
CORRECTION_LOG_FIELDS = ("entity_id", "reason", "previous_statement", "revised_statement")
RUBRIC_FIELDS = ("criterion", "score_0", "score_1", "score_2")


def unique_values(values, label, errors):
    if len(values) != len(set(values)):
        errors.append(f"{label} contains duplicate IDs.")


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def require_strings(item, label, fields, errors, strict=True):
    """Every named field must hold a string; strict rejects blank values."""
    expectation = "a non-empty string" if strict else "a string"
    for field in fields:
        value = item.get(field)
        if not isinstance(value, str) or (strict and not value.strip()):
            errors.append(f"{label}.{field} must be {expectation}.")


def require_string_arrays(item, label, fields, errors, non_empty=False):
    for field in fields:
        value = item.get(field)
        if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
            errors.append(f"{label}.{field} must be an array of strings.")
        elif non_empty and not value:
            errors.append(f"{label}.{field} must not be empty.")


def require_enum(item, label, field, allowed, errors):
    if item.get(field) not in allowed:
        errors.append(f"{label}.{field} must be one of {', '.join(sorted(allowed))}.")


def require_field_kind(item, label, field, kind, errors, strict=True):
    if field not in item:
        errors.append(f"{label}.{field} is required.")
        return
    value = item[field]
    if kind == "string":
        require_strings(item, label, (field,), errors, strict=strict)
    elif kind == "string_array":
        require_string_arrays(item, label, (field,), errors, non_empty=strict)
    elif kind == "optional_string_array":
        require_string_arrays(item, label, (field,), errors)
    elif kind in ("array", "required_array"):
        if not isinstance(value, list):
            errors.append(f"{label}.{field} must be an array.")
        elif strict and kind == "required_array" and not value:
            errors.append(f"{label}.{field} must not be empty in a complete stage.")
    elif kind == "nullable_string" and value is not None and not isinstance(value, str):
        errors.append(f"{label}.{field} must be a string or null.")
    elif kind == "object_or_null" and value is not None and not isinstance(value, dict):
        errors.append(f"{label}.{field} must be an object or null.")


def require_citation_field(item, label, errors):
    """Citation-bearing objects must state evidence_refs, even as an empty list."""
    if "evidence_refs" not in item:
        errors.append(f"{label}.evidence_refs is required (use [] when nothing is cited).")
        return
    require_string_arrays(item, label, ("evidence_refs",), errors)


def check_refs(refs, label, evidence_set, errors):
    """label names the citing object; its evidence_refs field is reported by path."""
    field = f"{label}.evidence_refs"
    if not isinstance(refs, list):
        errors.append(f"{field} must be an array.")
        return
    for ref in refs:
        if not isinstance(ref, str) or ref not in evidence_set:
            errors.append(f"{field} refers to missing evidence ID {ref!r}.")


def iter_evidence_refs(node, path):
    """Yield (owner path, refs) for every evidence_refs array so nested citations cannot hide."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "evidence_refs":
                yield path, value
            else:
                yield from iter_evidence_refs(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from iter_evidence_refs(item, f"{path}[{index}]")


def check_claims(claims, evidence_set, errors):
    claim_ids = []
    for claim in claims:
        if not isinstance(claim, dict):
            errors.append("Each claim must be an object.")
            continue
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id.strip():
            errors.append("Claim is missing a non-empty claim_id.")
        else:
            claim_ids.append(claim_id)
        label = f"claim {claim_id if isinstance(claim_id, str) else '?'}"
        require_enum(claim, label, "stage", STAGE_NAMES, errors)
        require_enum(claim, label, "kind", CLAIM_KINDS, errors)
        require_enum(claim, label, "confidence", CONFIDENCE_LEVELS, errors)
        require_strings(claim, label, CLAIM_STRING_FIELDS, errors)
        check_refs(claim.get("evidence_refs"), label, evidence_set, errors)
        if claim.get("evidence_refs") == [] and (claim.get("kind") == "observation"
                                                 or claim.get("confidence") != "low"):
            errors.append(f"{label} without source evidence must be a low-confidence inference or evaluation.")
    unique_values(claim_ids, "claims.claim_id", errors)
    return claim_ids


def check_cards(cards, evidence_set, errors):
    card_ids = []
    for card in cards:
        if not isinstance(card, dict):
            errors.append("Each technique card must be an object.")
            continue
        card_id = card.get("card_id")
        if not isinstance(card_id, str) or not card_id.strip():
            errors.append("Technique card is missing a non-empty card_id.")
        else:
            card_ids.append(card_id)
        label = f"technique card {card_id if isinstance(card_id, str) else '?'}"
        require_strings(card, label, CARD_STRING_FIELDS, errors)
        require_string_arrays(card, label, CARD_ARRAY_FIELDS, errors, non_empty=True)
        require_enum(card, label, "confidence", CONFIDENCE_LEVELS, errors)
        check_refs(card.get("evidence_refs"), label, evidence_set, errors)
        if card.get("evidence_refs") == [] and card.get("confidence") != "low":
            errors.append(f"{label} without source evidence must have low confidence.")
        require_field_kind(card, label, "sop_id", "nullable_string", errors)
    unique_values(card_ids, "technique_cards.card_id", errors)
    return card_ids


def check_sops(sops, card_set, errors):
    sop_ids = []
    for sop in sops:
        if not isinstance(sop, dict):
            errors.append("Each SOP must be an object.")
            continue
        sop_id = sop.get("sop_id")
        if not isinstance(sop_id, str) or not sop_id.strip():
            errors.append("SOP is missing a non-empty sop_id.")
        else:
            sop_ids.append(sop_id)
        label = f"SOP {sop_id if isinstance(sop_id, str) else '?'}"
        require_strings(sop, label, SOP_STRING_FIELDS, errors)
        require_string_arrays(sop, label, SOP_ARRAY_FIELDS, errors, non_empty=True)
        if not isinstance(sop.get("technique_card_id"), str) or sop["technique_card_id"] not in card_set:
            errors.append(f"{label} refers to a missing technique card.")
    unique_values(sop_ids, "sops.sop_id", errors)
    return sop_ids


def check_stage_payloads(stages, errors):
    """Required stage fields and nested item fields; values must be substantive once complete."""
    for name, fields in STAGE_FIELD_KINDS.items():
        stage = stages.get(name)
        if not isinstance(stage, dict):
            continue
        strict = stage.get("status") == "complete"
        for field, kind in fields.items():
            require_field_kind(stage, f"stages.{name}", field, kind, errors, strict=strict)
    for (name, field), required in STAGE_ITEM_STRING_FIELDS.items():
        stage = stages.get(name)
        items = stage.get(field) if isinstance(stage, dict) else None
        if not isinstance(items, list):
            continue
        strict = stage.get("status") == "complete"
        for index, item in enumerate(items):
            label = f"stages.{name}.{field}[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{label} must be an object.")
                continue
            require_strings(item, label, required, errors, strict=strict)
            require_string_arrays(item, label, STAGE_ITEM_ARRAY_FIELDS.get((name, field), ()),
                                  errors, non_empty=strict)
            require_citation_field(item, label, errors)
            for nullable in STAGE_ITEM_NULLABLE_FIELDS.get((name, field), ()):
                require_field_kind(item, label, nullable, "nullable_string", errors)
    skeleton = stages.get("skeleton")
    points = skeleton.get("structure_points") if isinstance(skeleton, dict) else None
    if isinstance(points, list):
        for index, point in enumerate(points):
            if isinstance(point, dict):
                require_enum(point, f"stages.skeleton.structure_points[{index}]",
                             "label", STRUCTURE_LABELS, errors)


def check_practice(practice, card_set, sop_set, errors):
    if not isinstance(practice, dict):
        return
    require_enum(practice, "stages.practice", "draft_status", DRAFT_STATUSES, errors)
    selected = practice.get("selected_card_id")
    if selected is not None and (not isinstance(selected, str) or selected not in card_set):
        errors.append("stages.practice.selected_card_id must be null or refer to an existing technique card.")
    listed_sops = practice.get("sop_ids")
    if isinstance(listed_sops, list) and any(not isinstance(item, str) or item not in sop_set
                                            for item in listed_sops):
        errors.append("stages.practice.sop_ids must refer to existing SOPs.")
    assignment = practice.get("assignment")
    if assignment is None:
        return
    if not isinstance(assignment, dict):
        return
    label = "stages.practice.assignment"
    require_strings(assignment, label, ("technique_card_id", "prompt"), errors)
    if not isinstance(assignment.get("technique_card_id"), str) or assignment["technique_card_id"] not in card_set:
        errors.append("Practice assignment refers to a missing technique card.")
    minimum = assignment.get("target_length_min")
    maximum = assignment.get("target_length_max")
    if not is_number(minimum) or minimum < ASSIGNMENT_MIN_LENGTH:
        errors.append(f"{label}.target_length_min must be a number >= {ASSIGNMENT_MIN_LENGTH}.")
    if not is_number(maximum) or maximum > ASSIGNMENT_MAX_LENGTH:
        errors.append(f"{label}.target_length_max must be a number <= {ASSIGNMENT_MAX_LENGTH}.")
    if is_number(minimum) and is_number(maximum) and maximum < minimum:
        errors.append(f"{label}.target_length_max must not be below target_length_min.")
    rubric = assignment.get("rubric")
    if not isinstance(rubric, list) or len(rubric) < RUBRIC_MIN_CRITERIA:
        errors.append(f"{label}.rubric needs at least {RUBRIC_MIN_CRITERIA} criteria.")
    else:
        for index, criterion in enumerate(rubric):
            if not isinstance(criterion, dict):
                errors.append(f"{label}.rubric[{index}] must be an object.")
                continue
            require_strings(criterion, f"{label}.rubric[{index}]", RUBRIC_FIELDS, errors)
    if practice.get("draft_status") == "evaluated" and practice.get("draft_evaluation") is None:
        errors.append("Practice draft is marked evaluated but draft_evaluation is null.")


def check_quality_review(review, errors):
    if not isinstance(review, dict):
        errors.append("quality_review must be an object.")
        return
    require_string_arrays(review, "quality_review", QUALITY_REVIEW_ARRAY_FIELDS, errors)
    checklist = review.get("checklist")
    if not isinstance(checklist, dict):
        errors.append("quality_review.checklist must be an object.")
        return
    for field in QUALITY_CHECKLIST_FIELDS:
        if not isinstance(checklist.get(field), bool):
            errors.append(f"quality_review.checklist.{field} must be a boolean.")


def check_correction_log(entries, evidence_set, known_ids, errors, warnings):
    for index, entry in enumerate(entries):
        label = f"correction_log[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"Each {label} entry must be an object.")
            continue
        require_strings(entry, label, CORRECTION_LOG_FIELDS, errors)
        check_refs(entry.get("evidence_refs"), label, evidence_set, errors)
        entity_id = entry.get("entity_id")
        if isinstance(entity_id, str) and entity_id.strip() and entity_id not in known_ids:
            warnings.append(f"{label}.entity_id {entity_id!r} matches no claim, technique card, SOP, or evidence ID.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file", type=Path, help="Analysis output JSON file")
    parser.add_argument("--input", type=Path, help="New input JSON to check against this checkpoint before resuming")
    args = parser.parse_args()

    try:
        data = json.loads(args.json_file.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        print(f"ERROR: cannot read {args.json_file}: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}", file=sys.stderr)
        return 2

    errors = []
    warnings = []
    if not isinstance(data, dict):
        print("ERROR: top-level JSON value must be an object.")
        return 2

    missing = sorted(REQUIRED_TOP - set(data))
    if missing:
        errors.append("Missing top-level fields: " + ", ".join(missing))
    if data.get("schema_version") != "1.1":
        errors.append("schema_version must be 1.1; re-ingest source chunks for older checkpoints.")

    work = data.get("work")
    if not isinstance(work, dict) or not isinstance(work.get("title"), str) or not work["title"].strip():
        errors.append("work.title must be a non-empty string.")
    if isinstance(work, dict):
        require_enum(work, "work", "scope", WORK_SCOPES, errors)
    run = data.get("run")
    if not isinstance(run, dict):
        errors.append("run must be an object.")
        run = {}
    for field in ("mode", "status", "updated_at", "next_action"):
        if not isinstance(run.get(field), str):
            errors.append(f"run.{field} must be a string.")
    require_enum(run, "run", "mode", RUN_MODES, errors)
    require_enum(run, "run", "status", RUN_STATUSES, errors)

    coverage = data.get("coverage", {})
    evidence = data.get("evidence", [])
    source_chunks = data.get("source_chunks", [])
    stages = data.get("stages", {})
    claims = data.get("claims", [])
    cards = data.get("technique_cards", [])
    sops = data.get("sops", [])
    correction_log = data.get("correction_log", [])
    quality_review = data.get("quality_review")

    if not isinstance(coverage, dict):
        coverage = {}
        errors.append("coverage must be an object.")
    else:
        require_enum(coverage, "coverage", "completeness", COMPLETENESS_VALUES, errors)
        require_strings(coverage, "coverage", ("scope_note",), errors)
    if not isinstance(evidence, list):
        evidence = []
        errors.append("evidence must be an array.")
    if not isinstance(source_chunks, list):
        source_chunks = []
        errors.append("source_chunks must be an array.")
    if not isinstance(stages, dict):
        stages = {}
        errors.append("stages must be an object.")
    if not isinstance(claims, list):
        claims = []
        errors.append("claims must be an array.")
    if not isinstance(cards, list):
        cards = []
        errors.append("technique_cards must be an array.")
    if not isinstance(sops, list):
        sops = []
        errors.append("sops must be an array.")
    if not isinstance(correction_log, list):
        correction_log = []
        errors.append("correction_log must be an array.")

    chapter_of_chunk = {}
    for chunk in source_chunks:
        if not isinstance(chunk, dict):
            errors.append("Each source chunk must be an object.")
            continue
        chunk_id = chunk.get("chunk_id")
        chapter_id = chunk.get("chapter_id")
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            errors.append("Source chunk is missing a non-empty chunk_id.")
            continue
        if chunk_id in chapter_of_chunk:
            errors.append(f"source_chunks contains duplicate chunk ID {chunk_id!r}.")
        if not isinstance(chapter_id, str) or not chapter_id.strip():
            errors.append(f"Source chunk {chunk_id!r} is missing a non-empty chapter_id.")
        if not isinstance(chunk.get("sha256"), str) or not SHA256.fullmatch(chunk["sha256"]):
            errors.append(f"Source chunk {chunk_id!r} needs a lowercase SHA-256 content hash.")
        chapter_of_chunk[chunk_id] = chapter_id

    if args.input is not None:
        try:
            new_input = json.loads(args.input.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Cannot read new input JSON: {exc}")
        else:
            input_work = new_input.get("work", {}) if isinstance(new_input, dict) else {}
            if not isinstance(input_work, dict) or input_work.get("title") != (work if isinstance(work, dict) else {}).get("title"):
                errors.append("New input work.title differs from checkpoint work.title.")
            input_source = new_input.get("source", {}) if isinstance(new_input, dict) else {}
            input_chunks = input_source.get("chunks", []) if isinstance(input_source, dict) else []
            if not isinstance(input_chunks, list) or not input_chunks:
                errors.append("New input source.chunks must be a non-empty array.")
            else:
                old_chunks = {chunk["chunk_id"]: chunk for chunk in source_chunks
                              if isinstance(chunk, dict) and isinstance(chunk.get("chunk_id"), str)}
                seen_new = set()
                for chunk in input_chunks:
                    if not isinstance(chunk, dict) or not all(isinstance(chunk.get(key), str) for key in ("chunk_id", "chapter_id", "content")):
                        errors.append("Each new input chunk needs chunk_id, chapter_id, and content strings.")
                        continue
                    chunk_id = chunk["chunk_id"]
                    if chunk_id in seen_new:
                        errors.append(f"New input repeats chunk ID {chunk_id!r}.")
                    seen_new.add(chunk_id)
                    old = old_chunks.get(chunk_id)
                    digest = hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest()
                    if old is not None and (old.get("chapter_id") != chunk["chapter_id"] or old.get("sha256") != digest):
                        errors.append(f"New input reuses chunk ID {chunk_id!r} with different chapter or content.")

    for name in sorted(STAGE_NAMES):
        stage = stages.get(name)
        if not isinstance(stage, dict):
            errors.append(f"stages.{name} must be an object.")
            continue
        if stage.get("status") not in VALID_STAGE_STATUS:
            errors.append(f"stages.{name}.status is missing or invalid.")
        if stage.get("gate") not in VALID_GATES:
            errors.append(f"stages.{name}.gate is missing or invalid.")
        if not isinstance(stage.get("summary"), str):
            errors.append(f"stages.{name}.summary must be a string.")
        if not isinstance(stage.get("missing_inputs"), list):
            errors.append(f"stages.{name}.missing_inputs must be an array.")

    expected = coverage.get("expected_chapter_ids", [])
    received = coverage.get("received_chapter_ids", [])
    processed = coverage.get("processed_chapter_ids", [])
    pending = coverage.get("pending_chapter_ids", [])
    for field in ("expected_chapter_ids", "received_chapter_ids", "processed_chapter_ids",
                  "pending_chapter_ids", "coverage_percent", "completeness",
                  "last_processed_chapter_id", "scope_note"):
        if field not in coverage:
            errors.append(f"coverage.{field} is required.")
    for label, values in (("expected_chapter_ids", expected),
                          ("received_chapter_ids", received),
                          ("processed_chapter_ids", processed),
                          ("pending_chapter_ids", pending)):
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            errors.append(f"coverage.{label} must be an array of strings.")
        else:
            unique_values(values, f"coverage.{label}", errors)
    if isinstance(received, list) and all(isinstance(chapter, str) for chapter in received):
        if set(received) != {chapter for chapter in chapter_of_chunk.values() if isinstance(chapter, str)}:
            errors.append("received_chapter_ids must equal the chapters in source_chunks.")
    if all(isinstance(value, list) and all(isinstance(item, str) for item in value)
           for value in (expected, received, processed, pending)):
        if not set(processed).issubset(set(received)):
            errors.append("processed_chapter_ids must be a subset of received_chapter_ids.")
        if expected:
            if not set(received).issubset(set(expected)):
                errors.append("received_chapter_ids contains a chapter outside expected_chapter_ids.")
            calculated_pending = set(expected) - set(processed)
            if set(pending) != calculated_pending:
                errors.append("pending_chapter_ids must equal expected_chapter_ids minus processed_chapter_ids.")
            calculated_percent = 100 * len(set(processed)) / len(set(expected))
            actual_percent = coverage.get("coverage_percent")
            # Match at the reported precision instead of allowing a flat 0.1 slack:
            # two of three chapters is 66.7, so 66.6 is a wrong number, not a rounding.
            if (not is_number(actual_percent)
                    or round(actual_percent, COVERAGE_PERCENT_DECIMALS)
                    != round(calculated_percent, COVERAGE_PERCENT_DECIMALS)):
                errors.append("coverage_percent should be "
                              f"{round(calculated_percent, COVERAGE_PERCENT_DECIMALS)} for the declared chapter sets.")
            if calculated_pending and coverage.get("completeness") != "partial":
                errors.append("completeness must be partial while expected chapters remain pending.")
            if not calculated_pending and coverage.get("completeness") != "complete":
                errors.append("completeness must be complete when all expected chapters are processed.")
            if calculated_pending and run.get("status") == "complete":
                errors.append("run.status cannot be complete while expected chapters remain pending.")
        elif coverage.get("coverage_percent") is not None or coverage.get("completeness") != "unknown":
            errors.append("Without an expected chapter manifest, coverage_percent must be null and completeness must be unknown.")
    if is_number(coverage.get("coverage_percent")) and not 0 <= coverage["coverage_percent"] <= 100:
        errors.append("coverage_percent must be between 0 and 100.")
    title_only = isinstance(work, dict) and work.get("scope") == "title_only"
    if title_only:
        declared = [values for values in (expected, received, processed, pending)
                    if isinstance(values, list) and values]
        if declared:
            errors.append("work.scope title_only means no source material is visible, so coverage must list no "
                          "chapters; use complete_work, selected_chapters, excerpt, or notes when chapters were supplied.")
    if not processed and not title_only:
        if run.get("status") == "complete":
            errors.append("run.status cannot be complete with no processed chapters.")
        for name, stage in stages.items():
            if isinstance(stage, dict) and (stage.get("status") == "complete" or stage.get("gate") == "pass"):
                errors.append(f"stages.{name} cannot be complete/pass with no processed chapters.")
    last_processed = coverage.get("last_processed_chapter_id")
    if last_processed is not None and last_processed not in processed:
        errors.append("last_processed_chapter_id must be null or in processed_chapter_ids.")

    evidence_ids = []
    for item in evidence:
        if not isinstance(item, dict):
            errors.append("Each evidence item must be an object.")
            continue
        for field in ("evidence_id", "chapter_id", "chunk_id", "locator", "paraphrase"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                errors.append(f"Evidence item is missing a non-empty {field}.")
        if isinstance(item.get("evidence_id"), str):
            evidence_ids.append(item["evidence_id"])
        chunk_id = item.get("chunk_id")
        if not isinstance(chunk_id, str) or chunk_id not in chapter_of_chunk:
            errors.append(f"Evidence {item.get('evidence_id', '?')} refers to a missing source chunk {chunk_id!r}.")
        elif item.get("chapter_id") != chapter_of_chunk[chunk_id]:
            errors.append(f"Evidence {item.get('evidence_id', '?')} chapter differs from its source chunk.")
    unique_values(evidence_ids, "evidence.evidence_id", errors)
    evidence_set = set(evidence_ids)

    claim_ids = check_claims(claims, evidence_set, errors)
    card_ids = check_cards(cards, evidence_set, errors)
    card_set = set(card_ids)
    sop_ids = check_sops(sops, card_set, errors)
    sop_set = set(sop_ids)

    check_stage_payloads(stages, errors)
    check_practice(stages.get("practice"), card_set, sop_set, errors)
    check_quality_review(quality_review, errors)
    check_correction_log(correction_log, evidence_set,
                         set(claim_ids) | card_set | sop_set | evidence_set, errors, warnings)

    for path, refs in iter_evidence_refs(stages, "stages"):
        check_refs(refs, path, evidence_set, errors)

    technique_stage = stages.get("technique", {})
    if isinstance(technique_stage, dict):
        listed = technique_stage.get("technique_card_ids", [])
        if not isinstance(listed, list) or any(not isinstance(item, str) or item not in card_set for item in listed):
            errors.append("stages.technique.technique_card_ids must refer to existing technique cards.")
    for card in cards:
        if isinstance(card, dict) and card.get("sop_id") is not None and (not isinstance(card["sop_id"], str) or card["sop_id"] not in sop_set):
            errors.append(f"Technique card {card.get('card_id', '?')} refers to a missing SOP.")

    mode = data.get("run", {}).get("mode") if isinstance(data.get("run"), dict) else None
    required_cards = {"quick": 1, "standard": 3, "deep": 3}.get(mode)
    if required_cards is not None and len(cards) < required_cards:
        warnings.append(f"{mode} mode normally targets at least {required_cards} technique card(s); confirm the technique gate explains any shortfall.")

    for warning in warnings:
        print("WARNING:", warning)
    for error in errors:
        print("ERROR:", error)

    if errors:
        print(f"Structural check failed with {len(errors)} error(s).")
        return 1
    print(f"Structural check passed: {len(evidence_ids)} evidence item(s), {len(claim_ids)} claim(s), {len(card_ids)} technique card(s).")
    print("This check does not validate literary interpretation against source text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
