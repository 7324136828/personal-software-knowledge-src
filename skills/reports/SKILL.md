---
name: reports
description: Build a structured analytical report for one topic as JSON + Markdown + self-contained HTML, with every claim cited. Use when the user wants a written report, analysis, or briefing on a text or corpus.
---

# reports

A sectioned prose report with a cited claim list under each section. Scope: **topic**
(a single text, or a cross-text theme such as `report_emptiness_across_traditions`).

## Files

| Path | Format |
|------|--------|
| `output/reports/report_<topic>.json` | schema below |
| `output/reports/report_<topic>.md`   | derived |
| `output/reports/report_<topic>.html` | derived, self-contained |

## JSON schema

```json
{
  "title": "string",
  "executive_summary": "string — one dense paragraph",
  "sections": [
    {
      "title": "string",
      "content": "string — 1–3 paragraphs of prose",
      "claims": [
        {
          "claim": "string — one verifiable assertion",
          "citations": [
            { "source_id": "心经_en.txt", "page": null, "chunk_id": "opening" }
          ]
        }
      ]
    }
  ],
  "conclusions": "string — one paragraph"
}
```

- 3–7 `sections`. Each `content` is prose, not bullets.
- Every `claim` has **≥1 citation**. `source_id` is a bare filename; `page` is a
  number or `null`; `chunk_id` is a short slug or `null`.

## Markdown derivation

```
# <title>

## Executive Summary
<executive_summary>

## <section title>
<section content>

### Key Claims
- <claim>
  Sources: [<source_id>:<chunk_id>], [<source_id>:<chunk_id>]
- ...

## Conclusions
<conclusions>
```

In a `Sources:` entry drop `:` and `chunk_id` when `chunk_id` is null (just `[source_id]`).

## HTML derivation

Standalone document: `<h1>` title, `<h2>` for Executive Summary / each section /
Conclusions, section prose in `<p>`, each claim in `<div class="claim">` with its
citations in `<span class="citation">`. Inline `<style>` only (a light `.claim`
left-border and muted `.citation` is enough). No external assets.

## Checklist

- [ ] every claim carries ≥1 citation; `page` is number-or-null
- [ ] `.md` has the four heading kinds in order; `Sources:` line per claim
- [ ] `.html` is self-contained and valid; section text matches the JSON
