---
name: podcast-script
description: Build a two-host podcast script over a dataset as JSON, split into numbered episodes when long, with a minimum total running time. Use when the user wants a podcast script, audio dialogue, or spoken-word walkthrough of a corpus.
---

# podcast-script

A conversational script (two hosts by default) that walks through the texts of a
dataset. Scope: **dataset**. Text only — **never** emit audio files.

## Files

- Single episode: `output/podcasts/<dataset>.json`
- Split: `output/podcasts/<dataset>_ep01.json`, `<dataset>_ep02.json`, …
- Optional companion transcript: `output/podcasts/<dataset>.md` (or `_epNN.md`)

## JSON schema

```json
{
  "episode_title": "string — include 'Episode N of M' when split",
  "podcast_show": "string — the show name, constant across episodes",
  "cast": [
    {
      "speaker_id": "maya",
      "host_id": "HOST_A",
      "name": "Maya",
      "voice_file": "af_heart",
      "style": "one sentence describing delivery"
    }
  ],
  "script": [
    {
      "segment_name": "string",
      "scenes": [
        { "speaker_id": "maya", "directions": "[calm] [pause=400]", "dialogue": "string" }
      ]
    }
  ]
}
```

- `cast`: 2 hosts. `host_id` is `HOST_A` / `HOST_B` (add `GUEST_1`… only if used).
  `voice_file` is a voice tag (e.g. `af_heart`, `am_michael`).
- Every scene `speaker_id` must match a `cast[].speaker_id`.
- `directions` is optional. Bracket tags only: mood (`[calm]`, `[upbeat]`,
  `[reflective]`) and `[pause=NNN]` in milliseconds. A scene with `"dialogue": ""`
  and a `[pause=NNN]` is a deliberate silence (fine as the last scene).
- `script`: a cold-open/intro segment, 3–6 body segments, a closing-practice/sign-off
  segment. Alternate speakers; keep turns ~40–120 words.

## Running-time rule

Spoken rate ≈ **150 words per minute**, counting only `dialogue` text.

- Minimum total: **20 minutes ⇒ ≥ 4,500 words** of dialogue across all episode files.
- If one episode would exceed ~25 min (~3,800 words), **split**. Each episode file is
  self-contained: its own intro and sign-off, its own `episode_title` ending
  `"— Episode N of M"`, `podcast_show` unchanged. The episodes together must still
  clear 20 minutes.
- Name split files `<dataset>_epNN.json` with zero-padded 2-digit `NN`.

## Markdown companion (optional)

```
# <episode_title>
_<podcast_show>_

## <segment_name>
**<Name>:** <dialogue>
```

Skip empty-dialogue pause scenes in the `.md`.

## Checklist

- [ ] total dialogue words ≥ 4,500 (sum across episodes); note the count when reporting
- [ ] every scene `speaker_id` is in `cast`; `directions` uses only bracket tags
- [ ] split files are individually self-contained and numbered `epNN`
- [ ] no audio files written
