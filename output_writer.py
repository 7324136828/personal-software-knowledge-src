"""Safe persistence for generated text artifacts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from errors import OutputWriteError


def write_output(path: Path, content: str) -> None:
    """Atomically replace UTF-8 output so readers keep seeing the previous file."""

    path = Path(path)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    except OSError as exc:
        raise OutputWriteError(f"Could not write output file '{path}': {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Cleanup must not hide the error from writing/replacing the output.
                pass
