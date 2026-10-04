"""The single-episode runtime policy shared by podcast entry points."""

from __future__ import annotations

import json
import re


PODCAST_MAX_MINUTES = 45
PODCAST_WORDS_PER_MINUTE = 150
PODCAST_MAX_MILLISECONDS = PODCAST_MAX_MINUTES * 60_000
PODCAST_MILLISECONDS_PER_WORD = 60_000 // PODCAST_WORDS_PER_MINUTE

PODCAST_PROMPT_CONSTRAINT = f"""PODCAST RUNTIME AND EPISODE LIMIT
Generate exactly one complete podcast episode for this study set. Never split it into
multiple episodes, return an episode array, or promise another episode. Its estimated
runtime must be at most {PODCAST_MAX_MINUTES} minutes, using {PODCAST_WORDS_PER_MINUTE} spoken dialogue words per minute plus
all explicit [pause=NNN] durations in milliseconds. Count only spoken dialogue, not
titles, speaker labels, JSON keys, cast descriptions, or other metadata. Without
pauses the hard maximum is {PODCAST_MAX_MINUTES * PODCAST_WORDS_PER_MINUTE:,} dialogue words; pauses reduce that allowance.
There is no minimum runtime or word count. Prefer a shorter focused overview when
the source is long, selecting its central ideas and representative examples. Include
one introduction and one sign-off. These episode and runtime limits take precedence
over any conflicting minimum-length, series, or episode-splitting instructions."""

_PAUSE = re.compile(r"\[pause=(\d+)\]", re.I)
_DIRECTION = re.compile(r"\[(?:[A-Za-z][A-Za-z -]*|pause=\d+)\]", re.I)
_EPISODE_SERIES = re.compile(r"\bEpisode\s+\d+\s+of\s+(\d+)\b", re.I)
_SPEAKER = re.compile(r"^\s*(?:\*\*[^*\n]+?\*\*\s*:|\*\*[^*\n]+?:\*\*|[A-Za-z][\w .'-]{0,60}:)\s*")


def _decode(value):
    """Recognize canonical JSON even when a provider surrounds it with a fence."""
    if not isinstance(value, str):
        return value
    clean = value.strip().removeprefix("\ufeff")
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", clean, re.S | re.I)
    if fenced:
        clean = fenced.group(1)
    try:
        return json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        return value


def _transcript_lines(text: str):
    """Extract spoken lines; count unlabelled prose conservatively as dialogue.

    Canonical Markdown headings, the show-name subtitle and fences are metadata.
    Everything else counts, including continuation lines and plain transcripts.
    This keeps changing transcript formatting from evading the duration limit.
    """
    subtitle_allowed = False
    title_seen = False
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        if clean.startswith("#"):
            subtitle_allowed = not title_seen and bool(re.match(r"^#\s+", clean))
            title_seen = True
            continue
        if clean.startswith("```"):
            subtitle_allowed = False
            continue
        if subtitle_allowed and re.fullmatch(r"_[^_\n]+_", clean):
            subtitle_allowed = False
            continue
        subtitle_allowed = False
        yield _SPEAKER.sub("", clean, count=1)


def podcast_runtime(value: dict | str) -> dict:
    """Return dialogue-only word count, pause time and estimated runtime."""
    decoded = _decode(value)
    words = pauses = 0
    if isinstance(decoded, dict):
        segments = decoded.get("script", [])
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                continue
            scenes = segment.get("scenes", [])
            for scene in scenes if isinstance(scenes, list) else []:
                if not isinstance(scene, dict):
                    continue
                dialogue = scene.get("dialogue", "")
                if isinstance(dialogue, str):
                    words += len(dialogue.split())
                directions = scene.get("directions", "")
                if isinstance(directions, str):
                    pauses += sum(int(match) for match in _PAUSE.findall(directions))
    elif isinstance(decoded, str):
        for line in _transcript_lines(decoded):
            pauses += sum(int(match) for match in _PAUSE.findall(line))
            words += len(_DIRECTION.sub("", line).split())
    return {
        "dialogue_words": words,
        "pause_milliseconds": pauses,
        "estimated_duration_minutes": (words * PODCAST_MILLISECONDS_PER_WORD + pauses) / 60_000,
    }


def podcast_errors(value) -> list[str]:
    """Check episode count and the exact runtime boundary without a minimum."""
    decoded = _decode(value)
    errors = []
    if not isinstance(decoded, (dict, str)) or (isinstance(decoded, dict) and "episodes" in decoded):
        errors.append("Exactly one podcast episode is required per study set; episode arrays and wrappers are not allowed.")
    if isinstance(decoded, dict):
        titles = [decoded.get("episode_title", "")]
    elif isinstance(decoded, str):
        # Inspect metadata headings or standalone episode labels, never spoken dialogue.
        titles = [line.strip().lstrip("# ") for line in decoded.splitlines()
                  if line.lstrip().startswith("#") or re.match(r"^\s*Episode\s+\d+\s+of\s+\d+\s*$", line, re.I)]
    else:
        titles = []
    if any(isinstance(title, str) and any(int(total) > 1 for total in _EPISODE_SERIES.findall(title)) for title in titles):
        errors.append("Exactly one podcast episode is required per study set; 'Episode N of M' must not describe a series.")
    runtime = podcast_runtime(value)
    duration = runtime["dialogue_words"] * PODCAST_MILLISECONDS_PER_WORD + runtime["pause_milliseconds"]
    if duration > PODCAST_MAX_MILLISECONDS:
        errors.append(
            f"Podcast estimated runtime is {runtime['estimated_duration_minutes']:.3f} minutes "
            f"({runtime['dialogue_words']} dialogue words and {runtime['pause_milliseconds']} ms of pauses); "
            f"the maximum is {PODCAST_MAX_MINUTES} minutes at {PODCAST_WORDS_PER_MINUTE} words per minute including pauses. Shorten this single episode."
        )
    return errors
