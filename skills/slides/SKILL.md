---
name: slides
description: Build a slide deck for one topic as JSON + Markdown, each slide with bullets and speaker notes. Use when the user wants slides, a deck, or a presentation from a text.
---

# slides

A linear slide deck for one topic. Scope: **topic**.

## Files

| Path | Format |
|------|--------|
| `output/slides/slides_<topic>.json` | schema below |
| `output/slides/slides_<topic>.md`   | derived |

## JSON schema

```json
{
  "title": "string — deck title",
  "slides": [
    {
      "title": "string",
      "subtitle": "string",
      "bullets": ["string", "..."],
      "speaker_notes": "string — 1–3 sentences of what to say",
      "image_query": "string — short stock-image search phrase",
      "source_ids": ["心经_en.txt"]
    }
  ]
}
```

- ~8–12 slides: an opening title slide, body slides, and a closing
  "What to carry away" / discussion slide.
- 3–6 `bullets` per slide, each a **phrase**, not a full sentence.
- `speaker_notes` adds context a bullet cannot carry; not just a re-read of the bullets.
- `image_query` is 2–5 words. `source_ids`: bare filenames (≥1).

## Markdown derivation

```
# <deck title>

---
## <slide title>
### <slide subtitle>

- <bullet>
- <bullet>

**Speaker Notes:** <speaker_notes>

**Sources:** <source_id>, <source_id>
```

- A `---` separator **before each** slide. Omit the `### subtitle` line if `subtitle`
  is empty. `image_query` is not rendered in the `.md`.

## Checklist

- [ ] first slide is a title slide, last is a takeaway/discussion slide
- [ ] every slide: 3–6 phrase bullets, non-empty `speaker_notes`, ≥1 `source_id`
- [ ] `.md` has a `---` before every slide; bullet text matches the JSON
