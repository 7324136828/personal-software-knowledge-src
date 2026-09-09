"""Canonical artifact structures from the nine checked-in skills.

Only shape/content invariants are hard failures. Editorial size recommendations
are reported separately because a complete chapter aggregate can exceed them.
Extra fields (including provenance) are retained.
"""

from __future__ import annotations


def string(*, empty: bool = False, **constraints) -> dict:
    return {"type": "string", "minLength": 0 if empty else 1, **constraints}


def array(items: dict, minimum: int = 0, **constraints) -> dict:
    return {"type": "array", "items": items, "minItems": minimum, **constraints}


def obj(properties: dict, optional: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties,
            "required": [key for key in properties if key not in optional]}


BARE_SOURCE = string(pattern=r"^[^/\\]+$")
SOURCES = array(BARE_SOURCE, 1)
NULLABLE = {"type": ["string", "null"]}
TAG = string(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CITATION = obj({"source_id": BARE_SOURCE, "page": {"type": ["number", "null"]}, "chunk_id": NULLABLE})
NODE = obj({"name": string(), "children": array({"$ref": "node"})})

SCHEMAS = {
    "create_datatables": obj({
        "title": string(),
        "fields": array(obj({"name": string(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"), "description": string(), "example": string(empty=True)}), 1),
        "data": array(obj({"source_id": BARE_SOURCE, "page": string()}), 1),
    }),
    "create_flashcards": obj({
        "title": string(), "description": string(),
        "cards": array(obj({"type": string(enum=["basic", "cloze", "definition", "concept"]),
                            "front": string(), "back": string(), "tags": array(TAG, 1, maxItems=3), "source_ids": SOURCES}), 1),
    }),
    "create_infographics": obj({
        "title": string(), "subtitle": string(empty=True),
        "sections": array(obj({"type": string(enum=["quote", "stat", "flow", "comparison", "chart", "svg"]),
                               "title": string(), "value": NULLABLE, "label": NULLABLE,
                               "items": array(string()), "panel": NULLABLE, "span": string(enum=["full", "half"])}), 1),
    }),
    "create_mindmaps": NODE,
    "create_podcasts": obj({
        "episode_title": string(), "podcast_show": string(),
        "cast": array(obj({"speaker_id": string(), "host_id": string(), "name": string(), "voice_file": string(), "style": string()}), 2),
        "script": array(obj({"segment_name": string(), "scenes": array(obj({"speaker_id": string(), "directions": string(empty=True), "dialogue": string(empty=True)}, optional=("directions",)), 1)}), 1),
    }),
    "create_qandas": obj({
        "title": string(), "description": string(),
        "questions": array(obj({"id": TAG, "kind": string(enum=["source_exercise", "generated"]),
                                "exercise_label": NULLABLE, "question": string(), "answer": string(),
                                "placeholder": string(), "required": {"type": "boolean"}, "source_ids": SOURCES}), 1),
    }),
    "create_quizzes": obj({
        "title": string(), "description": string(),
        "questions": array(obj({"question": string(), "options": array(string(), 4, maxItems=4),
                                "correct": {"type": "integer", "minimum": 0, "maximum": 3}, "explanation": string(),
                                "difficulty": string(enum=["recall", "understanding", "analysis", "expert"]), "sources": SOURCES}), 1),
    }),
    "create_reports": obj({
        "title": string(), "executive_summary": string(),
        "sections": array(obj({"title": string(), "content": string(),
                               "claims": array(obj({"claim": string(), "citations": array(CITATION, 1)}), 1)}), 1),
        "conclusions": string(),
    }),
    "create_slides": obj({
        "title": string(), "slides": array(obj({"title": string(), "subtitle": string(empty=True),
                                                "bullets": array(string(), 3, maxItems=6), "speaker_notes": string(),
                                                "image_query": string(), "source_ids": SOURCES}), 1),
    }),
}

ITEM_KEYS = {"create_datatables": "data", "create_flashcards": "cards", "create_infographics": "sections",
             "create_mindmaps": "children", "create_podcasts": "script", "create_qandas": "questions",
             "create_quizzes": "questions", "create_reports": "sections", "create_slides": "slides"}


def canonical_action(action: str) -> str:
    aliases = {"podcast-script": "podcasts"}
    action = aliases.get(action, action)
    return action if action.startswith("create_") else "create_" + action


def get_schema(action: str) -> dict:
    try:
        return SCHEMAS[canonical_action(action)]
    except KeyError as exc:
        raise ValueError(f"Unknown artifact action: {action}") from exc


def schema_errors(value, schema: dict, path: str = "$", depth: int = 0) -> list[str]:
    """Small dependency-free validator for the schema features above."""
    import re

    if depth > 100:
        return [f"{path}: structure exceeds maximum validation depth"]
    if "$ref" in schema:
        schema = NODE
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    checks = {"string": lambda: isinstance(value, str), "object": lambda: isinstance(value, dict),
              "array": lambda: isinstance(value, list), "boolean": lambda: isinstance(value, bool),
              "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
              "integer": lambda: isinstance(value, int) and not isinstance(value, bool), "null": lambda: value is None}
    if expected and not any(checks[kind]() for kind in types):
        return [f"{path}: expected {' or '.join(types)}"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value.strip()) < schema.get("minLength", 0):
            errors.append(f"{path}: must not be empty")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errors.append(f"{path}: invalid format")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        for index, item in enumerate(value):
            errors.extend(schema_errors(item, schema.get("items", {}), f"{path}[{index}]", depth + 1))
    elif isinstance(value, dict):
        for field in schema.get("required", []):
            if field not in value:
                errors.append(f"{path}.{field}: required field missing")
        for field, subschema in schema.get("properties", {}).items():
            if field in value:
                errors.extend(schema_errors(value[field], subschema, f"{path}.{field}", depth + 1))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    return errors
