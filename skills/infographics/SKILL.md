---
name: infographics
description: Build a panelled infographic for one topic as JSON plus Markdown, self-contained HTML, a tall poster SVG, an ASCII wireframe, and optional diagram assets. Use when the user wants an infographic, a visual one-pager, or a poster summary of a text.
---

# infographics

A panelled visual summary of one topic. Scope: **topic**.

## Files

| Path | Format |
|------|--------|
| `output/infographics/infographic_<topic>.json` | schema below — the source of truth |
| `output/infographics/infographic_<topic>.md`   | derived |
| `output/infographics/infographic_<topic>.html` | derived, self-contained, dark-mode aware |
| `output/infographics/infographic_<topic>.svg`  | derived — single tall poster |
| `output/infographics/infographic_<topic>.wireframe.txt` | derived — ASCII layout |
| `output/infographics/assets/<slug>.svg` | optional — a diagram referenced by a `svg` section |

## JSON schema

```json
{
  "title": "string",
  "subtitle": "string",
  "sections": [
    {
      "type": "quote | stat | flow | comparison | chart | svg",
      "title": "string",
      "value": "string | null",
      "label": "string | null",
      "items": ["string", "..."],
      "panel": "string | null",
      "span": "full | half"
    }
  ]
}
```

Every section object has **all seven keys**; unused ones are `null` or `[]`.

| type | uses | meaning |
|------|------|---------|
| `quote` | `value` = the quotation, `label` = attribution | a pulled quote |
| `stat` | `value` = headline number/phrase, `label` = context | a key figure |
| `flow` | `items` = ordered steps | a numbered sequence / arrow chain |
| `comparison` | `items` = `"A vs B"` lines | side-by-side contrasts |
| `chart` | `items` = `"label: number"` lines | a bar chart |
| `svg` | `value` = `assets/<slug>.svg`, `label` = caption/alt | an embedded diagram |

- `panel` groups **consecutive** sections under one heading; `null` = ungrouped.
  Sections sharing a `panel` string must be adjacent, in order.
- `span`: `full` = own row; `half` = shares a row with the next `half` section.
- Aim for 2–4 panels, ~4–8 sections.

## Markdown derivation

```
# <title>

*<subtitle>*

## Panel N: <panel>          (heading only when panel changes; N counts panels)

### <section title>
<body per type>
```

Per-type body: `quote` → blockquote lines then `> — <label>`; `stat` → `**<value>**`
then an indented `<label>`; `flow` / `chart` → `1.`-numbered `items`; `comparison` →
numbered `items`; `svg` → `![<label>](<value>)`.

## HTML derivation

One standalone page: `:root` CSS variables with a `@media (prefers-color-scheme: dark)`
override, `<main>` max-width ~1040px, each panel a rounded `.panel` card with a
numbered circle, `half` sections laid out two-up (grid/flex). `chart` renders as
labelled horizontal bars sized from the numbers. Inline `<style>` only; the sole
external references allowed are the `assets/*.svg` files.

## SVG derivation (`.svg`)

A single **poster**: `width="900"`, `height` computed from content, `viewBox="0 0 900 H"`,
a full-bleed background `<rect>`, title + subtitle `<text>`, numbered panel badges, and
each section rendered with `<text>` / `<rect>` / simple bars. Pure SVG 1.1 — no scripts,
no foreignObject, no external fonts (use `font-family="Segoe UI, system-ui, sans-serif"`).

## Wireframe derivation (`.wireframe.txt`)

ASCII mockup: a `+===+` banner for title/subtitle, `+---+` boxes for panels and
sections, each section line `[type] <title> :: <value/first items> — <label>`. `half`
pairs are drawn as two columns split by ` | `. Fixed width ~78 columns.

## Diagram assets (`assets/<slug>.svg`)

Each is standalone: `<svg xmlns=... viewBox=... role="img" aria-label="...">`, a
`viewBox` (not just width/height), theme-neutral palette, no scripts. Referenced by a
`svg` section's `value` as `assets/<slug>.svg` (path relative to the infographic files).

## Checklist

- [ ] every section has all 7 keys; `type` in the allowed set; `span` in {full, half}
- [ ] sections with the same `panel` are contiguous and ordered
- [ ] `chart` items all match `label: number`; `svg` `value` points to an existing asset
- [ ] `.html` and every `assets/*.svg` are self-contained (no external scripts/fonts)
- [ ] `.md`, `.svg`, `.wireframe.txt` all reflect the same section list as the JSON
