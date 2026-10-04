---
name: podcast-script
description: Build one self-contained two-host podcast script per study-set dataset as JSON, with an estimated running time of at most 20 minutes. Use when the user wants a podcast script, audio dialogue, or spoken-word walkthrough of a corpus.
---

# podcast-script

A conversational script (two hosts by default) that walks through the texts of a
dataset. Scope: **dataset** — exactly one episode per study set/source-file dataset.
Text only — **never** emit audio files.

## Files

- Single episode: `output/podcasts/<dataset>.json`
- Optional companion transcript: `output/podcasts/<dataset>.md`
- Honor the caller's output path and naming convention. A compatibility filename
  such as `<timestamp>_episode0.json` still represents the sole episode; create no
  additional episode files.

## JSON schema

```json
{
  "episode_title": "string — title of the self-contained episode",
  "podcast_show": "string — the show name",
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

Estimate speech at **150 dialogue words per minute** and include every explicit
`[pause=NNN]` in scene directions, including empty-dialogue silence scenes:

```text
estimated_minutes = dialogue_words / 150 + total_pause_milliseconds / 60000
```

* The single episode must have an estimated runtime **at most 20 minutes**.
  With no pauses, the limit is **3,000 dialogue words**; reduce that allowance by
  `150 * total_pause_milliseconds / 60000` when pauses are present.
* Aim for 15–18 minutes when the source supports useful coverage. There is **no
  minimum runtime or word count**; a short source can produce a shorter episode.
* Summarize long material and prioritize key concepts, useful examples, and a
  closing recap to fit the limit. Do not split a study set into a podcast series.
* Include one introduction and one sign-off. Keep the episode self-contained.

## Markdown companion (optional)

```
# <episode_title>
_<podcast_show>_

## <segment_name>
**<Name>:** <dialogue>
```

Skip empty-dialogue pause scenes in the `.md`.

## Checklist

- [ ] exactly one self-contained episode for the study set/source-file dataset
- [ ] estimated runtime ≤ 20 minutes, including explicit pauses; report dialogue words and runtime
- [ ] every scene `speaker_id` is in `cast`; `directions` uses only bracket tags
- [ ] filename and destination follow the caller's requirements
- [ ] no audio files written
