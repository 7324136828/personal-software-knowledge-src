"""Portable relative paths for uploaded study-set packages."""

from __future__ import annotations

from pathlib import PureWindowsPath
import re

from errors import InvalidArgumentsError


_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$", re.IGNORECASE)


def relative_package_parts(value: str, *, allow_parent: bool = False) -> tuple[str, ...]:
    """Reject paths that Windows can reinterpret or that are not relative."""

    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/") or PureWindowsPath(value).drive:
        raise InvalidArgumentsError(f"Package path must be relative: {value!r}")
    parts = normalized.rstrip("/").split("/")
    for part in parts:
        if allow_parent and part in {".", ".."}:
            continue
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or any(ord(character) < 32 or character in '<>:"|?*' for character in part)
                or _DEVICE.fullmatch(part.split(".", 1)[0].rstrip(" ."))):
            raise InvalidArgumentsError(f"Unsafe package path: {value!r}")
    return tuple(parts)
