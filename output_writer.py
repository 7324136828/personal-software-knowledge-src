"""Safe persistence for generated text artifacts."""

from __future__ import annotations

from pathlib import Path

from errors import OutputWriteError


def write_output(path: Path, content: str) -> None:
    """Write UTF-8 content exactly to the requested path, creating its parent."""

    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        raise OutputWriteError(f"Could not write output file '{path}': {exc}") from exc
