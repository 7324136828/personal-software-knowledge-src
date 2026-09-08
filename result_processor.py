"""Minimal cleanup and format validation for provider responses."""

from __future__ import annotations

import json
import re
from pathlib import Path

from errors import ProviderError

_OPENING_FENCE = re.compile(r"^```(?:json|csv|html|markdown|md|mermaid|svg|text|txt)?\s*$", re.I)


def _remove_outer_code_fence(content: str) -> str:
    lines = content.splitlines(keepends=True)
    if len(lines) < 2:
        return content
    if not _OPENING_FENCE.match(lines[0].strip()) or lines[-1].strip() != "```":
        return content
    return "".join(lines[1:-1])


def process_result(content: str, output_path: Path | None) -> str:
    """Remove accidental wrappers and validate structured output when possible."""

    if not isinstance(content, str) or not content.strip():
        raise ProviderError("The selected connector returned an empty model response.")
    content = content.removeprefix("\ufeff")
    if output_path is not None and output_path.suffix.lower() != ".md":
        content = _remove_outer_code_fence(content)
    if not content.strip():
        raise ProviderError("The model response contained no artifact content.")

    if output_path is not None and output_path.suffix.lower() == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "The model returned invalid JSON for the requested .json output "
                f"(line {exc.lineno}, column {exc.colno}): {exc.msg}"
            ) from exc
    return content
