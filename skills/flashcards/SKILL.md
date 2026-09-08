---
name: flashcards
description: Build a study deck for one topic as paired JSON + tab-separated TXT. Use when the user wants flashcards, Anki cards, or spaced-repetition cards from a text.
---

# flashcards

A deck of recall cards for one topic. Scope: **topic**.

## Files

| Path | Format |
|------|--------|
| `output/flashcards/flashcards_<topic>.json` | schema below |
| `output/flashcards/flashcards_<topic>.txt`  | derived; import-friendly |

## JSON schema

```json
{
  "title": "string",
  "description": "string — one or two sentences; note if this is 'part one' of a series",
  "cards": [
    {
      "type": "basic | cloze | definition | concept",
      "front": "string",
      "back": "string",
      "tags": ["kebab-case", "..."],
      "source_ids": ["心经_en.txt"]
    }
  ]
}
```

Card `type` semantics:

| type | front | back |
|------|-------|------|
| `definition` | a term or named set | its meaning |
| `cloze` | a sentence with one or more `______` blanks | what fills them (`"emptiness (both blanks)"`) |
| `basic` | a direct recall question | the short answer |
| `concept` | a "why" / "how" / "is X really Y?" question | a 1–3 sentence reasoning answer |

- 10–50 cards. If a topic needs more, split into `flashcards_<topic>_part2.json` with a
  shared series `title` and a `description` that says "part two".
- `tags`: 1–3 kebab-case labels; first tag is usually the topic slug.
- `source_ids`: bare filenames the card is drawn from (≥1).

## TXT derivation

```
# <title>
# <description>
<front>\t<back>
<front>\t<back>
...
```

- Two `#` comment lines, then one card per line: `front`, a single TAB, `back`.
- Keep `______` blanks literal. Strip any TAB/newline inside `front`/`back` to a space.
- Card order identical to the JSON. LF endings, trailing newline, no BOM.

## Checklist

- [ ] every card has a valid `type`, non-empty `front`/`back`, ≥1 `source_id`
- [ ] `cloze` fronts contain `______`; `back` explains the fill
- [ ] TXT line count = card count; exactly one TAB per line
