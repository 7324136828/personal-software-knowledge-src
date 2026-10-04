"""Load the required, authoritative NotebookLM source identifier mapping."""

from __future__ import annotations

import json
import re
from pathlib import Path

from errors import InputDocumentError, InvalidArgumentsError


SOURCE_METADATA_TEMPLATE = '{"{metadata.json}": {"id": "source_id"}}'
_METADATA_SUFFIXES = (" metadata.json", ".metadata.json")
_UNSAFE_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED_NAMES = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def notebooklm_root(input_dir: Path) -> Path:
    """Resolve a notebook root supplied directly or through its export folder."""
    input_dir = Path(input_dir)
    root = input_dir.parent if input_dir.name.casefold() in {"artifacts", "sources"} else input_dir
    try:
        return root.resolve()
    except (OSError, RuntimeError) as exc:
        raise InputDocumentError(f"Could not resolve NotebookLM input directory '{input_dir}': {exc}") from exc


def _contained(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError) as exc:
        raise InvalidArgumentsError(
            "NotebookLM input and source metadata mapping must stay inside the study-set package."
        ) from exc


def _safe_name(value: str) -> bool:
    """Accept one literal, portable filename component without changing it."""
    return (
        bool(value)
        and value == value.strip()
        and value not in {".", ".."}
        and not value.endswith((".", " "))
        and _UNSAFE_CHARACTERS.search(value) is None
        and value.split(".", 1)[0].casefold() not in _RESERVED_NAMES
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """Keep duplicate keys visible instead of accepting json.loads' last value."""
    result, seen = {}, set()
    for key, value in pairs:
        folded = key.casefold()
        if folded in seen:
            raise ValueError(f"duplicate JSON key '{key}' (filenames are case-insensitive)")
        seen.add(folded)
        result[key] = value
    return result


def load_notebooklm_source_metadata(
    input_dir: Path, *, contained: bool = False, package_root: Path | None = None
) -> tuple[Path, dict[str, str]]:
    """Return the required mapping path and exact metadata-filename to ID pairs.

    The mapping is read from ``metadata/sources.metadata.json`` at the notebook
    root. Entries identify source metadata files by their bare filenames; their
    recorded identifiers may be opaque strings, but must be safe folder names.
    This loader does not infer identifiers or create the required file.
    """
    input_dir = Path(input_dir)
    root = notebooklm_root(input_dir)
    mapping_path = root / "metadata" / "sources.metadata.json"
    if contained:
        boundary = Path(package_root) if package_root is not None else root
        for path in (input_dir, root, mapping_path):
            _contained(path, boundary)
    if not mapping_path.is_file():
        raise InputDocumentError(
            f"Required NotebookLM source mapping is missing: '{mapping_path}'. "
            "Generate this file with each exact Sources metadata filename and its recorded "
            f"NotebookLM source ID. JSON template: {SOURCE_METADATA_TEMPLATE}"
        )
    try:
        values = json.loads(mapping_path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError) as exc:
        raise InputDocumentError(f"Could not read NotebookLM source mapping '{mapping_path}': {exc}") from exc
    if not isinstance(values, dict) or not values:
        raise InputDocumentError(
            f"NotebookLM source mapping '{mapping_path}' must contain a nonempty JSON object. "
            f"JSON template: {SOURCE_METADATA_TEMPLATE}"
        )
    mapping, identifiers = {}, {}
    for filename, entry in values.items():
        suffix = next((suffix for suffix in _METADATA_SUFFIXES
                       if filename.casefold().endswith(suffix)), None)
        if (
            not _safe_name(filename)
            or suffix is None
            or not filename[:-len(suffix)].strip()
        ):
            raise InputDocumentError(
                f"Invalid source metadata filename '{filename}' in '{mapping_path}'. "
                "Use its exact bare Sources filename ending in ' metadata.json' or '.metadata.json'."
            )
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not _safe_name(entry["id"]):
            raise InputDocumentError(
                f"Source mapping entry '{filename}' in '{mapping_path}' must be an object "
                "with a nonempty, safe string 'id'."
            )
        source_id = entry["id"]
        folded_id = source_id.casefold()
        if folded_id in identifiers:
            raise InputDocumentError(
                f"NotebookLM source ID '{source_id}' is mapped to multiple metadata filenames: "
                f"'{identifiers[folded_id]}' and '{filename}'."
            )
        identifiers[folded_id] = filename
        mapping[filename] = source_id
    return mapping_path.resolve(), mapping
