"""Schema validation, conservative response repair and evidence-based metrics.

Coverage is lexical evidence, not a semantic correctness score. Metadata injected
by the pipeline is deliberately excluded from content coverage calculations.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter

from podcast_policy import podcast_errors

from .chunker import source_inventory
from .schemas import ITEM_KEYS, canonical_action, get_schema, schema_errors

_META_KEYS = {"provenance", "_provenance", "metadata", "_metadata", "source_sections", "source_inventory",
              "coverage", "coverage_manifest", "citations", "source_ids", "source_id", "sources", "chunk_id", "chunk_ids",
              "model", "connector", "generation_parameters", "source_refs", "source_references"}


def _strings(value, *, include_metadata: bool = False):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, include_metadata=include_metadata)
    elif isinstance(value, dict):
        for key, item in value.items():
            if include_metadata or (not key.startswith("_") and key not in _META_KEYS):
                yield from _strings(item, include_metadata=include_metadata)


def normalize_artifact(data, action: str):
    """Repair unambiguous scalar/list formatting without inventing content.

    This never unescapes backslashes or rewrites mathematics.
    """
    data = copy.deepcopy(data)
    action = canonical_action(action)
    if not isinstance(data, dict):
        return data

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for key in ("source_ids", "sources", "tags"):
                if isinstance(value.get(key), str):
                    value[key] = [value[key]]
            for item in value.values():
                walk(item)
    walk(data)
    if action == "create_infographics":
        for section in data.get("sections", []) if isinstance(data.get("sections"), list) else []:
            if isinstance(section, dict):
                for key in ("value", "label", "panel"):
                    section.setdefault(key, None)
                section.setdefault("items", [])
    elif action == "create_mindmaps":
        def leaves(node):
            if isinstance(node, dict):
                node.setdefault("children", [])
                if isinstance(node["children"], list):
                    for child in node["children"]:
                        leaves(child)
        leaves(data)
    elif action in {"create_qandas", "create_quizzes"}:
        for question in data.get("questions", []) if isinstance(data.get("questions"), list) else []:
            if not isinstance(question, dict):
                continue
            if action == "create_quizzes" and isinstance(question.get("correct"), str) and re.fullmatch(r"[0-3]", question["correct"]):
                question["correct"] = int(question["correct"])
            if action == "create_qandas":
                if question.get("required") in ("true", "false"):
                    question["required"] = question["required"] == "true"
                if question.get("kind") == "generated":
                    question.setdefault("exercise_label", None)
                if isinstance(question.get("exercise_label"), (int, float)):
                    question["exercise_label"] = str(question["exercise_label"])
                hint = question.get("placeholder")
                if isinstance(hint, str) and hint.strip() and not hint.endswith("…"):
                    question["placeholder"] = hint.rstrip().removesuffix("...").rstrip(".") + "…"
    elif action == "create_datatables":
        fields = [f.get("name") for f in data.get("fields", []) if isinstance(f, dict)] if isinstance(data.get("fields"), list) else []
        for row in data.get("data", []) if isinstance(data.get("data"), list) else []:
            if isinstance(row, dict):
                for name in fields:
                    if name in row and isinstance(row[name], (int, float)) and not isinstance(row[name], bool):
                        row[name] = str(row[name])
    return data


def validate_artifact(data, action: str, *, final: bool = False) -> list[str]:
    """Validate canonical skill structure and non-editorial content invariants."""
    action = canonical_action(action)
    try:
        errors = schema_errors(data, get_schema(action))
    except ValueError as exc:
        return [str(exc)]
    if errors or not isinstance(data, dict):
        return errors
    if action == "create_qandas":
        ids = [q["id"] for q in data["questions"]]
        if len(ids) != len(set(ids)):
            errors.append("$.questions: ids must be unique")
        for index, question in enumerate(data["questions"]):
            if question["kind"] == "source_exercise" and not question["exercise_label"]:
                errors.append(f"$.questions[{index}].exercise_label: source exercise needs its original label")
            if question["kind"] == "generated" and question["exercise_label"] is not None:
                errors.append(f"$.questions[{index}].exercise_label: generated question must use null")
            if not question["placeholder"].endswith("…"):
                errors.append(f"$.questions[{index}].placeholder: hint must end with an ellipsis character")
    elif action == "create_flashcards":
        for index, card in enumerate(data["cards"]):
            if card["type"] == "cloze" and "______" not in card["front"]:
                errors.append(f"$.cards[{index}].front: cloze requires literal ______ blank")
    elif action == "create_datatables":
        fields = [field["name"] for field in data["fields"]]
        if len(fields) != len(set(fields)):
            errors.append("$.fields: names must be unique")
        for index, row in enumerate(data["data"]):
            for name in fields:
                if name not in row or not isinstance(row[name], str):
                    errors.append(f"$.data[{index}].{name}: every comparative cell must be a string")
    elif action == "create_infographics":
        seen, prior = set(), None
        for index, section in enumerate(data["sections"]):
            panel = section["panel"]
            if panel is not None and panel != prior and panel in seen:
                errors.append(f"$.sections[{index}].panel: same-panel sections must be contiguous")
            if panel is not None:
                seen.add(panel)
            prior = panel
            if section["type"] == "chart" and any(not re.fullmatch(r".+:\s*-?\d+(?:\.\d+)?%?\s*", item) for item in section["items"]):
                errors.append(f"$.sections[{index}].items: chart entries must be 'label: number'")
            if section["type"] in {"quote", "stat", "svg"} and not section["value"]:
                errors.append(f"$.sections[{index}].value: required for {section['type']}")
            if section["type"] == "svg" and section["value"] and not re.fullmatch(r"assets/[\w-]+\.svg", section["value"]):
                errors.append(f"$.sections[{index}].value: must reference assets/<slug>.svg")
    elif action == "create_podcasts":
        errors.extend(podcast_errors(data))
        speakers = [cast["speaker_id"] for cast in data["cast"]]
        if len(speakers) != len(set(speakers)):
            errors.append("$.cast: speaker ids must be unique")
        if not {"HOST_A", "HOST_B"}.issubset({cast["host_id"] for cast in data["cast"]}):
            errors.append("$.cast: HOST_A and HOST_B are required")
        for index, segment in enumerate(data["script"]):
            for scene_index, scene in enumerate(segment["scenes"]):
                path = f"$.script[{index}].scenes[{scene_index}]"
                if scene["speaker_id"] not in speakers:
                    errors.append(path + ".speaker_id: not in cast")
                directions = scene.get("directions", "")
                if directions and not re.fullmatch(r"(?:\s*\[[A-Za-z][A-Za-z -]*\]|\s*\[pause=\d+\])*\s*", directions):
                    errors.append(path + ".directions: only bracket mood/pause tags are allowed")
                if not scene["dialogue"].strip() and not re.search(r"\[pause=\d+\]", directions):
                    errors.append(path + ".dialogue: empty dialogue requires a deliberate pause")
    return errors


def editorial_warnings(data, action: str) -> list[str]:
    """Final artifact size/style checks, separated from schema validity."""
    if not isinstance(data, dict):
        return []
    action = canonical_action(action)
    rules = {"create_datatables": ("fields", 10, 15), "create_flashcards": ("cards", 10, 50),
             "create_reports": ("sections", 3, 7), "create_slides": ("slides", 8, 12),
             "create_infographics": ("sections", 4, 8)}
    warnings = []
    if action in rules:
        key, minimum, maximum = rules[action]
        value = data.get(key)
        if isinstance(value, list) and not minimum <= len(value) <= maximum:
            warnings.append(f"{key}: {len(value)} items; skill recommends {minimum}–{maximum}. Preserve coverage when consolidating.")
    if action == "create_quizzes":
        levels = {q.get("difficulty") for q in data.get("questions", []) if isinstance(q, dict)}
        if len(levels) < 4:
            warnings.append("Quiz does not yet include all four difficulty levels.")
    return warnings


def _strip_fence(text: str) -> str:
    match = re.fullmatch(r"\s*```(?:json)?\s*\n(.*?)\n```\s*", text, re.S | re.I)
    return match.group(1) if match else text


def _remove_trailing_commas(text: str) -> str:
    """State machine: commas inside string literals and LaTeX remain untouched."""
    result, quoted, escaped = [], False, False
    for index, char in enumerate(text):
        if quoted:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        else:
            if char == '"':
                quoted = True
            if char == ",":
                following = text[index + 1:].lstrip()
                if following.startswith(("}", "]")):
                    continue
            result.append(char)
    return "".join(result)


def latex_errors(text: str) -> list[str]:
    """Check delimiter/environment balance; not a TeX compiler or proof checker."""
    errors = []
    for opening, closing in ((r"\(", r"\)"), (r"\[", r"\]")):
        if text.count(opening) != text.count(closing):
            errors.append(f"Unbalanced LaTeX delimiters {opening} / {closing}")
    if len(re.findall(r"(?<!\\)\$\$", text)) % 2:
        errors.append("Unbalanced display dollar delimiters")
    stack = []
    for match in re.finditer(r"\\(begin|end)\{([^}]+)\}", text):
        if match.group(1) == "begin":
            stack.append(match.group(2))
        elif not stack or stack.pop() != match.group(2):
            errors.append("Mismatched LaTeX environment " + match.group(2))
    if stack:
        errors.append("Unclosed LaTeX environments: " + ", ".join(stack))
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        errors.append("Control characters may indicate unescaped JSON LaTeX backslashes")
    for formula in source_inventory(text)["formulas"]:
        balance = 0
        for char in re.sub(r"\\[{}]", "", formula["text"]):
            if char == "{":
                balance += 1
            elif char == "}":
                balance -= 1
            if balance < 0:
                break
        if balance:
            errors.append("Unbalanced braces inside LaTeX formula")
    return list(dict.fromkeys(errors))


def validate_output(text: str, action: str, extension: str = ".json", finish_reason: str | None = None) -> dict:
    errors, repairs, data = [], [], None
    truncated = str(finish_reason).lower() in {"length", "max_tokens", "max_output_tokens", "model_length"}
    if truncated:
        errors.append("Provider reported output token limit")
    if not isinstance(text, str) or not text.strip():
        return {"valid": False, "errors": errors + ["Empty model output"], "truncated": truncated, "data": None, "text": text, "repairs": []}
    text = text.removeprefix("\ufeff")
    if extension.lower() == ".json":
        clean = _strip_fence(text)
        if clean != text:
            repairs.append("removed_outer_json_fence")
        try:
            data = json.loads(clean, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Non-finite JSON number: {value}")))
        except (json.JSONDecodeError, ValueError) as exc:
            repaired = _remove_trailing_commas(clean)
            try:
                if repaired == clean:
                    raise exc
                data = json.loads(repaired, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Non-finite JSON number: {value}")))
                repairs.append("removed_trailing_commas")
            except (json.JSONDecodeError, ValueError):
                errors.append(f"Invalid JSON: {exc}")
                if isinstance(exc, json.JSONDecodeError) and (exc.pos >= len(clean.rstrip()) - 2 or "Unterminated" in exc.msg):
                    truncated = True
        if data is not None:
            normalized = normalize_artifact(data, action)
            if normalized != data:
                repairs.append("normalized_minor_schema_formatting")
            data = normalized
            errors.extend(validate_artifact(data, action))
            for value in _strings(data):
                errors.extend(latex_errors(value))
            text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    else:
        if canonical_action(action) == "create_podcasts":
            errors.extend(podcast_errors(text))
        if len(re.findall(r"^\s*```", text, re.M)) % 2:
            errors.append("Unfinished Markdown code fence")
            truncated = True
        errors.extend(latex_errors(text))
        if text.rstrip().endswith((",", ":", ";", "\\")):
            errors.append("Output ends at a likely incomplete phrase")
            truncated = True
    return {"valid": not errors, "errors": list(dict.fromkeys(errors)), "truncated": truncated,
            "data": data, "text": text, "repairs": repairs}


_STOP = {"the", "and", "for", "with", "this", "that", "from", "are", "was", "were", "has", "have", "its", "into", "what", "which", "why", "how", "can", "will", "all", "not", "use", "using"}


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z][a-z0-9]+", text.casefold()) if len(word) > 2 and word not in _STOP}


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def _formula_key(text: str) -> str:
    return re.sub(r"\s+", "", text)


def validate_coverage(source_text: str, artifacts, action: str | None = None, source_name: str | None = None) -> dict:
    """Compute auditable lexical coverage with no fabricated percentage baseline.

    ``None`` ratios mean no identifiable source items, not 100% coverage. Source
    references establish traceability only; they do not establish factual truth.
    """
    inventory = source_inventory(source_text)
    if not isinstance(artifacts, list):
        artifacts = [artifacts]
    texts = [text for artifact in artifacts for text in _strings(artifact)]
    visible = "\n".join(texts)
    normalized = _normalized(visible)
    words = _words(visible)
    section_hits, missing_sections, section_positions = [], [], []
    for section in inventory["sections"]:
        title = _normalized(section["title"])
        wanted = _words(section["title"])
        exact = bool(title and title in normalized)
        lexical = bool(len(wanted) >= 2 and len(wanted & words) / len(wanted) >= .8)
        if exact or lexical:
            section_hits.append({"id": section["id"], "label": section["label"], "evidence": "heading_text" if exact else "lexical_terms"})
            if exact:
                section_positions.append(normalized.find(title))
        else:
            missing_sections.append(section)
    question_records = []
    for artifact in artifacts:
        if isinstance(artifact, dict) and isinstance(artifact.get("questions"), list):
            question_records.extend(q for q in artifact["questions"] if isinstance(q, dict))
    exercise_hits, missing_exercises, exercise_positions = [], [], []
    for exercise in inventory["exercises"]:
        label = exercise["label"].strip().rstrip(".")
        wanted = _words(" ".join(exercise["text"].split()[:55]))
        hit = False
        if question_records:
            for index, record in enumerate(question_records):
                record_label = str(record.get("exercise_label", "")).strip().rstrip(".")
                # Multi-part records may append '(a)' or '.a' to original numbering.
                label_matches = record_label == label or bool(re.fullmatch(re.escape(label) + r"(?:\s*\(?[a-z]\)?|[.-][a-z])", record_label, re.I))
                question = record.get("question", "")
                answer = record.get("answer", "")
                overlap = len(wanted & _words(question)) / max(len(wanted), 1) if isinstance(question, str) else 0
                if record.get("kind") == "source_exercise" and label_matches and isinstance(answer, str) and answer.strip() and overlap >= .25:
                    hit = True
                    exercise_positions.append(index)
                    break
        else:
            label_match = re.search(r"(?<!\d)" + re.escape(label) + r"(?!\d)", visible)
            hit = bool(label_match and wanted and len(wanted & words) / len(wanted) >= .5)
        (exercise_hits if hit else missing_exercises).append(exercise)
    formula_keys = {_formula_key(formula["text"]) for text in texts for formula in source_inventory(text)["formulas"]}
    missing_formulas = [formula for formula in inventory["formulas"] if _formula_key(formula["text"]) not in formula_keys]
    items = []
    action = canonical_action(action) if action else None
    for artifact in artifacts:
        if isinstance(artifact, dict):
            key = ITEM_KEYS.get(action or "")
            if key and isinstance(artifact.get(key), list):
                items.extend(artifact[key])
            else:
                items.append(artifact)
        else:
            items.append(artifact)
    fingerprints = [_normalized("\n".join(_strings(item))) for item in items]
    fingerprints = [fingerprint for fingerprint in fingerprints if fingerprint]
    duplicate_count = sum(count - 1 for count in Counter(fingerprints).values())
    grounded = 0
    for item in items:
        def references(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"source_ids", "source_id", "sources", "citations"}:
                        yield from _strings(child, include_metadata=True)
                    elif not key.startswith("_") and key not in _META_KEYS:
                        yield from references(child)
            elif isinstance(value, list):
                for child in value:
                    yield from references(child)
        refs = list(references(item))
        if (source_name in refs if source_name else bool(refs)):
            grounded += 1
    errors = [error for text in texts for error in latex_errors(text)]
    source_latex = latex_errors(source_text)
    inversions = sum(left > right for positions in (section_positions, exercise_positions) for left, right in zip(positions, positions[1:]))
    ratio = lambda found, total: found / total if total else None
    schema_results = [validate_artifact(artifact, action) for artifact in artifacts] if action else []
    return {
        "coverage": ratio(len(section_hits), len(inventory["sections"])),
        "exercise_coverage": ratio(len(exercise_hits), len(inventory["exercises"])),
        "formula_coverage": ratio(len(inventory["formulas"]) - len(missing_formulas), len(inventory["formulas"])),
        "schema_valid": all(not errors for errors in schema_results) if schema_results else None,
        "schema_errors": [error for errors in schema_results for error in errors],
        "duplicate_ratio": ratio(duplicate_count, len(fingerprints)) or 0.0,
        "source_grounding": ratio(grounded, len(items)), "grounding_method": "explicit_artifact_source_references_only",
        "latex_errors": len(errors), "latex_error_details": list(dict.fromkeys(errors)),
        "source_latex_errors": source_latex, "ordering_errors": inversions,
        "sections_total": len(inventory["sections"]), "exercises_total": len(inventory["exercises"]), "formulas_total": len(inventory["formulas"]),
        "section_evidence": section_hits, "missing_sections": missing_sections, "missing_exercises": missing_exercises, "missing_formulas": missing_formulas,
        "coverage_method": "lexical_evidence_excluding_provenance", "semantic_correctness_verified": False,
        "warnings": ["Coverage and ordering are lexical heuristics; paraphrases, OCR damage, and semantic correctness require review."] +
                    [warning for artifact in artifacts for warning in editorial_warnings(artifact, action)] if action else ["Coverage is lexical evidence, not semantic verification."],
    }
