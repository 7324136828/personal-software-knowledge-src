# Learning-set skills

Each folder here is a **schematic skill**: it fixes the *shape* of one artifact type —
file set, filenames, JSON schema, field meanings, and how the derived renderings
(`.md`, `.html`, `.svg`, …) are built from the JSON. Content quality (faithfulness to
the sources, good pedagogy) is assumed; these specs only pin down structure so a run
is reproducible and machine-checkable.

| Skill | Output dir | Naming scope |
|-------|-----------|--------------|
| [datatables](datatables/SKILL.md) | `output/datatables/` | dataset |
| [flashcards](flashcards/SKILL.md) | `output/flashcards/` | topic |
| [infographics](infographics/SKILL.md) | `output/infographics/` | topic |
| [mindmaps](mindmaps/SKILL.md) | `output/mindmaps/` | topic |
| [podcast-script](podcast-script/SKILL.md) | `output/podcasts/` | dataset |
| [qandas](qandas/SKILL.md) | `output/qandas/` | dataset |
| [quizzes](quizzes/SKILL.md) | `output/quizzes/` | topic |
| [reports](reports/SKILL.md) | `output/reports/` | topic |
| [slides](slides/SKILL.md) | `output/slides/` | topic |

## Shared conventions

**Datasets and sources.** Every supported text source file under `input/` is one *dataset*. Folders are organizational only. Supported source formats are defined by the project `Readme.md` (including `.txt`, `.md`, and `.tex`).
Refer to a source by its **bare filename** — `心经_en.txt`, not a path. Fields named
`source_id` / `source_ids` / `sources` always hold these bare filenames.

**Naming scope.**
- *dataset* skills emit one artifact per dataset: `<dataset>.<ext>`
  (`classical_chinese.json`). Podcasts emit exactly one self-contained episode per
  study set/source-file dataset, with an estimated runtime of at most 45 minutes
  at 150 dialogue words per minute plus explicit pauses. Honor the caller's
  destination and naming convention, including a sole `_episode0.json` file when required.
- *topic* skills emit one artifact per topic (one per source-file dataset unless the project README explicitly requests synthesis): `<prefix>_<topic>.<ext>`
  (`quiz_heart_sutra.json`, `mindmap_comparative_synthesis.mmd`). `<topic>` is a
  lowercase snake_case slug.

**Allowed file types — nothing else.** `.csv` `.html` `.json` `.md` `.mmd` `.svg` `.txt`.
No `.mp3`, `.pdf`, `.pptx`, `.xlsx`, `.docx`, images other than `.svg`, etc.

**JSON house style.** UTF-8 **without BOM**, 2-space indent, `\n` (LF) line endings,
trailing newline. Do **not** `\u`-escape non-ASCII — write `心经` literally. Object keys
in the order the schema lists them.

**HTML/SVG.** Fully self-contained: inline `<style>`, no external scripts, no external
fonts or images. The only permitted external reference is a sibling `assets/*.svg`
(infographics only). HTML carries a `prefers-color-scheme: dark` block.
