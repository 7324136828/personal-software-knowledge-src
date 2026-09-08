"""Loading of authoritative generation skill specifications."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from errors import SkillFileError


@dataclass(frozen=True)
class LoadedSkill:
    """The path and complete text of a loaded skill specification."""

    name: str
    path: Path
    text: str


_SKILL_ALIASES = {
    # The supplied skill set uses this historical folder name.
    "podcasts": "podcast-script",
}


def load_skill(skill_name: str, *, skills_root: Path | None = None) -> LoadedSkill:
    """Load the complete ``SKILL.md`` for a configured skill."""

    if not re.fullmatch(r"[a-z][a-z0-9_-]*", skill_name):
        raise SkillFileError(f"Invalid skill name: {skill_name!r}")

    root = skills_root or Path(__file__).resolve().parent / "skills"
    candidates = [skill_name]
    if alias := _SKILL_ALIASES.get(skill_name):
        candidates.append(alias)

    checked: list[Path] = []
    for candidate in candidates:
        path = root / candidate / "SKILL.md"
        checked.append(path)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise SkillFileError(f"Could not read skill file '{path}': {exc}") from exc
        if not text.strip():
            raise SkillFileError(f"Skill file is empty: {path}")
        return LoadedSkill(name=skill_name, path=path, text=text)

    locations = ", ".join(str(path) for path in checked)
    raise SkillFileError(f"Missing SKILL.md for '{skill_name}'. Checked: {locations}")
