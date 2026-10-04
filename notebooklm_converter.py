"""Convert local NotebookLM exports without executing HTML or using a model.

Interactive exports carry their data in ``data-app-data``. Canonical content
sidecars take precedence; slides use local text extraction/OCR and audio uses
speaker-labelled transcripts. Infographics preserve their original visuals in
declarative SVG sections. Source metadata stays in separate companion files.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import struct
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from errors import GenerationCancelled, InputDocumentError, InvalidArgumentsError, OutputWriteError
from output_writer import write_output
from pipeline.engine import PipelineResult
from pipeline.schemas import NODE, canonical_action, get_schema, schema_errors
from pipeline.validator import editorial_warnings, normalize_artifact, validate_artifact

_METADATA_SUFFIX = " metadata.json"
_APPS = {"APP_TYPE_QUIZ": "create_quizzes", "APP_TYPE_FLASHCARDS": "create_flashcards",
         "APP_TYPE_MINDMAP": "create_mindmaps", "APP_TYPE_QANDA": "create_qandas", "APP_TYPE_QANDAS": "create_qandas"}
_TYPES = {"ARTIFACT_TYPE_TABLE": "create_datatables", "ARTIFACT_TYPE_AUDIO_OVERVIEW": "create_podcasts",
          "ARTIFACT_TYPE_INFOGRAPHIC": "create_infographics", "ARTIFACT_TYPE_SLIDES": "create_slides",
          "ARTIFACT_TYPE_REPORT": "create_reports", "ARTIFACT_TYPE_REPORTS": "create_reports",
          "ARTIFACT_TYPE_QANDA": "create_qandas", "ARTIFACT_TYPE_QANDAS": "create_qandas"}
_SUFFIXES = {"create_datatables": (".md",), "create_podcasts": (".wav", ".mp3", ".m4a", ".ogg"),
             "create_infographics": (".png", ".jpg", ".jpeg", ".webp", ".svg"),
             "create_slides": (".pptx", ".pdf")}
_MEDIA = {"create_podcasts", "create_infographics", "create_slides"}
NOTEBOOKLM_FORMATS = {"create_quizzes": {".json"}, "create_flashcards": {".json", ".txt"},
                     "create_mindmaps": {".json", ".md", ".mmd"}, "create_datatables": {".json", ".csv"},
                     "create_podcasts": {".json", ".md"}, "create_slides": {".json", ".md"},
                     "create_infographics": {".json", ".md", ".html", ".svg", ".wireframe.txt"},
                     "create_reports": {".json", ".md", ".html"}, "create_qandas": {".json"}}
LOGGER = logging.getLogger("content_generator.notebooklm")


class _MissingContentError(InputDocumentError):
    """Metadata-only exports may be replaced by an explicit source workflow."""


@dataclass(frozen=True)
class NotebookLMArtifact:
    metadata_path: Path
    source: Path
    action: str
    name: str
    metadata: dict
    dependencies: tuple[Path, ...]
    source_names: tuple[str, ...] = ()


def supported_notebooklm_formats(artifact: NotebookLMArtifact | str) -> frozenset[str]:
    action = artifact.action if isinstance(artifact, NotebookLMArtifact) else canonical_action(artifact)
    return frozenset(NOTEBOOKLM_FORMATS.get(action, ()))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise InputDocumentError(f"Could not read NotebookLM export '{path.name}': {exc}") from exc


def _json(text: str, label: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise InputDocumentError(f"Invalid JSON in NotebookLM export '{label}': {exc}") from exc


def _action(metadata: dict) -> str | None:
    explicit = metadata.get("action")
    if isinstance(explicit, str) and canonical_action(explicit) in NOTEBOOKLM_FORMATS:
        return canonical_action(explicit)
    artifact_type = metadata.get("type")
    if not isinstance(artifact_type, str):
        raise InputDocumentError("NotebookLM metadata needs a text artifact type.")
    if artifact_type == "ARTIFACT_TYPE_APP":
        app = metadata.get("app", {})
        if not isinstance(app, dict) or not isinstance(app.get("generationOptions", {}), dict):
            raise InputDocumentError("NotebookLM app metadata has invalid generationOptions.")
        app_type = app.get("generationOptions", {}).get("appType")
        if not isinstance(app_type, str):
            raise InputDocumentError("NotebookLM app metadata needs a text appType.")
        return _APPS.get(app_type)
    return _TYPES.get(artifact_type)


def _footnotes(text: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip() for line in text.splitlines()
            if (match := re.fullmatch(r"\s*\[(\d+)\]\s*:?\s+(.+?)\s*", line))}


def _source_ids(metadata: dict) -> list[str]:
    sources = metadata.get("sources", [])
    return [source["sourceId"]["id"] for source in sources if isinstance(source, dict)
            and isinstance(source.get("sourceId"), dict) and isinstance(source["sourceId"].get("id"), str)] if isinstance(sources, list) else []


def _bare_name(value: str) -> str:
    return Path(value.replace("\\", "/")).name


def _source_context(metadata_path: Path, metadata: dict, override=None) -> tuple[tuple[str, ...], tuple[Path, ...]]:
    root = next((parent for parent in (metadata_path.parent, *metadata_path.parents)
                 if parent.name.lower() == "artifacts"), metadata_path.parent)
    ids = _source_ids(metadata)
    names, dependencies = [], []
    if override is not None:
        if not isinstance(override, (list, tuple)) or any(not isinstance(name, str) or not name.strip() for name in override):
            raise InputDocumentError("NotebookLM source_names must be a list of filenames.")
        names = [_bare_name(name) for name in override]
    else:
        explicit = metadata.get("source_map", metadata.get("sourceMap", {}))
        if isinstance(explicit, dict):
            names += [_bare_name(explicit[source_id]) for source_id in ids
                      if isinstance(explicit.get(source_id), str) and explicit[source_id].strip()]
        sources = metadata.get("sources", [])
        for source in sources if isinstance(sources, list) else []:
            if not isinstance(source, dict):
                continue
            name = source.get("filename", source.get("title"))
            if isinstance(name, str) and name.strip():
                names.append(_bare_name(name))
        if not names and ids:
            mappings = {}
            mapping_paths = {}
            for path in root.rglob("*" + _METADATA_SUFFIX):
                candidate = _json(_read(path), path.name)
                if not isinstance(candidate, dict) or candidate.get("type") != "ARTIFACT_TYPE_TABLE":
                    continue
                candidate_ids = _source_ids(candidate)
                table_path = path.with_name(path.name[:-len(_METADATA_SUFFIX)] + ".md")
                if len(candidate_ids) != 1 or not table_path.is_file():
                    continue
                filenames = {_bare_name(name) for name in _footnotes(_read(table_path)).values()}
                if len(filenames) != 1:
                    continue
                source_id = candidate_ids[0]
                mappings.setdefault(source_id, set()).update(filenames)
                mapping_paths.setdefault(source_id, []).extend([path, table_path])
            for source_id in ids:
                if len(mappings.get(source_id, ())) == 1:
                    names.extend(mappings[source_id])
                    dependencies.extend(mapping_paths[source_id])
    names = list(dict.fromkeys(name for name in names if name))
    for name in names:
        for path in (root.parent / "Sources" / name, root.parent / "Sources" / (name + ".html"),
                     root.parent / "Sources" / (name + _METADATA_SUFFIX)):
            if path.is_file():
                dependencies.append(path)
    return tuple(names), tuple(dict.fromkeys(dependencies))


def artifact_from_metadata(metadata_path: Path, *, source_names=None) -> NotebookLMArtifact:
    """Reconstruct one queued export using its metadata and sibling payload."""
    metadata_path = Path(metadata_path)
    if not metadata_path.name.endswith(_METADATA_SUFFIX):
        raise InputDocumentError("NotebookLM exports need a '<name> metadata.json' file.")
    metadata = _json(_read(metadata_path), metadata_path.name)
    if not isinstance(metadata, dict):
        raise InputDocumentError(f"NotebookLM metadata '{metadata_path.name}' must be an object.")
    action = _action(metadata)
    if action is None:
        raise InputDocumentError(f"Unsupported NotebookLM artifact type in '{metadata_path.name}'.")
    status = metadata.get("status")
    if status is not None and status != "ARTIFACT_STATUS_READY":
        raise InputDocumentError(f"NotebookLM artifact '{metadata_path.name}' is not ready.")
    name = metadata_path.name[:-len(_METADATA_SUFFIX)]
    if not name or name in {".", ".."}:
        raise InputDocumentError("NotebookLM artifact metadata needs a non-empty export name.")
    candidates = [metadata_path.with_name(name)]
    candidates += [metadata_path.with_name(name + suffix) for suffix in _SUFFIXES.get(action, (".html", ".json"))]
    candidates.append(metadata_path.with_name(name + ".json"))
    content_path = metadata_path.with_name(name + ".content.json")
    transcript_path = metadata_path.with_name(name + ".transcript.json")
    candidates.append(content_path)
    source = next((candidate for candidate in candidates if candidate.is_file() or
                   (action == "create_slides" and candidate.is_dir())), None)
    if source is None:
        raise _MissingContentError(f"NotebookLM artifact '{name}' has metadata but no exported content.")
    natural_key = lambda path: [int(part) if part.isdigit() else part.lower()
                                for part in re.split(r"(\d+)", path.as_posix())]
    payloads = sorted(source.rglob("*"), key=natural_key) if source.is_dir() else [source]
    files = [path for path in payloads if path.is_file()]
    if not files:
        if content_path.is_file():
            source, files = content_path, [content_path]
        else:
            raise _MissingContentError(f"NotebookLM artifact '{name}' contains no exported files.")
    extra = [path for path in (content_path, transcript_path) if path.is_file()]
    asset_content_path = content_path if content_path.is_file() else (source if source.suffix.lower() == ".json" else None)
    if asset_content_path is not None and action == "create_infographics":
        content = _json(_read(asset_content_path), asset_content_path.name)
        for section in content.get("sections", []) if isinstance(content, dict) and isinstance(content.get("sections"), list) else []:
            if isinstance(section, dict) and section.get("type") == "svg" and isinstance(section.get("value"), str):
                reference = section["value"]
                if re.fullmatch(r"assets/[\w-]+\.svg", reference):
                    candidate = metadata_path.parent / reference
                    if candidate.is_file():
                        extra.append(candidate)
    resolved_names, source_dependencies = _source_context(metadata_path, metadata, source_names)
    dependencies = tuple(dict.fromkeys([metadata_path, *files, *extra, *source_dependencies]))
    return NotebookLMArtifact(metadata_path, source, action, name, metadata, dependencies, resolved_names)


def discover_notebooklm_artifacts(input_dir: Path, actions: list[str], *, contained: bool = False,
                                 package_root: Path | None = None, allow_empty: bool = False,
                                 warn_missing: bool = True) -> list[NotebookLMArtifact]:
    """Discover metadata-backed exports, independent of the document glob."""
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise InputDocumentError(f"NotebookLM input directory does not exist: {input_dir}")
    requested = {canonical_action(action) for action in actions}
    if allow_empty and input_dir.name.lower() != "artifacts" and (input_dir / "Sources").is_dir() and not (input_dir / "Artifacts").is_dir():
        return []
    root = input_dir / "Artifacts" if (input_dir / "Artifacts").is_dir() else input_dir
    results = []
    for metadata_path in sorted(root.rglob("*" + _METADATA_SUFFIX)):
        if any(part.lower() == "sources" for part in metadata_path.relative_to(root).parts[:-1]):
            continue
        if not metadata_path.is_file():
            continue
        if contained:
            try:
                metadata_path.resolve().relative_to(input_dir.resolve())
            except ValueError as exc:
                raise InvalidArgumentsError("NotebookLM metadata must stay inside the study-set package.") from exc
        metadata = _json(_read(metadata_path), metadata_path.name)
        if not isinstance(metadata, dict):
            raise InputDocumentError(f"NotebookLM metadata '{metadata_path.name}' must be an object.")
        if _action(metadata) not in requested:
            continue
        try:
            artifact = artifact_from_metadata(metadata_path)
        except _MissingContentError:
            if allow_empty:
                LOGGER.info("NotebookLM %s has no exported payload; source generation may supply it.", metadata_path.name)
                continue
            raise
        if contained:
            try:
                artifact.source.resolve().relative_to(input_dir.resolve())
            except ValueError as exc:
                raise InvalidArgumentsError("NotebookLM source must stay inside its configured input directory.") from exc
            boundary = Path(package_root).resolve() if package_root is not None else input_dir.resolve()
            for path in artifact.dependencies:
                try:
                    path.resolve().relative_to(boundary)
                except ValueError as exc:
                    raise InvalidArgumentsError("NotebookLM export files must stay inside the study-set package.") from exc
        results.append(artifact)
    if not results and not allow_empty:
        raise InputDocumentError("No NotebookLM exports match the requested study-set types. "
                                 "Use an exported notebook containing an Artifacts directory and metadata files.")
    if warn_missing:
        for action in sorted(requested - {artifact.action for artifact in results}):
            LOGGER.warning("NotebookLM %s skipped: no matching artifact was exported.", action)
    return results


class _AppDataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.payloads = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "data-app-data" and value is not None:
                self.payloads.append(value)


def _app_data(artifact: NotebookLMArtifact) -> dict:
    text = _read(artifact.source)
    if text.lstrip().startswith(("{", "[")):
        data = _json(text, artifact.source.name)
    else:
        parser = _AppDataParser()
        try:
            parser.feed(text)
        except (ValueError, RecursionError) as exc:
            raise InputDocumentError(f"Invalid NotebookLM HTML in '{artifact.source.name}'.") from exc
        if len(parser.payloads) != 1:
            raise InputDocumentError(f"NotebookLM HTML '{artifact.source.name}' must contain exactly one data-app-data payload.")
        data = _json(parser.payloads[0], artifact.source.name)
    if not isinstance(data, dict):
        raise InputDocumentError(f"NotebookLM data in '{artifact.source.name}' must be an object.")
    return data


def _looks_json(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            return stream.read(128).lstrip(b"\xef\xbb\xbf \t\r\n").startswith((b"{", b"["))
    except OSError as exc:
        raise InputDocumentError(f"Could not read NotebookLM export '{path.name}': {exc}") from exc


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputDocumentError(f"NotebookLM {label} must be non-empty text.")
    return value


def _title(artifact: NotebookLMArtifact) -> str:
    return _text(artifact.metadata.get("title", artifact.name), "title")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "notebooklm"


def _provenance(artifact: NotebookLMArtifact) -> dict:
    # Exported Sources metadata has no source UUID. Retain those UUIDs without
    # guessing which local source title they represent.
    return {"study_set_type": "notebooklm", "conversion": "deterministic",
            "source_export": artifact.source.name, "source_metadata": artifact.metadata_path.name,
            "source_names": list(artifact.source_names), "notebooklm_source_ids": _source_ids(artifact.metadata),
            "original_metadata": artifact.metadata}


def _quiz(artifact: NotebookLMArtifact, data: dict) -> dict:
    quiz = data.get("quiz", data.get("questions"))
    if not isinstance(quiz, list) or not quiz:
        raise InputDocumentError("NotebookLM quiz needs a non-empty quiz array.")
    options = artifact.metadata.get("app", {}).get("generationOptions", {}).get("quizGenerationOptions", {})
    raw_difficulty = options.get("quizDifficulty") if isinstance(options, dict) else None
    difficulty = {"QUIZ_DIFFICULTY_EASY": "recall", "QUIZ_DIFFICULTY_MEDIUM": "understanding",
                  "QUIZ_DIFFICULTY_HARD": "analysis"}.get(raw_difficulty, "understanding")
    questions = []
    for index, question in enumerate(quiz):
        if not isinstance(question, dict):
            raise InputDocumentError(f"NotebookLM quiz question {index + 1} must be an object.")
        answers = question.get("answerOptions")
        if not isinstance(answers, list) or len(answers) != 4 or any(not isinstance(answer, dict) for answer in answers):
            raise InputDocumentError(f"NotebookLM quiz question {index + 1} must have exactly four answerOptions.")
        if any(not isinstance(answer.get("isCorrect"), bool) for answer in answers):
            raise InputDocumentError(f"NotebookLM quiz question {index + 1} needs boolean isCorrect flags.")
        correct = [i for i, answer in enumerate(answers) if answer["isCorrect"]]
        if len(correct) != 1:
            raise InputDocumentError(f"NotebookLM quiz question {index + 1} must have exactly one correct answer.")
        texts = [_text(answer.get("text"), "answer option") for answer in answers]
        rationales = [_text(answer.get("rationale"), "answer rationale") for answer in answers]
        right = correct[0]
        explanation = rationales[right] + "\n\n" + "\n".join(
            f"{texts[i]}: {rationales[i]}" for i in range(4) if i != right)
        converted = {"question": _text(question.get("question"), "question"), "options": texts,
                     "correct": right, "explanation": explanation, "difficulty": difficulty,
                     "sources": list(artifact.source_names) or [artifact.source.name]}
        questions.append(converted)
    return {"title": _title(artifact), "description": f"Imported NotebookLM quiz with {len(questions)} questions.",
            "questions": questions}


def _card_side(side, label: str) -> str:
    if isinstance(side, str):
        return _text(side, label)
    blocks = side.get("flashcardContentBlock") if isinstance(side, dict) else None
    if not isinstance(blocks, list) or not blocks:
        raise InputDocumentError(f"NotebookLM flashcard {label} needs content blocks.")
    values = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            raise InputDocumentError("NotebookLM flashcards currently support text content blocks only.")
        values.append(_text(block.get("content"), f"flashcard {label}"))
    return "\n".join(values)


def _flashcards(artifact: NotebookLMArtifact, data: dict) -> dict:
    cards = data.get("flashcards", data.get("cards"))
    if not isinstance(cards, list) or not cards:
        raise InputDocumentError("NotebookLM flashcards need a non-empty flashcards array.")
    converted = []
    for card in cards:
        if not isinstance(card, dict):
            raise InputDocumentError("NotebookLM flashcard must be an object.")
        converted.append({"type": "basic", "front": _card_side(card.get("f", card.get("front")), "front"),
                          "back": _card_side(card.get("b", card.get("back")), "back"),
                          "tags": [_slug(artifact.name)], "source_ids": list(artifact.source_names) or [artifact.source.name]})
    return {"title": _title(artifact), "description": f"Imported NotebookLM deck with {len(converted)} flashcards.",
            "cards": converted}


def _table_cells(line: str) -> list[str]:
    # Preserve escaped literal pipes; splitting naïvely changes the row shape.
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith(r"\|"):
        line = line[:-1]
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line)]


def _table(artifact: NotebookLMArtifact) -> dict:
    text = _read(artifact.source)
    lines = text.splitlines()
    start = next((i for i in range(len(lines) - 1) if "|" in lines[i] and
                  all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in _table_cells(lines[i + 1]))), None)
    if start is None:
        raise InputDocumentError("NotebookLM datatable needs a Markdown pipe table with a header separator.")
    headers = _table_cells(lines[start])
    if not headers or any(not header for header in headers):
        raise InputDocumentError("NotebookLM datatable headers must be non-empty.")
    names, seen = [], set()
    for index, header in enumerate(headers):
        name = _slug(re.sub(r"\*|`", "", header)).replace("-", "_")
        if not name[0].isalpha():
            name = "field_" + name
        if name in {"source_id", "page"}:
            name = "export_" + name
        original, suffix = name, 2
        while name in seen:
            name, suffix = f"{original}_{suffix}", suffix + 1
        names.append(name)
        seen.add(name)
    refs = _footnotes(text)
    rows = []
    for line in lines[start + 2:]:
        if not line.strip() or "|" not in line:
            break
        cells = _table_cells(line)
        if len(cells) != len(headers):
            raise InputDocumentError("NotebookLM datatable row width does not match its headers.")
        cited = [refs[ref] for cell in cells for ref in re.findall(r"\[(\d+)\]", cell) if ref in refs]
        source_id = next((_bare_name(source) for source in cited),
                         artifact.source_names[0] if artifact.source_names else artifact.source.name)
        rows.append({**dict(zip(names, cells)), "source_id": source_id, "page": "N/A"})
    if not rows:
        raise InputDocumentError("NotebookLM datatable has no data rows.")
    fields = [{"name": name, "description": header, "example": rows[0][name]}
              for name, header in zip(names, headers)]
    return {"title": _title(artifact), "fields": fields, "data": rows}


def _cancel(cancel_check):
    if cancel_check is not None and cancel_check():
        raise GenerationCancelled("NotebookLM conversion was cancelled.")


def _copy(source: Path, destination: Path, cancel_check) -> None:
    temporary = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as reader, tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as writer:
            temporary = Path(writer.name)
            while block := reader.read(1024 * 1024):
                _cancel(cancel_check)
                writer.write(block)
        _cancel(cancel_check)
        os.replace(temporary, destination)
    except OSError as exc:
        raise OutputWriteError(f"Could not preserve NotebookLM media '{source.name}': {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _clean(value, schema: dict):
    if "$ref" in schema:
        schema = NODE
    if isinstance(value, dict) and schema.get("type") == "object":
        return {key: _clean(value[key], child) for key, child in schema.get("properties", {}).items() if key in value}
    if isinstance(value, list) and schema.get("type") == "array":
        return [_clean(item, schema.get("items", {})) for item in value]
    return value


def _canonical(data: dict, action: str) -> dict:
    cleaned = _clean(data, get_schema(action))
    if action == "create_datatables" and isinstance(data.get("data"), list):
        fields = [field.get("name") for field in data.get("fields", []) if isinstance(field, dict)]
        cleaned["data"] = [{key: row[key] for key in [*fields, "source_id", "page"] if key in row}
                           if isinstance(row, dict) else row for row in data["data"]]
    return cleaned


def _infographic(artifact: NotebookLMArtifact, cancel_check) -> tuple[dict, list[dict], dict]:
    try:
        raw = artifact.source.read_bytes()
    except OSError as exc:
        raise InputDocumentError(f"Could not read infographic '{artifact.source.name}': {exc}") from exc
    slug = _slug(artifact.name) + "-" + hashlib.sha256(raw).hexdigest()[:8]
    reference = f"assets/{slug}.svg"
    if artifact.source.suffix.lower() == ".svg":
        svg = _read(artifact.source)
    else:
        mime = mimetypes.guess_type(artifact.source.name)[0]
        if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
            width, height = struct.unpack(">II", raw[16:24])
            mime = "image/png"
        elif raw.startswith(b"\xff\xd8\xff"):
            width, height, mime = 1600, 900, "image/jpeg"
            offset = 2
            while offset + 9 < len(raw):
                if raw[offset] != 255:
                    break
                marker = raw[offset + 1]
                length = struct.unpack(">H", raw[offset + 2:offset + 4])[0]
                if marker in {0xC0, 0xC1, 0xC2, 0xC3}:
                    height, width = struct.unpack(">HH", raw[offset + 5:offset + 9])
                    break
                offset += length + 2
        else:
            raise InputDocumentError("NotebookLM infographic image must be PNG, JPEG, or SVG, or provide canonical '.content.json'.")
        if width <= 0 or height <= 0:
            raise InputDocumentError("NotebookLM infographic image dimensions are invalid.")
        encoded = base64.b64encode(raw).decode("ascii")
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
               f'<image width="{width}" height="{height}" href="data:{mime};base64,{encoded}"/></svg>\n')
    data = {"title": _title(artifact), "subtitle": "", "sections": [{"type": "svg", "title": _title(artifact),
            "value": reference, "label": None, "items": [], "panel": None, "span": "full"}]}
    extraction = {}
    if artifact.source.suffix.lower() != ".svg":
        from notebooklm_media import extract_image_text
        text, extraction = extract_image_text(artifact.source, cancel_check)
        if text:
            data["sections"].append({"type": "quote", "title": "Extracted text", "value": text,
                                     "label": "Verbatim local OCR output", "items": [], "panel": None, "span": "full"})
    return data, [{"id": reference, "svg": svg}], extraction


def _svg_assets(artifact: NotebookLMArtifact, data: dict, payload: dict) -> list[dict]:
    inline = {asset.get("id"): asset.get("svg", asset.get("content")) for asset in payload.get("assets", [])
              if isinstance(asset, dict)} if isinstance(payload.get("assets"), list) else {}
    assets = []
    for section in data.get("sections", []):
        if section.get("type") != "svg":
            continue
        reference = section.get("value")
        if not isinstance(reference, str) or not re.fullmatch(r"assets/[\w-]+\.svg", reference):
            continue
        content = inline.get(reference)
        if not isinstance(content, str):
            path = artifact.metadata_path.parent / reference
            if not path.is_file():
                raise InputDocumentError(f"NotebookLM canonical infographic references missing SVG asset '{reference}'.")
            content = _read(path)
        assets.append({"id": reference, "svg": content})
    return assets


def _preserve_media(artifact: NotebookLMArtifact, output_path: Path, cancel_check) -> list[dict]:
    if artifact.action not in _MEDIA:
        return []
    if artifact.source.is_dir():
        files = [path for path in artifact.dependencies if path.is_file() and
                 path != artifact.metadata_path and path.is_relative_to(artifact.source)]
    elif artifact.source.suffix.lower() not in {".json", ".html", ".md"}:
        files = [artifact.source]
    else:
        return []
    planned = []
    for source in files:
        relative = source.relative_to(artifact.source) if artifact.source.is_dir() else Path(source.name)
        asset_path = Path("assets") / output_path.stem / relative
        destination = output_path.parent / asset_path
        if any(destination.resolve() == dependency.resolve() for dependency in artifact.dependencies):
            raise InvalidArgumentsError("NotebookLM preserved media must not overwrite its input export.")
        planned.append((source, destination, asset_path))
    inventory = []
    for source, destination, asset_path in planned:
        _cancel(cancel_check)
        if source.suffix.lower() == ".svg":
            from notebooklm_renderers import _safe_svg
            try:
                safe_svg = _safe_svg(_read(source))
            except InvalidArgumentsError as exc:
                raise InputDocumentError(f"NotebookLM SVG import is invalid: {exc}") from exc
            write_output(destination, safe_svg)
        else:
            _copy(source, destination, cancel_check)
        mime = mimetypes.guess_type(source.name)[0]
        if mime is None:
            with source.open("rb") as stream:
                signature = stream.read(12)
            mime = "image/png" if signature.startswith(b"\x89PNG\r\n\x1a\n") else (
                "image/jpeg" if signature.startswith(b"\xff\xd8\xff") else "application/octet-stream")
        inventory.append({"path": asset_path.as_posix(), "mime_type": mime})
    return inventory


def convert_notebooklm_artifact(artifact: NotebookLMArtifact, output_path: Path, *, cancel_check=None) -> PipelineResult:
    """Validate, convert and atomically write one selected artifact format."""
    output_path = Path(output_path)
    _cancel(cancel_check)
    suffix = ".wireframe.txt" if output_path.name.lower().endswith(".wireframe.txt") else output_path.suffix.lower()
    allowed = supported_notebooklm_formats(artifact)
    if suffix not in allowed:
        raise InvalidArgumentsError(f"NotebookLM {artifact.action} output supports: {', '.join(sorted(allowed))}.")
    if any(output_path.resolve() == dependency.resolve() for dependency in artifact.dependencies):
        raise InvalidArgumentsError("NotebookLM output must not overwrite its input export.")
    if artifact.source.is_dir():
        try:
            output_path.resolve().relative_to(artifact.source.resolve())
        except ValueError:
            pass
        else:
            raise InvalidArgumentsError("NotebookLM output must not be inside its exported artifact directory.")
    metadata_path = output_path.with_suffix(".metadata.json")
    if any(metadata_path.resolve() == dependency.resolve() for dependency in artifact.dependencies):
        raise InvalidArgumentsError("NotebookLM output metadata must not overwrite an input export.")
    content_path = artifact.metadata_path.with_name(artifact.name + ".content.json")
    payload, assets = None, []
    metadata = _provenance(artifact)
    if content_path.is_file():
        payload = _json(_read(content_path), content_path.name)
        if not isinstance(payload, dict):
            raise InputDocumentError("NotebookLM canonical .content.json must contain an object.")
        data = payload
        metadata["content_sidecar"] = content_path.name
    elif artifact.source.suffix.lower() == ".json" or _looks_json(artifact.source):
        payload = _json(_read(artifact.source), artifact.source.name)
        if not isinstance(payload, dict):
            raise InputDocumentError("NotebookLM canonical JSON export must contain an object.")
        data = payload
    elif artifact.action == "create_infographics":
        data, assets, extraction_metadata = _infographic(artifact, cancel_check)
        metadata["visual_import"] = "Original image embedded in a self-contained SVG; semantic section text was not exported."
        metadata.update(extraction_metadata)
    elif artifact.action == "create_slides":
        from notebooklm_media import extract_slides
        data = extract_slides(artifact.source, _title(artifact), list(artifact.source_names) or [artifact.source.name], cancel_check)
        metadata["text_extraction"] = "Local slide text extraction or installed OCR; no content generated."
        metadata["speaker_notes_policy"] = "Original speaker notes when exported; otherwise verbatim extracted bullet text."
    elif artifact.action == "create_podcasts":
        from notebooklm_media import extract_podcast
        data, transcript_metadata = extract_podcast(artifact.source, artifact.name, _title(artifact), cancel_check)
        metadata.update(transcript_metadata)
    elif artifact.action == "create_datatables":
        data = _table(artifact)
    else:
        payload = _app_data(artifact)
        if not schema_errors(payload, get_schema(artifact.action)):
            data = payload
        elif artifact.action == "create_quizzes":
            data = _quiz(artifact, payload)
        elif artifact.action == "create_flashcards":
            data = _flashcards(artifact, payload)
        else:
            data = payload
    if payload is not None:
        metadata["original_payload"] = payload
        if schema_errors(payload, get_schema(artifact.action)):
            if artifact.action == "create_quizzes" and "quiz" in payload:
                data = _quiz(artifact, payload)
            elif artifact.action == "create_flashcards" and "flashcards" in payload:
                data = _flashcards(artifact, payload)
    try:
        # Existing Q&A hints are source content; generation's ellipsis styling
        # does not justify editing an imported canonical exercise.
        normalized = data if artifact.action == "create_qandas" else normalize_artifact(data, artifact.action)
        data = _canonical(normalized, artifact.action)
    except RecursionError as exc:
        raise InputDocumentError("NotebookLM artifact exceeds the supported structure depth.") from exc
    if artifact.action == "create_qandas":
        errors = schema_errors(data, get_schema(artifact.action))
        if not errors:
            ids = [question["id"] for question in data["questions"]]
            if len(ids) != len(set(ids)):
                errors.append("Imported Q&A question ids must be unique.")
            for question in data["questions"]:
                if question["kind"] == "source_exercise" and not question["exercise_label"]:
                    errors.append("Imported source exercises need their original exercise labels.")
                if question["kind"] == "generated" and question["exercise_label"] is not None:
                    errors.append("Generated Q&A exercises must use a null exercise label.")
    elif artifact.action == "create_podcasts":
        # Imported speech is evidence, not a newly authored episode. Preserve it
        # in full even when generation-specific runtime recommendations differ.
        errors = schema_errors(data, get_schema(artifact.action))
        if not errors:
            speakers = [member["speaker_id"] for member in data["cast"]]
            if len(speakers) != len(set(speakers)) or len(speakers) != 2:
                errors.append("Imported podcast must have exactly two distinct observed speakers.")
            if {member["host_id"] for member in data["cast"]} != {"HOST_A", "HOST_B"}:
                errors.append("Imported podcast cast must identify HOST_A and HOST_B.")
            if any(scene["speaker_id"] not in speakers for segment in data["script"] for scene in segment["scenes"]):
                errors.append("Imported podcast scene speaker is not in the cast.")
        metadata["duration_policy"] = "Original imported dialogue preserved; generation duration limits are not applied."
    else:
        errors = validate_artifact(data, artifact.action, final=True)
    if errors:
        raise InputDocumentError("NotebookLM conversion failed artifact validation: " + "; ".join(errors[:5]))
    if artifact.action == "create_infographics" and not assets:
        assets = _svg_assets(artifact, data, payload or {})
    if assets:
        from notebooklm_renderers import _safe_svg
        try:
            assets = [{"id": asset["id"], "svg": _safe_svg(asset["svg"])} for asset in assets]
        except InvalidArgumentsError as exc:
            raise InputDocumentError(f"NotebookLM SVG import is invalid: {exc}") from exc
        metadata["svg_policy"] = "Declarative SVG and embedded raster images retained; active content and external references removed."
    warnings = editorial_warnings(data, artifact.action)
    if isinstance(metadata.get("warning"), str):
        warnings.append(metadata["warning"])
    validation = {"valid": True, "errors": [], "schema": artifact.action, "warnings": warnings}
    _cancel(cancel_check)
    if suffix == ".json":
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    else:
        from notebooklm_renderers import render_artifact
        text = render_artifact({**data, **({"assets": assets} if assets else {})}, artifact.action, suffix)
    original_media = _preserve_media(artifact, output_path, cancel_check)
    if original_media:
        metadata["original_media"] = original_media
    for asset in assets:
        _cancel(cancel_check)
        destination = output_path.parent / asset["id"]
        if any(destination.resolve() == dependency.resolve() for dependency in artifact.dependencies):
            raise InvalidArgumentsError("NotebookLM SVG output must not overwrite an input export.")
        write_output(destination, asset["svg"])
    _cancel(cancel_check)
    write_output(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    write_output(output_path, text)
    metrics = {"study_set_type": "notebooklm", "strategy": "conversion", "status": "completed",
               "provider_calls": 0, "source_export": artifact.source.name, "source_names": list(artifact.source_names),
               "warnings": warnings, "metadata_filename": metadata_path.name}
    return PipelineResult(text, metrics, validation, output_path.parent)
