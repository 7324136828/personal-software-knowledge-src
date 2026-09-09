"""Lossless semantic source segmentation with separate, bounded overlap.

Offsets always refer to the original Unicode string, not a normalized copy. An
atomic block larger than the budget is returned with ``oversized=True`` so the
caller can fail explicitly or choose a larger model; it is never cut silently.
Malformed unmatched markup is bounded at a nearby recovery delimiter so one OCR
error cannot hide every later section or exercise.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import re
from dataclasses import asdict, dataclass
from typing import Callable

TokenCounter = Callable[[str], int]


def estimate_tokens(text: str) -> int:
    """Conservative fallback, useful without a provider tokenizer."""
    return math.ceil(len(text.encode("utf-8")) / 3)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    sequence: int
    source: str
    section: str
    text: str
    start: int
    end: int
    overlap_text: str = ""
    estimated_input_tokens: int = 0
    oversized: bool = False
    atomic_kind: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_TEX_HEADING = re.compile(r"^\s*\\(chapter|section|subsection|subsubsection)\*?\{(.+)\}\s*$")
_NUMBER_HEADING = re.compile(r"^\s*(\d+(?:\.\d+)+)(?![.\d])\s*([A-Za-z][^\n]{2,150})\s*$")
_EXPLICIT_EXERCISE = re.compile(r"^\s*(?:Exercise|Problem|Question)\s+(\d+(?:\.\d+)*(?:[a-z])?)\b[.:)?\s-]*(.*)", re.I)
_NUMBER_EXERCISE = re.compile(r"^\s*(\d+(?:\.\d+)*)(?:\.|\))\s+\S")
_LIST = re.compile(r"^\s*(?:[-+*]\s+|\(?[a-zivx]+\)\s+|\d+[.)]\s+|\\item\b)", re.I)
_ENV = re.compile(r"\\(begin|end)\{([^}]+)\}")
_MATH_ENV = {"equation", "align", "aligned", "gather", "gathered", "multline", "split", "eqnarray", "array", "matrix", "pmatrix", "bmatrix", "vmatrix", "cases", "displaymath", "math"}


def _lines(text: str) -> list[tuple[int, int, str]]:
    out, offset = [], 0
    for line in text.splitlines(keepends=True):
        out.append((offset, offset + len(line), line))
        offset += len(line)
    return out


def _structural_spans(text: str) -> list[tuple[int, int, str]]:
    """Code and TeX/math spans with bounded recovery for malformed input."""
    spans: list[tuple[int, int, str]] = []
    fence = None
    for start, end, line in _lines(text):
        mark = re.match(r"^\s*(`{3,}|~{3,})", line)
        if mark and fence is None:
            fence = (mark.group(1)[0], len(mark.group(1)), start)
        elif fence and re.match(r"^\s*" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*$", line):
            spans.append((fence[2], end, "code"))
            fence = None
    if fence:
        # A later heading is stronger evidence of recovered document structure
        # than treating the entire remaining book as one malformed code block.
        heading = re.search(r"(?m)^(?:#{1,6}\s+|\s*\\(?:chapter|section|subsection)\b|\s*\d+(?:\.\d+)+\s+[A-Za-z])", text[fence[2] + 1:])
        end = fence[2] + 1 + heading.start() if heading else len(text)
        spans.append((fence[2], end, "unclosed_code"))

    def in_code(position: int) -> bool:
        return any(a <= position < b for a, b, _ in spans)

    stack: list[tuple[str, int]] = []
    for match in _ENV.finditer(text):
        if in_code(match.start()) or match.group(2) == "document":
            continue
        env = match.group(2)
        if match.group(1) == "begin":
            stack.append((env, match.start()))
        elif stack and stack[-1][0] == env:
            name, start = stack.pop()
            if not stack:
                kind = "formula" if name.rstrip("*") in _MATH_ENV else "latex_environment"
                spans.append((start, match.end(), kind))
    if stack:
        for _, start in stack:
            candidates = []
            for marker in (r"\]", "$$", "\n\n"):
                position = text.find(marker, start + 1)
                if position >= 0:
                    candidates.append(position + len(marker))
            # Limit damage from OCR-dropped \end{...}; an unmatched environment
            # cannot legitimately consume an arbitrary remainder of a textbook.
            end = min(candidates) if candidates else min(len(text), start + 16_384)
            spans.append((start, end, "unclosed_latex_environment"))
    for opening, closing, kind in ((r"\[", r"\]", "formula"), (r"\(", r"\)", "formula"), ("$$", "$$", "formula")):
        cursor = 0
        while True:
            start = text.find(opening, cursor)
            if start < 0:
                break
            end = text.find(closing, start + len(opening))
            end = len(text) if end < 0 else end + len(closing)
            if not in_code(start):
                spans.append((start, end, kind))
            cursor = max(end, start + len(opening))
    # Single-dollar inline math, excluding escaped dollars and display math.
    for match in re.finditer(r"(?<![\\$])\$(?!\$)(?:\\.|[^$\n])+?(?<!\\)\$(?!\$)", text):
        if not in_code(match.start()):
            spans.append((match.start(), match.end(), "formula"))
    return sorted(spans)


def _heading(line: str) -> tuple[str, str] | None:
    line = line.strip()
    match = _TEX_HEADING.match(line)
    if match:
        title = match.group(2).strip()
        label = re.match(r"\d+(?:\.\d+)*", title)
        return (label.group() if label else "", title)
    match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
    if match:
        title = match.group(1)
        label = re.match(r"\d+(?:\.\d+)*", title)
        return (label.group() if label else "", title)
    match = re.match(r"^(Chapter\s+\d+)\b[.:\s]*(.*)$", line, re.I)
    if match:
        return match.group(1), line
    match = _NUMBER_HEADING.match(line)
    if match and not _EXPLICIT_EXERCISE.match(line):
        # OCR page headers repeat section titles with an appended page number.
        return match.group(1), match.group(1) + " " + re.sub(r"\s+\d{1,4}$", "", match.group(2)).strip()
    if re.fullmatch(r"(?:Exercises|Review Questions|Practice Problems|Problems)", line, re.I):
        return "", line
    return None


def _stable_id(kind: str, start: int, text: str) -> str:
    return f"{kind}-{start:08d}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"


def source_inventory(text: str) -> dict:
    """Return source-derived section, exercise and formula evidence.

    This is a transparent lexical inventory, not a claim that arbitrary OCR or
    prose questions have been exhaustively recognized.
    """
    spans = _structural_spans(text)
    excluded = [(a, b) for a, b, kind in spans if kind != "formula"]
    sections, exercises = [], []
    seen_sections: set[str] = set()
    exercise_area = False
    for start, end, line in _lines(text):
        if any(a <= start < b for a, b in excluded):
            continue
        heading = _heading(line)
        if heading:
            label, title = heading
            key = label or title.casefold()
            if key not in seen_sections:
                sections.append({"id": _stable_id("section", start, title), "label": label, "title": title, "text": line.strip(), "start": start, "end": end})
                seen_sections.add(key)
            exercise_area = bool(re.search(r"exercise|problem|review question|practice", title, re.I))
            continue
        match = _EXPLICIT_EXERCISE.match(line)
        if not match and exercise_area:
            match = _NUMBER_EXERCISE.match(line)
        if match:
            label = match.group(1)
            exercises.append({"id": _stable_id("exercise", start, label), "label": label, "text": line.strip(), "start": start, "end": end})
    boundaries = sorted([s["start"] for s in sections] + [e["start"] for e in exercises] + [len(text)])
    for exercise in exercises:
        exercise["end"] = boundaries[bisect.bisect_right(boundaries, exercise["start"])]
        exercise["text"] = text[exercise["start"]:exercise["end"]].strip()
    for index, section in enumerate(sections):
        section["end"] = sections[index + 1]["start"] if index + 1 < len(sections) else len(text)
    formulas = []
    for start, end, kind in spans:
        if kind != "formula" or any(a <= start and end <= b for a, b, _ in spans if (a, b) != (start, end)):
            continue
        if formulas and (start, end) == (formulas[-1]["start"], formulas[-1]["end"]):
            continue
        value = text[start:end]
        formulas.append({"id": _stable_id("formula", start, value), "text": value, "start": start, "end": end})
    return {"sections": sections, "exercises": exercises, "formulas": formulas, "method": "lexical_source_inventory"}


def _protected_spans(text: str, inventory: dict) -> list[tuple[int, int, str]]:
    spans = _structural_spans(text)
    spans.extend((e["start"], e["end"], "exercise") for e in inventory["exercises"])
    lines = _lines(text)
    index = 0
    while index < len(lines):
        start, end, line = lines[index]
        kind = "table" if "|" in line and line.count("|") >= 2 else "list" if _LIST.match(line) else None
        if not kind:
            index += 1
            continue
        last = index + 1
        while last < len(lines):
            candidate = lines[last][2]
            if kind == "table":
                if candidate.count("|") < 2:
                    break
            elif _heading(candidate) or not candidate.strip():
                if not candidate.strip() and last + 1 < len(lines) and _LIST.match(lines[last + 1][2]):
                    last += 1
                    continue
                break
            last += 1
        spans.append((start, lines[last - 1][1], kind))
        index = last
    # Union intersecting intervals. Adjacent blocks can still be separated.
    merged: list[tuple[int, int, str]] = []
    for start, end, kind in sorted(spans):
        if merged and start < merged[-1][1]:
            prior_start, prior_end, prior_kind = merged[-1]
            merged[-1] = (prior_start, max(end, prior_end), prior_kind if prior_kind == kind else "atomic_blocks")
        else:
            merged.append((start, end, kind))
    return merged


def chunk_source(text: str, source: str, target_tokens: int, overlap_tokens: int = 0,
                 token_counter: TokenCounter | None = None) -> list[Chunk]:
    """Chunk exactly once, preferring headings, exercises and paragraphs.

    ``target_tokens`` bounds core text; overlap has its own independent budget.
    The caller must reserve both in the request budget. Atomic oversized chunks
    are explicitly marked, never discarded or partially copied.
    """
    if target_tokens < 1 or overlap_tokens < 0:
        raise ValueError("target_tokens must be positive and overlap_tokens nonnegative")
    if not text:
        return []
    count = token_counter or estimate_tokens
    inventory = source_inventory(text)
    structural = _structural_spans(text)
    protected = _protected_spans(text, inventory)
    starts = [a for a, _, _ in protected]

    def containing(position: int) -> tuple[int, int, str] | None:
        index = bisect.bisect_right(starts, position) - 1
        if index >= 0:
            span = protected[index]
            if span[0] < position < span[1]:
                return span
        return None

    structural_union: list[tuple[int, int, str]] = []
    for start, end, kind in structural:
        if kind.startswith("unclosed_"):
            # Malformed markup is a soft boundary: preserve it when it fits, but
            # permit a fallback split rather than making the document impossible
            # to process on a finite-context model.
            continue
        if structural_union and start < structural_union[-1][1]:
            prior_start, prior_end, _ = structural_union[-1]
            structural_union[-1] = (prior_start, max(prior_end, end), "structural")
        else:
            structural_union.append((start, end, kind))
    structural_starts = [a for a, _, _ in structural_union]

    def containing_structural(position: int) -> tuple[int, int, str] | None:
        index = bisect.bisect_right(structural_starts, position) - 1
        if index >= 0:
            span = structural_union[index]
            if span[0] < position < span[1]:
                return span
        return None

    choices: dict[int, int] = {0: 0, len(text): 0}
    for item in inventory["sections"]:
        choices[item["start"]] = 0
    for item in inventory["exercises"]:
        choices[item["start"]] = 1
    for match in re.finditer(r"\n[ \t\r]*\n", text):
        choices.setdefault(match.end(), 2)
    for start, end, _ in protected:
        choices.setdefault(start, 3)
        choices.setdefault(end, 3)
    # Line boundaries work with OCR sources that have no blank paragraphs.
    for _, end, _ in _lines(text):
        choices.setdefault(end, 4)
    # Whitespace fallback is lossless and never cuts an inline formula.
    for match in re.finditer(r"\s+", text):
        choices.setdefault(match.end(), 5)
    boundaries = sorted(p for p in choices if not containing(p))
    chunks, cursor = [], 0
    section_starts = [s["start"] for s in inventory["sections"]]
    while cursor < len(text):
        low, high = cursor, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if count(text[cursor:mid]) <= target_tokens:
                low = mid
            else:
                high = mid - 1
        fitting = boundaries[bisect.bisect_right(boundaries, cursor):bisect.bisect_right(boundaries, low)]
        if fitting:
            farthest = fitting[-1]
            # Prefer a semantic boundary when it fills at least half the budget.
            threshold = cursor + max(1, (farthest - cursor) // 2)
            good = [p for p in fitting if p >= threshold]
            end = min(good, key=lambda p: (choices[p], -p))
            oversized, kind = False, None
        else:
            span = next((s for s in protected if s[0] <= cursor < s[1]), None)
            if span:
                end, kind = span[1], span[2]
                oversized = count(text[cursor:end]) > target_tokens
                if oversized and kind in {"exercise", "list", "table", "atomic_blocks"}:
                    # When a composite exercise/list/table cannot fit at all, split
                    # only as a last resort and only at paragraph/line boundaries
                    # outside equations, code, and LaTeX environments. This retains
                    # subquestions and rows intact far more often than a hard token cut.
                    relaxed = [
                        point for point, priority in choices.items()
                        if cursor < point <= low and priority <= 5 and not containing_structural(point)
                    ]
                    if relaxed:
                        end = max(relaxed)
                        oversized = False
                        kind = "split_" + kind
            else:
                end = max(cursor + 1, low)
                if (span := containing(end)):
                    end = span[0] if span[0] > cursor else span[1]
                kind = None
                oversized = count(text[cursor:end]) > target_tokens
        overlap_start = cursor
        if overlap_tokens and chunks:
            candidates = boundaries[bisect.bisect_left(boundaries, chunks[-1].start):bisect.bisect_right(boundaries, cursor)]
            lo, hi = 0, len(candidates) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if count(text[candidates[mid]:cursor]) <= overlap_tokens:
                    hi = mid
                else:
                    lo = mid + 1
            if candidates and count(text[candidates[lo]:cursor]) <= overlap_tokens:
                overlap_start = candidates[lo]
        section_index = bisect.bisect_right(section_starts, cursor) - 1
        section = inventory["sections"][section_index]["title"] if section_index >= 0 else "Preamble"
        core, overlap = text[cursor:end], text[overlap_start:cursor]
        sequence = len(chunks) + 1
        chunks.append(Chunk(f"chunk_{sequence:04d}", sequence, str(source), section, core, cursor, end,
                            overlap, count(overlap + core), oversized, kind))
        cursor = end
    return chunks
