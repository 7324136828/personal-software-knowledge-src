"""Loss-preserving tree synthesis for the nine canonical artifact structures.

Each merge operates on a bounded number of children. Optional model edits are
accepted only if the deterministic content ledger survives; models cannot silently
summarize away formulas, exercise answers, or whole child artifacts.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Callable

from .checkpoint import atomic_json, digest


def canonical(value: Any) -> str:
    if isinstance(value, str):
        # Do not lowercase or strip punctuation: math and case can change meaning.
        return re.sub(r"\s+", " ", value).strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def strings(value: Any):
    if isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in {"_provenance", "id", "source_ids", "source_id", "chunk_id"}:
                yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def preserves_content(before: Any, after: Any) -> bool:
    """Conservative guard, intentionally rejects paraphrases that could lose detail."""
    haystack = canonical("\n".join(strings(after)))
    return all(canonical(value) in haystack for value in strings(before))


def _join(left: Any, right: Any) -> Any:
    if left == right:
        return copy.deepcopy(left)
    if isinstance(left, str) and isinstance(right, str):
        parts = [left] if left.strip() else []
        if right.strip() and canonical(right) not in {canonical(p) for p in parts}:
            parts.append(right)
        return "\n\n".join(parts)
    if isinstance(left, list) and isinstance(right, list):
        result = copy.deepcopy(left)
        seen = {canonical(item) for item in result}
        for item in right:
            if canonical(item) not in seen:
                result.append(copy.deepcopy(item))
                seen.add(canonical(item))
        return result
    if isinstance(left, dict) and isinstance(right, dict):
        result = copy.deepcopy(left)
        for key, value in right.items():
            result[key] = _join(result[key], value) if key in result else copy.deepcopy(value)
        return result
    # Conflicting scalar values should remain separate at the record level.
    return copy.deepcopy(left)


def merge_artifacts(artifacts: list[dict], action: str) -> dict:
    if not artifacts:
        raise ValueError("Cannot aggregate an empty artifact list")
    result = copy.deepcopy(artifacts[0])
    for incoming in artifacts[1:]:
        if action == "create_mindmaps":
            result["children"] = _merge_named(result.get("children", []), incoming.get("children", []), "name")
            continue
        if action == "create_datatables":
            result["fields"] = _merge_named(result.get("fields", []), incoming.get("fields", []), "name")
            # A source normally contributes many distinct rows. Deduplicate exact
            # records rather than collapsing everything sharing one source_id.
            result["data"] = _join(result.get("data", []), incoming.get("data", []))
            continue
        if action == "create_podcasts":
            # Canonicalize cast IDs by host role; never leave dangling speaker IDs.
            incoming = copy.deepcopy(incoming)
            roles = {c["host_id"]: c for c in result.get("cast", [])}
            mapping = {}
            for member in incoming.get("cast", []):
                match = roles.get(member["host_id"])
                if match:
                    mapping[member["speaker_id"]] = match["speaker_id"]
                else:
                    new_id = member["speaker_id"]
                    if new_id in {c["speaker_id"] for c in result["cast"]}:
                        new_id += "_" + str(len(result["cast"]))
                    mapping[member["speaker_id"]] = new_id
                    member["speaker_id"] = new_id
                    result["cast"].append(member)
            for segment in incoming.get("script", []):
                for scene in segment.get("scenes", []):
                    scene["speaker_id"] = mapping.get(scene["speaker_id"], scene["speaker_id"])
            result["script"] = _join(result.get("script", []), incoming.get("script", []))
            continue
        for key, value in incoming.items():
            if key in {"title", "name", "episode_title", "podcast_show"}:
                continue
            if key == "sections" and action == "create_reports":
                result[key] = _merge_named(result.get(key, []), value, "title")
            else:
                result[key] = _join(result[key], value) if key in result else copy.deepcopy(value)
    if action == "create_qandas":
        questions = []
        seen = {}
        ids = set()
        for q in result.get("questions", []):
            # Same prompt with a different answer carries additional information.
            identity = (q.get("kind"), q.get("exercise_label"), canonical(q.get("question", "")))
            if identity in seen:
                existing = questions[seen[identity]]
                existing["answer"] = _join(existing["answer"], q["answer"])
                existing["source_ids"] = _join(existing["source_ids"], q["source_ids"])
                continue
            q = copy.deepcopy(q)
            identifier = re.sub(r"[^a-z0-9]+", "-", str(q.get("id", "question")).lower()).strip("-") or "question"
            unique, number = identifier, 2
            while unique in ids:
                unique, number = f"{identifier}-{number}", number + 1
            q["id"] = unique
            ids.add(unique)
            seen[identity] = len(questions)
            questions.append(q)
        result["questions"] = questions
    if action == "create_datatables":
        names = [field["name"] for field in result.get("fields", [])]
        result["data"] = [{**{name: row.get(name, "N/A") for name in names},
                           "source_id": row["source_id"], "page": row.get("page", "N/A")}
                          for row in result.get("data", [])]
    return result


def _merge_named(left: list, right: list, key: str) -> list:
    result = copy.deepcopy(left)
    for item in right:
        # Reconcile harmless capitalization/spacing drift while retaining the first
        # occurrence as the canonical terminology and ordering.
        match = next((old for old in result if canonical(old.get(key)).casefold() == canonical(item.get(key)).casefold()), None)
        if match is None:
            result.append(copy.deepcopy(item))
        else:
            for field, value in item.items():
                if field == key:
                    continue
                if field == "children":
                    match[field] = _merge_named(match.get(field, []), value, "name")
                else:
                    match[field] = _join(match[field], value) if field in match else copy.deepcopy(value)
    return result


def hierarchical_aggregate(artifacts: list[dict], action: str, directory: Path,
                           group_size: int = 4,
                           editor: Callable[[dict, str], dict] | None = None) -> dict:
    if group_size < 2:
        raise ValueError("Aggregation group_size must be at least 2")
    nodes, level = artifacts, 0
    # Even a singleton gets normalization/deduplication.
    while len(nodes) > 1:
        parents = []
        for number, start in enumerate(range(0, len(nodes), group_size), 1):
            children = nodes[start:start + group_size]
            merged = merge_artifacts(children, action)
            node_id = f"level_{level:02d}_node_{number:04d}"
            if editor:
                merged = editor(merged, node_id)
            atomic_json(directory / (node_id + ".json"), merged)
            atomic_json(directory / (node_id + ".manifest.json"),
                        {"children": [digest(child) for child in children], "output": digest(merged)})
            parents.append(merged)
        nodes, level = parents, level + 1
    return merge_artifacts(nodes, action)


def provenance_index(final: dict, artifacts: list[dict], references: list[dict]) -> dict:
    """Map final leaf paths to contributing chunks without changing skill schemas."""
    index = {}
    lookup = [(canonical("\n".join(strings(a))), ref) for a, ref in zip(artifacts, references)]
    def walk(value: Any, path: str):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            for number, child in enumerate(value):
                walk(child, f"{path}/{number}")
        elif isinstance(value, str) and value.strip():
            pieces = [canonical(piece) for piece in value.split("\n\n") if piece.strip()]
            matches = [ref for text, ref in lookup if any(piece in text for piece in pieces)]
            if matches:
                index[path] = matches
    walk(final, "")
    return {"method": "content lineage, not evidence of factual correctness", "items": index}
