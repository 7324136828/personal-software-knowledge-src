# Full Learning Set Generation

Generate a complete learning set from every supported text source file under `input/`, following the artifact specifications under `skills/` and the timestamp-based naming convention defined by `skills/file_naming_convention.md`.

**Mandatory Q&A requirement: convert every exercise and every subpart at the end of every section, subsection, chapter, and complete document into source-exercise Q&A. There is no exercise-count cap. Verify completeness against an inventory made from the full source, including when resuming an existing learning set.**

The checkpoint-aware procedure in §§28–43 governs new runs, resumes, repairs, and skip decisions. Its timestamp rules qualify the simpler first-run sequence in §§5 and 23. The exercise requirements in §§13 and 31A apply to every Q&A generation, validation, reuse, and completion decision.

# 1. CORE DATASET RULE

## One source file = one dataset

Treat **each individual supported text file under `input/` as an independent dataset**.

Folders under `input/` are organizational only. They do **not** define datasets.

Example:

```text
input/
├── 心经_en.txt
├── 道德经_en.tex
└── 庄子_en.***
```

This represents **three datasets**:

```text
心经_en.txt
道德经_en.txt
庄子_en.txt
```

It does **not** represent two datasets.

Process every supported text file independently.

---

# 1A. SUPPORTED SOURCE FORMATS AND PASTED-TEXT BEHAVIOR

Treat source files as **text pasted directly into the model**, not as files that must be compiled or executed.

Supported source extensions are:

```text
.txt
.md
.markdown
.tex
.rst
```

The extension does not change dataset semantics: **one source file = one dataset = one learning set**.

For `.tex` files, read the UTF-8 source directly. Do **not** require XeLaTeX/LuaLaTeX compilation before study-set generation. Ignore document-wrapper commands when they are not pedagogical content (for example `\documentclass`, package imports, layout commands, `\begin{document}`, `\end{document}`, and page-break commands), while preserving meaningful headings, prose, equations, tables, examples, and exercises.

If an unsupported or binary file appears under `input/`, do not guess its contents. Skip it and report it as unsupported unless a dedicated extraction skill is explicitly provided.

## Pasted-text equivalence

For content generation, behave as though the complete textual contents of the current source file had been pasted into the prompt. In particular:

* do not depend on filesystem paths for semantic meaning;

* do not require the source to be arranged in a folder-per-dataset structure;

* do not require document compilation or rendering before understanding it;

* preserve the source's mathematical notation and section structure;

* process the whole source independently before moving to the next source file.

---

# 2. SOURCE IDs

The source ID for a dataset is always the source file's **bare filename**.

Correct:

```text
心经_en.txt
mechanics.txt
```

Incorrect:

```text
input/心经_en.txt
心经_en.txt
./心经_en.txt
```

Never use a path as a source ID.

If an artifact contains a `source`, `sources`, `source_id`, or equivalent field, use only the bare filename.

Example:

```json
{
  "sources": [
    "心经_en.txt"
  ]
}
```

Because each source file is its own dataset, artifacts generated for that dataset will normally cite that one source ID.

---

# 3. DATASET IDENTITY

For each source file, derive a logical dataset name from the filename.

Example:

```text
Source ID:
心经_en.txt
Dataset name:
心经_en
```

Another example:

```text
Source ID:
classical_mechanics.txt
Dataset name:
classical_mechanics
```

The bare source filename remains the authoritative **source ID**.

The filename without its final extension may be used as the dataset/topic identifier when a skill requires a readable identifier.

Do not modify or transliterate Unicode filenames unless the relevant skill explicitly requires it.

---

# 4. SKILLS

Artifact-generation instructions are stored under:

```text
skills/
```

Before generating an artifact type, read and follow its corresponding `SKILL.md`.

Required skills include:

```text
skills/datatables/SKILL.md
skills/flashcards/SKILL.md
skills/infographics/SKILL.md
skills/mindmaps/SKILL.md
skills/podcast-script/SKILL.md
skills/qandas/SKILL.md
skills/quizzes/SKILL.md
skills/reports/SKILL.md
skills/slides/SKILL.md
```

Also read:

```text
skills/file_naming_convention.md
```

The artifact-specific skills define:

* schemas

* required fields

* required output files

* derived renderings

* validation rules

* checklists

* content requirements

The file-naming skill defines timestamp generation and naming conventions.

Follow all applicable specifications exactly.

---

# 5. ONE TIMESTAMP PER SOURCE FILE

At the beginning of processing **each supported text source file**, execute:

```bash
python generate_system_time.py
```

The script returns:

```text
YYYYMMDDHHMMSS
```

Example:

```text
20260907121900
```

Store this value as:

```text
TIMESTAMP
```

## Critical rule

Run the timestamp script exactly **once for each source-file dataset**.

Reuse that exact `TIMESTAMP` for every artifact generated from that source file.

For example:

```text
input/心经_en.txt
```

may receive:

```text
TIMESTAMP=20260907121900
```

Every artifact generated from `心经_en.txt` must therefore use:

```text
20260907121900
```

When processing:

```text
input/道德经_en.txt
```

run the timestamp script again.

It may receive:

```text
TIMESTAMP=20260907121904
```

All artifacts for `道德经_en.txt` then use that second timestamp.

Therefore:

```text
one source file
    =
one dataset
    =
one timestamp
    =
one complete learning set
```

Do not generate a separate timestamp for each artifact type.

Do not generate another timestamp merely to avoid a filename collision.

Do not invent or manually approximate timestamps when the script is available.

---

# 5A. LATEX / MATHEMATICAL CONTENT

Study sets may and should contain LaTeX whenever the source contains mathematics or when LaTeX is the clearest faithful representation.

## Preservation rules

* Preserve meaningful source LaTeX instead of converting equations into lossy plain text.

* Normalize obviously corrupted spacing such as `\mathrm{V a r}` to `\operatorname{Var}` only when the intended mathematics is unambiguous.

* Keep inline mathematics in `\( ... \)` and display mathematics in `\[ ... \]` in Markdown-oriented fields.

* In JSON, encode backslashes correctly according to JSON syntax. For example, LaTeX `\sigma_t^2` must appear as a valid JSON string with escaped backslashes.

* Do not replace standard mathematical symbols with ASCII approximations when valid LaTeX is available.

* Do not invent missing terms merely to repair a damaged formula. If the source is ambiguous, preserve the closest faithful form and note the ambiguity in explanatory prose when needed.

## Artifact behavior

LaTeX is permitted in flashcards, Q&A, quizzes, reports, slides, mind maps, datatables, and infographic text wherever it remains readable. In podcast dialogue, explain equations naturally in speech rather than reading raw commands unless the commands themselves are pedagogically relevant.

For HTML artifacts, render mathematical source safely as text unless the artifact skill explicitly provides a local math renderer. Do not introduce external JavaScript/CDN dependencies merely to render LaTeX.

---

# 6. REQUIRED OUTPUTS FOR EVERY SOURCE FILE

For **every supported text source file**, generate all of the following:

```text
datatables
flashcards
infographics
mindmaps
podcast script
qandas
quizzes
reports
slides
```

Therefore, if there are 100 supported text source files under `input/`, there must be 100 independently generated learning sets.

---

# 7. OUTPUT LOCATIONS

Write artifacts only beneath:

```text
output/
```

Use:

```text
datatables       -> output/datatables/
flashcards       -> output/flashcards/
infographics     -> output/infographics/
mindmaps         -> output/mindmaps/
podcast script   -> output/podcasts/
qandas           -> output/qandas/
quizzes          -> output/quizzes/
reports          -> output/reports/
slides           -> output/slides/
```

Create missing directories as necessary.

---

# 8. DATA TABLES

Generate **one datatable artifact for every source file**.

This is no longer folder-scoped.

Example:

```text
心经_en.txt
```

produces its own datatable.

```text
道德经_en.txt
```

produces a separate datatable.

Follow:

```text
skills/datatables/SKILL.md
```

Base naming:

```text
output/datatables/{TIMESTAMP}.csv
output/datatables/{TIMESTAMP}.json
```

Example for `心经_en.txt`:

```text
output/datatables/20260907121900.csv
output/datatables/20260907121900.json
```

Example for `道德经_en.txt`:

```text
output/datatables/20260907121904.csv
output/datatables/20260907121904.json
```

The table should extract, organize, summarize, categorize, or structure information that is genuinely relevant to the individual source document.

Do not combine unrelated source files into one datatable.

Every row or record containing source-grounded information must identify:

```text
心经_en.txt
```

rather than its filesystem path.

---

# 9. FLASHCARDS

Generate one complete flashcard set for every source file.

Follow:

```text
skills/flashcards/SKILL.md
```

Base naming:

```text
output/flashcards/flashcards_{TIMESTAMP}.json
output/flashcards/flashcards_{TIMESTAMP}.txt
```

Example:

```text
output/flashcards/flashcards_20260907121900.json
output/flashcards/flashcards_20260907121900.txt
```

Unless otherwise specified by the flashcard skill, generate:

```text
20 cards
```

Every card must be grounded in the current dataset's source file and carry that bare filename as its source ID.

---

# 10. INFOGRAPHICS

Generate one infographic artifact set for every source file.

Follow:

```text
skills/infographics/SKILL.md
```

Base forms:

```text
output/infographics/infographic_{TIMESTAMP}.html
output/infographics/infographic_{TIMESTAMP}.json
output/infographics/infographic_{TIMESTAMP}.md
output/infographics/infographic_{TIMESTAMP}.svg
output/infographics/infographic_{TIMESTAMP}.wireframe.txt
```

Supporting SVG assets may be placed beneath:

```text
output/infographics/assets/
```

when permitted by the skill.

The infographic must represent the content of the current source-file dataset only.

---

# 11. MIND MAPS

Generate one mind-map artifact set for every source file.

Follow:

```text
skills/mindmaps/SKILL.md
```

Base naming:

```text
output/mindmaps/mindmap_{TIMESTAMP}.mmd
output/mindmaps/mindmap_{TIMESTAMP}.md
output/mindmaps/mindmap_{TIMESTAMP}.json
```

Nodes, relationships, labels, and explanatory content must be grounded in the current source file.

---

# 12. PODCAST SCRIPT

Generate exactly one self-contained podcast episode for **every source file**.

Each source-file dataset is one study set with one podcast, regardless of its
folder. Produce a text script only, with one introduction and one sign-off.

Follow:

```text
skills/podcast-script/SKILL.md
```

## Maximum duration

Each study set's single episode must have an estimated runtime **at most 20
minutes**, at 150 dialogue words per minute plus explicit pauses:

```text
estimated_minutes = dialogue_words / 150 + total_pause_milliseconds / 60000
```

Sum all `[pause=NNN]` durations in scene directions, including empty-dialogue
silence scenes. With no pauses, the limit is **3,000 dialogue words**. Reduce the
word allowance by `150 * total_pause_milliseconds / 60000` when pauses are used.

Aim for 15–18 minutes when useful material supports it. There is **no minimum
runtime or word count**; short sources may produce shorter episodes. Summarize
long material and prioritize the key concepts, useful examples, and a closing
recap to fit the limit.

Apply the limit independently to each source file's podcast.

## Dialogue count

Count dialogue only.

Do not count:

* JSON keys

* metadata

* source identifiers

* speaker labels

* stage directions

* structural notes

* citations

toward the dialogue word count. Explicit pause directions contribute to runtime
as specified above.

## Single episode file

Create exactly one podcast JSON file per study set. Do not split long sources
into a series. Honor the caller's output destination and filename; the timestamp
convention retains `episode0` for compatibility with the sole episode.

Example:

```text
output/podcasts/20260907121900_episode0.json
```

Report:

```text
single episode file
dialogue words
total pause milliseconds
estimated duration at 150 wpm plus explicit pauses (must be <= 20 minutes)
```

All factual statements in the podcast must remain faithful to the current source file.

---

# 13. Q&A DATASET

Generate one Q&A dataset for every source file, following `skills/qandas/SKILL.md`.

Base naming:

```text
output/qandas/{TIMESTAMP}.json
```

Questions must faithfully represent the current source. Answers must use the source and reasoning or calculations directly implied by its exercises, subject to the missing-information policy below.

## Mandatory exhaustive exercise conversion

Convert **every exercise, review question, practice problem, self-check, and problem-set item at the end/back of every section, subsection, chapter, and complete source document** into Q&A. Also include other identifiable source exercises as required by the Q&A skill.

This requirement takes priority over any default Q&A count, question cap, sampling instruction, or preference to order generated questions by difficulty. Do not select only representative, easy, odd-numbered, answered, or computationally convenient problems. Do not replace several exercises with a summary or let generated conceptual questions substitute for source exercises.

### A. Inventory the complete source before conversion

1. Read the source from beginning to end in document order. If it is too long for one context window, read consecutive, overlapping chunks, carry forward chapter/section context and unfinished exercise blocks, and persist progress until the entire source has been inspected. A keyword search or truncated preview is not a complete inspection.
2. Record every chapter, section, and subsection inspected, including those with zero identifiable exercises. Identify exercise blocks under headings such as `Exercises`, `Problems`, `Review Questions`, `Practice`, `Self-Check`, `Chapter Review`, `Chapter Exercises`, and `Further Questions`, including equivalent wording used by the source.
3. Inspect unheaded numbered/lettered lists after instructional material, starred or unnumbered problems, and LaTeX exercise/problem environments, custom exercise macros, and nested lists. Do not stop at the first exercise block or first chapter. Distinguish exercises from worked examples, table-of-contents entries, and answer-key restatements. A solution-key restatement is evidence for its original exercise, not a second exercise.
4. Create a source-derived inventory in the checkpoint's `exercise_coverage` metadata (§31A). For each exercise, retain a stable location-aware key, document order, chapter/section/subsection context, block identifier, exact original label (or `null`), and a source locator such as line range or character offsets. Record every explicit subpart, including nested subparts and unlettered tasks within an exercise.
5. Define required answer units: an exercise without subparts is one unit; a multipart exercise contributes each lowest-level requested task plus any separately requested parent-level task. A shared stem containing only givens is context, not an extra answer unit. Use explicit local ordinals for unlabeled tasks without presenting them as source numbering.
6. Derive the inventory from the source, independently of the generated Q&A. Do not reconstruct the expected inventory from the output or infer missing exercise numbers from numbering gaps. Repeated labels or identical prompts at different source locations remain distinct exercises.

### B. Preserve each exercise and its context

* Preserve the original exercise label and subpart labels exactly as printed, including starred markers and nested labels. If parent and subpart labels are printed separately, combine them unambiguously for `exercise_label` while retaining their exact components in the inventory. Use `null` when no source label exists; never invent source numbering.
* For an exercise without independently answerable subparts, reproduce the complete exercise prompt faithfully in one question.
* Split independently answerable subparts into separate records. Each question must include the shared stem, all givens and constraints needed for that subpart, its exact task, and any dependency on earlier parts. Preserve the parent label and context so each record is understandable on its own. Across the records, preserve every original task; do not repeat all sibling questions in each record.
* Keep genuinely inseparable subparts together only when splitting would lose their meaning. Include and answer every subpart in that record, and map each required answer unit to it in the inventory. Do not add a duplicate whole-parent question when split records already cover the parent completely.
* Preserve supplied tables, data, units, mathematical notation, and textual figure descriptions. If a question depends on an unavailable figure, dataset, appendix, or external reference, retain that dependency and identify what is unavailable. Do not guess its contents.
* Keep source exercises in document order, followed by any optional generated conceptual Q&A. Use unique, stable, kebab-case record IDs that distinguish repeated exercise labels in different chapters or blocks.

### C. Provide a substantive answer for every required task

* When an answer follows from the source and the reasoning or mathematics implied by the exercise, solve it. An answer key is not required. Do not use the absence of a printed solution, the exercise's difficulty, or its length as a reason to leave it unanswered.
* For numerical problems, show the essential formula, substitutions, calculations, result, and units or interpretation where relevant. For derivations and proofs, give the essential logical steps and conclusion. For multipart records, explicitly address every included subpart.
* For conceptual or open-ended exercises, provide a source-grounded explanation or a clearly identified valid example response and its reasoning.
* Use supplied solution material when present, while checking that it matches the exercise and givens. Identify source inconsistencies instead of silently treating conflicting answers as reliable.
* When information is genuinely missing or ambiguous, still include the exercise. State the exact limitation in `answer`, identify the missing inputs, solve any determinate portion, and describe the remaining method. Never fabricate data, coefficients, simulation output, citations, figure content, or numerical results.
* A method-only or limitation answer is allowed only for a genuine source-information or execution limitation, not as a substitute for solving an otherwise answerable exercise. Report these records separately from fully solved records.
* Preserve meaningful LaTeX and encode its backslashes correctly in JSON.

### D. Use the existing Q&A schema

Use the exact schema in `skills/qandas/SKILL.md`. For the current schema, every converted source exercise must contain:

```text
id: unique kebab-case identifier
kind: "source_exercise"
exercise_label: original exercise/subpart label, or null if absent
question: complete, context-preserving exercise or subpart prompt
answer: worked answer or explicit missing-information limitation
placeholder: short learner hint ending with the literal character …
required: true
source_ids: [SOURCE_ID]
```

`SOURCE_ID` is the current source file's bare filename. Use `kind: "source_exercise"`, not an unsupported `tags` field. Generated questions use `kind: "generated"` and `exercise_label: null`.

Store chapter, section, subsection, parent labels, source locators, coverage mappings, and answer status in the checkpoint inventory unless the artifact schema explicitly supports them. Include helpful location context in the question text. Do not add unsupported fields to the Q&A JSON merely to carry audit metadata.

### E. Validate exhaustive coverage before marking Q&A complete

Perform both a source-reading audit and a source-inventory-to-output reconciliation:

1. Recheck every inventoried chapter/section and exercise block against the source, including boundaries and continuations across chunks. Confirm that no block, exercise, subpart, or embedded requested task was missed.
2. Map every required answer unit to exactly one existing `source_exercise` Q&A record ID. One combined record may cover multiple units only under the inseparable-subpart rule. Verify the full prompt and the corresponding answer for each mapped unit.
3. Check the reverse mapping: every `source_exercise` record must map to a real inventoried exercise/unit. Detect accidental duplicates, label collisions, and records mislabeled as generated. Total counts alone do not prove coverage.
4. Verify shared givens, constraints, tables, dependencies, LaTeX, source IDs, record order, unique IDs, schema fields, and non-empty answers. Audit any limitation answer to ensure it names an actual unavailable input or execution requirement.
5. Require zero missing exercises, zero missing required answer units, zero accidental duplicate conversions, and no unmatched source-exercise records. Generated questions do not count toward exercise coverage.
6. Persist the completed inventory, mappings, counts, current source hash, final Q&A file hash, policy version, and validation result in `exercise_coverage` (§31A). A zero-exercise result is valid only after complete source inspection and an explicit zero count.

A limitation answer can satisfy conversion coverage if it follows the missing-information policy; it does not mean the exercise has been fully solved. Report conversion coverage and answer limitations separately. Never claim that all exercises are solved when any remain limited by missing information or execution requirements.

### F. Handle long exercise sets without truncation

Generate in chapter/section or exercise batches when needed. Persist the inventory, completed record IDs, partial Q&A, and checkpoint after each batch, keeping partial files valid JSON. Reuse the dataset timestamp and preserve already validated work when resuming. Merge the batches into the one required Q&A dataset in source order and validate the complete merged file.

Never truncate, silently omit exercises, or mark Q&A complete because a response, context window, or batch has reached its limit. Keep the artifact `in_progress` (or `failed` if an actual error prevents progress) until exhaustive validation passes. Add optional generated Q&A only after all source exercises have been converted.

---

# 14. QUIZZES

Generate one quiz artifact for every source file.

Follow:

```text
skills/quizzes/SKILL.md
```

Base naming:

```text
output/quizzes/quiz_{TIMESTAMP}.json
```

If the skill requires:

```text
output/quizzes/quiz_best_choices.json
```

follow the skill's rules for generating and managing that derived/stable file.

Every:

* question

* correct answer

* distractor

* rationale

* explanation

must remain faithful to the current source file.

Do not introduce external facts merely to make distractors more sophisticated.

---

# 15. REPORTS

Generate one complete report artifact set for every source file.

Follow:

```text
skills/reports/SKILL.md
```

Base naming:

```text
output/reports/report_{TIMESTAMP}.md
output/reports/report_{TIMESTAMP}.json
output/reports/report_{TIMESTAMP}.html
```

The report should explain and organize the current source document in a useful educational form.

Do not combine other source documents into the report unless explicitly requested.

---

# 16. SLIDES

Generate one slide artifact set for every source file.

Follow:

```text
skills/slides/SKILL.md
```

Base naming:

```text
output/slides/slides_{TIMESTAMP}.md
output/slides/slides_{TIMESTAMP}.json
```

Every factual slide must carry the current source ID.

Do not generate `.pptx`.

---

# 17. DEFAULT CROSS-SOURCE POLICY

Because **each source file is an independent dataset**, do not automatically combine multiple source files into synthesis artifacts.

By default:

```text
心经_en.txt
```

is processed independently from:

```text
道德经_en.txt
```

even if both appear in:

```text
input/
```

Their shared folder does not imply that their contents should be merged.

Cross-source synthesis should be generated only when:

1. explicitly requested; or

2. a specific skill explicitly requires cross-source aggregation.

Otherwise, maintain strict one-file-per-dataset isolation.

---

# 18. SOURCE FIDELITY

The current source file is the authoritative source for its learning set.

For exercise answers, source-grounded logical deductions and standard mathematical calculations directly implied by the source and exercise are permitted, including calculations performed with available tools. These are derived answers, not additional source facts. Do not execute a source document as code. Do not invent missing empirical inputs or silently import another document or external dataset. Apply the explicit limitation policy in §13 when necessary.

Do not introduce unsupported information from:

* model memory

* general world knowledge

* websites

* Wikipedia

* search engines

* external databases

* other files

* neighboring files in the same input folder

unless explicitly authorized.

For a dataset created from:

```text
心经_en.txt
```

derive its learning materials from:

```text
心经_en.txt
```

only.

Do not silently supplement it with information from:

```text
道德经_en.txt
庄子_en.txt
```

or any other document.

---

# 19. SOURCE ATTRIBUTION

Every applicable content unit must carry the current source ID according to its schema.

This applies to:

```text
claims
cards
questions
answers
rows
definitions
quiz items
podcast claims
mind-map nodes
mind-map relationships
report statements
infographic statements
slides
```

Example:

```json
{
  "question": "What concept does the text introduce?",
  "sources": [
    "心经_en.txt"
  ]
}
```

Never use:

```json
{
  "sources": [
    "input/心经_en.txt"
  ]
}
```

---

# 20. JSON REQUIREMENTS

Every `.json` file must use:

```text
Encoding:       UTF-8
BOM:            none
Indentation:    2 spaces
Line endings:   LF
Final newline:  required
Unicode:        literal
```

Correct:

```json
{
  "source": "心经_en.txt",
  "title": "心经"
}
```

Avoid unnecessary Unicode escaping such as:

```json
{
  "source": "\u5fc3\u7ecf_en.txt"
}
```

Python serialization should behave like:

```python
json.dump(
    data,
    file,
    ensure_ascii=False,
    indent=2
)
file.write("\n")
```

Validate that every JSON file parses successfully.

---

# 21. HTML AND SVG

HTML and SVG must be self-contained.

Do not reference:

```text
external JavaScript
external CSS
CDNs
external fonts
remote images
remote SVG resources
external APIs
tracking scripts
```

Infographics may reference their permitted sibling:

```text
assets/*.svg
```

files when allowed by the infographic skill.

---

# 22. ALLOWED OUTPUT FILE TYPES

Generate only:

```text
.csv
.html
.json
.md
.mmd
.svg
.txt
```

Do not generate:

```text
.mp3
.wav
.pdf
.pptx
.xlsx
.xls
.docx
.png
.jpg
.jpeg
.webp
.gif
```

No audio binaries.

No raster images.

No PowerPoint.

No Excel.

No Word documents.

---

# 23. PROCESSING ALGORITHM

Recursively discover all supported text source files beneath:

```text
input/
```

Discovery must match the supported extensions listed in §1A recursively. Do not hard-code `.txt` as the only input type.

For each source file, perform the following sequence independently.

## Step 1 — Select one source file

Example:

```text
input/心经_en.txt
```

Internally retain its path only for reading the file.

Its public/source identifier is:

```text
心经_en.txt
```

---

## Step 2 — Establish dataset identity

Set:

```text
SOURCE_PATH = input/心经_en.txt
SOURCE_ID   = 心经_en.txt
DATASET     = 心经_en
```

Never place `SOURCE_PATH` into generated source-attribution fields.

---

## Step 3 — Read skills

Read:

```text
skills/file_naming_convention.md
```

and every relevant:

```text
skills/<artifact>/SKILL.md
```

Do not rely on remembered schemas.

---

## Step 4 — Generate timestamp

Execute:

```bash
python generate_system_time.py
```

exactly once for this source file.

Validate:

```regex
^\d{14}$
```

Store the result as:

```text
TIMESTAMP
```

---

## Step 5 — Read source

Read the complete source document.

Analyze its:

```text
concepts
definitions
claims
relationships
themes
examples
terminology
structure
important passages
```

Do not use neighboring source files to fill gaps.

---

## Step 6 — Generate datatable

Create a useful structured data representation of this particular source file.

Generate:

```text
output/datatables/{TIMESTAMP}.csv
output/datatables/{TIMESTAMP}.json
```

---

## Step 7 — Generate flashcards

Generate the complete flashcard set for this source.

---

## Step 8 — Generate infographic

Generate the complete infographic file set for this source.

---

## Step 9 — Generate mind map

Generate the complete mind-map file set for this source.

---

## Step 10 — Generate podcast

Generate exactly one self-contained podcast episode for this source-file study
set. Its estimated runtime must be at most 20 minutes at 150 dialogue words per
minute plus explicit pauses. Summarize long material to fit; shorter episodes are
allowed without a minimum word count. Follow §12.

---

## Step 11 — Generate Q&A dataset

Generate a complete Q&A dataset from this source.

Before adding generated Q&A, complete §13: inventory every chapter/section, convert every exercise and required answer unit, validate the source-to-output mapping, and persist the coverage audit under §31A. Existing Q&A must pass this same audit before reuse.

---

## Step 12 — Generate quiz

Generate the required quiz files from this source.

---

## Step 13 — Generate report

Generate the required report files from this source.

---

## Step 14 — Generate slides

Generate the required slide files from this source.

---

## Step 15 — Validate

Run:

* artifact-specific validation;

* schema validation;

* source-attribution validation;

* file-format validation;

* timestamp validation;

* podcast single-episode and runtime validation, including explicit pauses;

* global validation;
* exhaustive Q&A exercise/unit coverage validation under §§13 and 31A.

Only after validation should the source-file dataset be marked complete.

---

## Step 16 — Move to next file

Select the next source file.

Generate a **new timestamp** for that file.

Repeat the entire process.

---

# 24. EXAMPLE EXECUTION

Given:

```text
input/
├── 心经_en.txt
└── 道德经_en.txt
```

Suppose:

```text
心经_en.txt
TIMESTAMP=20260907121900
```

Generate:

```text
output/datatables/20260907121900.csv
output/datatables/20260907121900.json
output/flashcards/flashcards_20260907121900.json
output/flashcards/flashcards_20260907121900.txt
output/infographics/infographic_20260907121900.html
output/infographics/infographic_20260907121900.json
output/infographics/infographic_20260907121900.md
output/infographics/infographic_20260907121900.svg
output/infographics/infographic_20260907121900.wireframe.txt
output/mindmaps/mindmap_20260907121900.mmd
output/mindmaps/mindmap_20260907121900.md
output/mindmaps/mindmap_20260907121900.json
output/podcasts/20260907121900_episode0.json
output/qandas/20260907121900.json
output/quizzes/quiz_20260907121900.json
output/reports/report_20260907121900.md
output/reports/report_20260907121900.json
output/reports/report_20260907121900.html
output/slides/slides_20260907121900.md
output/slides/slides_20260907121900.json
```

Then process:

```text
道德经_en.txt
```

Suppose its timestamp is:

```text
20260907121905
```

Generate an entirely separate learning set using:

```text
20260907121905
```

Do not reuse the timestamp from `心经_en.txt`.

---

# 25. GLOBAL VALIDATION CHECKLIST

For every supported text source file verify:

## Dataset identity

* [ ] The file was treated as its own independent dataset.

* [ ] Its containing folder was not treated as the dataset.

* [ ] The bare filename is used as `SOURCE_ID`.

* [ ] No filesystem paths appear in source-attribution fields.

## Timestamp

* [ ] Timestamp script ran exactly once for this file.

* [ ] Timestamp contains exactly 14 digits.

* [ ] All artifacts for the source use the same timestamp.

* [ ] Other source files receive their own timestamps.

## Data table

* [ ] CSV exists.

* [ ] JSON exists.

* [ ] Datatable describes the current source file.

* [ ] Rows are source-grounded.

* [ ] Relevant rows carry the source ID.

## Flashcards

* [ ] Flashcard artifact exists.

* [ ] Required card count is satisfied.

* [ ] Every card is grounded in the source.

* [ ] Every card carries appropriate source attribution.

## Infographic

* [ ] Required files exist.

* [ ] Information comes from the current source.

* [ ] HTML/SVG external-dependency rules pass.

## Mind map

* [ ] `.mmd` exists.

* [ ] `.md` exists.

* [ ] `.json` exists.

* [ ] Nodes and relationships are source-grounded.

## Podcast

* [ ] Exactly one self-contained podcast episode exists for this source-file study set.

* [ ] Dialogue word count excludes metadata.

* [ ] Estimated runtime is at most 20 minutes at 150 dialogue words per minute plus explicit pauses.

* [ ] The sole episode file follows the caller's destination, filename, and dataset timestamp.

* [ ] Dialogue word count, total pause milliseconds, and estimated runtime are recorded.

* [ ] One introduction and one sign-off are present; no additional episode files exist.

## Q&A

* [ ] Q&A dataset exists for this source.

* [ ] Questions are supported by the source.

* [ ] Answers are supported by the source.

* [ ] Source attribution is present.

* [ ] Every end/back-of-section, end/back-of-subsection, and end/back-of-chapter exercise block was inspected.

* [ ] Every exercise in those blocks was converted to one or more Q&A records; no exercise was omitted due to length or count.

* [ ] Exercise labels, full prompts, subparts, and LaTeX were preserved.

* [ ] Converted exercises use `kind: "source_exercise"`, distinct from generated Q&A.
* [ ] The complete source-derived exercise inventory and every required answer unit reconcile with actual Q&A IDs under §13.
* [ ] No missing exercises/units, duplicate conversions, or unmatched source-exercise records remain.
* [ ] Every multipart answer addresses all mapped tasks, and every limitation answer identifies a genuine limitation.
* [ ] Current `exercise_coverage` policy version, source/Q&A hashes, mappings, counts, and `PASS` are persisted (§31A).

## Quiz

* [ ] Quiz exists.

* [ ] Correct answers are supported by the source.

* [ ] Distractors do not introduce unsupported source claims.

* [ ] Explanations are source-grounded.

## Report

* [ ] Markdown exists.

* [ ] JSON exists.

* [ ] HTML exists.

* [ ] Report uses only the current source unless explicitly instructed otherwise.

## Slides

* [ ] Markdown exists.

* [ ] JSON exists.

* [ ] Every factual slide is source-grounded.

## JSON

* [ ] Valid JSON.

* [ ] UTF-8.

* [ ] No BOM.

* [ ] 2-space indentation.

* [ ] LF endings.

* [ ] Final newline.

* [ ] Unicode characters remain literal.

## File formats

* [ ] Only allowed file extensions were generated.

* [ ] No audio files.

* [ ] No PDF.

* [ ] No PPTX.

* [ ] No XLSX.

* [ ] No DOCX.

* [ ] No raster images.

## Skill compliance

* [ ] Relevant `SKILL.md` was read.

* [ ] Schema is exact.

* [ ] Required file set is complete.

* [ ] Derived renderings are complete.

* [ ] Artifact-specific checklist passes.

---

# 26. COMPLETION REPORT

After completing each source-file dataset, report:

```text
Dataset:
Source ID:
Timestamp:
Artifacts generated:
Podcast dialogue words:
Podcast total pause milliseconds:
Podcast estimated duration:
Validation status:
```

Example:

```text
Dataset: 心经_en
Source ID: 心经_en.txt
Timestamp: 20260907121900
Artifacts generated: complete
Podcast dialogue words: 2,550
Podcast total pause milliseconds: 30,000
Podcast estimated duration: 17.5 minutes
Validation status: PASS
```

Also report the exercise-coverage totals and limited-answer counts required by §42. List all generated file paths for that dataset.

At the end of the entire run, also report:

```text
Total source files discovered:
Total datasets completed:
Datasets passed:
Datasets failed:
Total files generated:
```

---

# 27. FINAL OPERATING PRINCIPLE

Use this model throughout the task:

```text
input/**/*.{txt,md,markdown,tex,rst}
       │
       ├── file 1 = dataset 1 = timestamp 1 = complete learning set
       │
       ├── file 2 = dataset 2 = timestamp 2 = complete learning set
       │
       ├── file 3 = dataset 3 = timestamp 3 = complete learning set
       │
       └── ...
```

Folders are organizational containers only.

Do not aggregate files simply because they share a directory.

For each individual source file, produce:

```text
1 datatable
1 flashcard set
1 infographic set
1 mind-map set
1 self-contained podcast episode (estimated runtime <= 20 minutes)
1 Q&A dataset
1 quiz set
1 report set
1 slide set
```

subject to the exact schemas and derived-file requirements in the corresponding skills.

Priorities:

```text
ONE FILE = ONE DATASET
        ↓
SOURCE FIDELITY
        ↓
SKILL SCHEMA COMPLIANCE
        ↓
CORRECT BARE-FILENAME SOURCE ID
        ↓
ONE TIMESTAMP PER SOURCE FILE
        ↓
COMPLETE LEARNING SET
        ↓
VALID FILE FORMATS
        ↓
EDUCATIONAL QUALITY
```

Do not omit artifacts because a source is short.

When the source does not contain enough information for a particular type of learning item, create fewer distinct factual claims and use pedagogical transformations of the information that is actually present rather than inventing new facts.

Never fabricate information to satisfy volume requirements.

The final result must contain one complete, independently timestamped, source-grounded learning set for every supported text source file beneath `input/`.

# 28. CHECKPOINT / RESUME SYSTEM

Maintain a persistent checkpoint file at:

```text
output/checkpoint.json
```

The checkpoint exists so that repeated or interrupted runs do not unnecessarily regenerate learning sets that have already been completed.

The checkpoint is operational metadata only. It does not change the rule:

```text
one source file = one dataset
```

and it does not change source attribution rules.

Generated artifact content must continue using the source file's bare filename as `SOURCE_ID`.

---

## Checkpoint location

Use exactly:

```text
output/checkpoint.json
```

Create it if it does not already exist.

If `output/` does not exist, create it first.

The checkpoint itself is an allowed JSON output file.

---

## Checkpoint purpose

Use `checkpoint.json` to determine whether each discovered source file is:

```text
not_started
in_progress
complete
failed
stale
```

Definitions:

```text
not_started
    No usable checkpoint record exists for the source.
in_progress
    Processing began, but the entire learning set has not yet passed validation.
complete
    Every required artifact was generated and the complete dataset passed validation.
failed
    Processing encountered an error or validation failure.
stale
    The source file has changed since the checkpoint record was created.
```

Only datasets whose status is:

```text
complete
```

and whose source fingerprint still matches may be skipped.

---

# 29. CHECKPOINT IDENTITY

The public source identifier remains the bare filename.

Example:

```text
SOURCE_PATH = input/classics/心经_en.txt
SOURCE_ID   = 心经_en.txt
DATASET     = 心经_en
```

Generated artifacts must use:

```text
心经_en.txt
```

as the source identifier.

However, `checkpoint.json` is internal execution metadata and must uniquely distinguish source files.

Therefore, checkpoint records should use the normalized path relative to the project root as their internal key.

Example:

```text
input/classics/心经_en.txt
```

This prevents collisions if separate input folders contain files with the same bare filename.

Example:

```text
input/classics/text.txt
input/history/text.txt
```

These are two separate datasets even though both public source IDs are:

```text
text.txt
```

Do not expose the checkpoint key as the source ID inside learning artifacts.

---

# 30. SOURCE FINGERPRINT

For every discovered source file, calculate a SHA-256 hash of the exact source bytes before deciding whether it can be skipped.

Store it as:

```text
source_sha256
```

Example:

```json
{
  "source_sha256": "8ed3f6ad685b959ead7022518e1af76cd816f8e8ec7ccdd..."
}
```

The hash determines whether the source has changed since its previous learning set was generated.

Do not rely solely on:

```text
filename
file size
modification timestamp
```

to determine whether a source is unchanged.

The SHA-256 hash is authoritative for checkpoint invalidation.

---

# 31. CHECKPOINT SCHEMA

Use a structure equivalent to:

```json
{
  "version": 1,
  "datasets": {
    "input/classics/心经_en.txt": {
      "source_id": "心经_en.txt",
      "dataset": "心经_en",
      "source_sha256": "SOURCE_SHA256",
      "timestamp": "20260907121900",
      "status": "complete",
      "artifacts": {
        "datatables": "complete",
        "flashcards": "complete",
        "infographics": "complete",
        "mindmaps": "complete",
        "podcasts": "complete",
        "qandas": "complete",
        "quizzes": "complete",
        "reports": "complete",
        "slides": "complete"
      },
      "generated_files": [
        "output/datatables/20260907121900.csv",
        "output/datatables/20260907121900.json",
        "output/flashcards/flashcards_20260907121900.json",
        "output/flashcards/flashcards_20260907121900.txt"
      ],
      "validation": {
        "status": "PASS"
      },
      "error": null
    }
  }
}
```

Additional fields may be stored when operationally useful, but do not remove the required information above.

Valid top-level dataset statuses are:

```text
in_progress
complete
failed
stale
```

Valid artifact statuses are:

```text
pending
in_progress
complete
failed
```

---


# 31A. EXERCISE COVERAGE CHECKPOINT METADATA

Add `exercise_coverage` to each dataset checkpoint entry. This is operational metadata; it does not alter the Q&A artifact schema. Initialize it before Q&A work and update it after each exercise batch.

```json
{
  "exercise_coverage": {
    "policy_version": "all-source-exercises-v1",
    "source_sha256": "SOURCE_SHA256",
    "qandas_sha256": null,
    "discovery_complete": false,
    "reviewed_sections": [],
    "blocks": [],
    "items": [],
    "counts": {
      "exercise_blocks": 0,
      "exercises_found": 0,
      "exercises_covered": 0,
      "required_answer_units_found": 0,
      "required_answer_units_covered": 0,
      "source_exercise_records": 0,
      "generated_records": 0,
      "records_with_answer_limitations": 0,
      "missing_exercises": 0,
      "missing_answer_units": 0,
      "duplicate_conversions": 0,
      "unmatched_source_exercise_records": 0
    },
    "status": "PENDING"
  }
}
```

Populate the arrays from the source, not the generated questions:

* `reviewed_sections`: every inspected chapter/section/subsection, its source locator, and associated block IDs, explicitly including sections with no exercises. For unstructured documents, record the full document span.
* `blocks`: each block's stable ID, source order, heading/location context, source span, and exercise keys.
* `items`: one entry per original exercise with its stable location-aware key, source order, block ID, exact original label or `null`, parent/chapter/section/subsection context, and source locator. Include its `required_answer_units`, each with a stable key, original subpart label or `null`, locator/task description, mapped `qanda_id` (initially `null`), and `answer_status` (`pending`, `answered`, or `limited`). A parent is covered only when all its required units and shared prompt context are covered. Record the reason for any limited answer.

Set `discovery_complete: true` only after the entire source has been inspected and the inventory is complete. Recalculate counts from the inventory and parsed Q&A; never enter estimated or aspirational totals. Record counts and unit counts may differ because exercises can split and inseparable units can share a record.

Set `exercise_coverage.status: "PASS"` only after §13's complete audit passes. Otherwise use `PENDING` during work or `FAIL` after a failed audit. Hash the exact final Q&A file bytes into `qandas_sha256`. Any subsequent change to the source, Q&A, inventory, or mapping invalidates this audit until revalidation.

## Mandatory reuse and skip gate

Apply this gate to **every dataset skip or dataset completion decision and every Q&A reuse, adoption, or completion decision** in §§28–43, including Cases B, D, E, F, and G of §33. Other artifact types may be completed, adopted, or reused independently after their own validations; they do not depend on Q&A already being complete.

* The Q&A artifact must pass its schema and §13 validation.
* The coverage policy must be `all-source-exercises-v1`, discovery must be complete, and coverage status must be `PASS`.
* Coverage `source_sha256` must match the current source bytes and coverage `qandas_sha256` must match the current Q&A file bytes.
* The stored inventory/mappings must reconcile with actual output IDs, with zero missing exercises/units, zero accidental duplicate conversions, and zero unmatched source-exercise records.

An older checkpoint's dataset `complete`, artifact `complete`, or general validation `PASS` does not establish compliance with these exercise requirements. If the coverage metadata is missing, outdated, invalid, or mismatched, do not skip: inspect the source, inventory and revalidate the existing Q&A, and repair missing or invalid records as needed. A file that passes the new audit may be adopted without rewriting it.

For an unchanged source, reuse its existing dataset timestamp and preserve other validated artifacts during this audit/repair. Set the dataset and Q&A state to `in_progress` and general validation to `PENDING` while work remains. Save the coverage audit, run full dataset validation, and only then restore `complete` / `PASS`. For changed source bytes, follow the stale-source regeneration policy and rebuild the inventory for the new source.

---

# 32. CHECKPOINT JSON FORMAT

`output/checkpoint.json` must follow the same JSON formatting requirements as all other JSON files:

```text
Encoding:       UTF-8
BOM:            none
Indentation:    2 spaces
Line endings:   LF
Final newline:  required
Unicode:        literal
```

Use behavior equivalent to:

```python
json.dump(
    checkpoint,
    file,
    ensure_ascii=False,
    indent=2
)
file.write("\n")
```

Validate that `checkpoint.json` parses successfully after every update.

---

# 33. INITIAL CHECKPOINT DECISION

Before generating artifacts for a source file:

1. Calculate its normalized internal checkpoint path.

2. Calculate its SHA-256 hash.

3. Look for its checkpoint entry.

4. Compare the checkpoint status and hash.

5. Decide whether to skip, resume, restart, or process normally.

Use the following logic.

---

## Case A — No checkpoint record

If no checkpoint entry exists:

```text
PROCESS
```

Generate a new timestamp exactly once.

Create an `in_progress` checkpoint record before generating artifacts.

---

## Case B — Complete and unchanged

If:

```text
status == "complete"
```

and:

```text
stored source_sha256 == current source_sha256
```

then verify that the expected generated files still exist and apply the mandatory exercise-coverage reuse gate in §31A. An old `PASS` without a current coverage audit is insufficient.

Only if all required files exist, the §31A exercise-coverage gate passes, and the checkpoint records validation as:

```text
PASS
```

then:

```text
SKIP
```

Do not:

```text
generate a new timestamp
regenerate artifacts
modify completed artifacts
```

Report the dataset as:

```text
SKIPPED — already complete
```

---

## Case C — Source changed, regardless of previous status

For any existing checkpoint record, check for a changed source hash before applying unchanged-source skip, resume, or repair logic. If:

```text
stored source_sha256 != current source_sha256
```

the previous learning set is stale, whether its prior status was `complete`, `in_progress`, `failed`, or `stale`.

Set:

```text
status = "stale"
```

Then regenerate the entire learning set.

Because the source contents have changed, this is considered a new generation run for that dataset.

Run:

```bash
python generate_system_time.py
```

exactly once and store the new timestamp.

Do not reuse the previous timestamp.

Replace the checkpoint record with the new generation state while preserving old metadata only if useful for diagnostics.

---

## Case D — In progress

If:

```text
status == "in_progress"
```

and the source hash is unchanged:

```text
RESUME
```

Reuse the timestamp already stored in the checkpoint.

Critical:

```text
DO NOT run generate_system_time.py again.
```

The original timestamp remains the timestamp for this source-file generation attempt.

Inspect the per-artifact checkpoint states.

Skip artifacts already marked:

```text
complete
```

only after confirming their required files still exist and validate successfully.

Resume with the first incomplete or invalid artifact.

---

## Case E — Failed

If:

```text
status == "failed"
```

and the source hash has not changed:

```text
RESUME / RETRY
```

Reuse the stored timestamp.

Do not generate a new timestamp merely because the previous execution failed.

Change the dataset status to:

```text
in_progress
```

and retry the failed or incomplete artifacts.

Artifacts already completed and validated do not need to be regenerated.

---

## Case F — Checkpoint says complete but files are missing

If the checkpoint reports:

```text
complete
```

but one or more required generated files are missing:

```text
DO NOT SKIP
```

Change the dataset status to:

```text
in_progress
```

Reuse the existing timestamp if the source hash is unchanged.

Regenerate or repair only the missing or invalid artifacts.

Run full validation afterward.

---

## Case G — Artifact exists but checkpoint does not record completion

Do not assume that a file is valid merely because its expected pathname exists.

Validate it according to the corresponding skill.

For Q&A, validity includes the complete §13 audit and §31A coverage metadata; schema validity alone is insufficient.

If valid, it may be adopted into the current checkpoint and marked:

```text
complete
```

If invalid or uncertain, regenerate it.

The checkpoint and validation results, not file existence alone, determine completion.

---

# 34. TIMESTAMP RULE WITH CHECKPOINTING

The original timestamp rule remains:

```text
one source file
    =
one dataset
    =
one generation timestamp
    =
one complete learning set
```

For a brand-new dataset generation:

```bash
python generate_system_time.py
```

must still run exactly once.

However, checkpoint resume is not a new generation attempt.

Therefore:

```text
new source / stale source
    -> generate new timestamp
in_progress resume
    -> reuse checkpoint timestamp
failed retry
    -> reuse checkpoint timestamp
missing artifact repair
    -> reuse checkpoint timestamp
complete unchanged dataset
    -> skip and reuse nothing
```

Never generate a second timestamp simply because execution was interrupted.

Never generate a second timestamp for an individual artifact.

Never generate another timestamp merely to avoid filename collisions.

---

# 35. CHECKPOINT BEFORE PROCESSING

Immediately after selecting a source file that requires processing, create or update its checkpoint record before generating the first artifact.

Example:

```json
{
  "source_id": "心经_en.txt",
  "dataset": "心经_en",
  "source_sha256": "...",
  "timestamp": "20260907121900",
  "status": "in_progress",
  "artifacts": {
    "datatables": "pending",
    "flashcards": "pending",
    "infographics": "pending",
    "mindmaps": "pending",
    "podcasts": "pending",
    "qandas": "pending",
    "quizzes": "pending",
    "reports": "pending",
    "slides": "pending"
  },
  "generated_files": [],
  "validation": {
    "status": "PENDING"
  },
  "error": null
}
```

Write this checkpoint to disk before beginning artifact generation.

This ensures an interrupted process can resume.

---

# 36. UPDATE CHECKPOINT AFTER EACH ARTIFACT

Update `checkpoint.json` after every artifact type is successfully generated and validated.

Example sequence:

```text
datatable complete
    ↓
save checkpoint
flashcards complete
    ↓
save checkpoint
infographic complete
    ↓
save checkpoint
mind map complete
    ↓
save checkpoint
podcast complete
    ↓
save checkpoint
Q&A complete
    ↓
save checkpoint
quiz complete
    ↓
save checkpoint
report complete
    ↓
save checkpoint
slides complete
    ↓
save checkpoint
```

Do not wait until the entire corpus has finished before saving checkpoint progress.

---

# 37. ARTIFACT CHECKPOINT FLOW

For each artifact type:

```text
pending
   ↓
in_progress
   ↓
generate files
   ↓
validate files
   ↓
complete
```

Before starting an artifact, optionally set:

```text
in_progress
```

and persist the checkpoint.

After successful validation, set:

```text
complete
```

and persist the checkpoint again.

If generation or validation fails:

```text
failed
```

Store a short diagnostic message in the dataset's `error` field and persist the checkpoint.

---

# 38. FAILURE HANDLING

If an artifact fails:

1. Preserve all previously completed artifacts.

2. Preserve the source's timestamp.

3. Mark the failing artifact as:

```text
failed
```

4. Mark the dataset as:

```text
failed
```

5. Record a useful diagnostic in:

```text
error
```

6. Write `checkpoint.json`.

7. Continue to the next source file when practical.

Do not mark the dataset:

```text
complete
```

unless every required artifact and every required validation succeeds.

---

# 39. ATOMIC CHECKPOINT WRITES

Avoid corrupting `checkpoint.json` if execution stops during a write.

Prefer an atomic update strategy.

For example:

```text
1. serialize new checkpoint
2. write output/checkpoint.json.tmp
3. flush and close the file
4. validate the temporary JSON
5. atomically replace output/checkpoint.json
```

After successful replacement, no temporary checkpoint file should remain.

Do not intentionally leave:

```text
checkpoint.json.tmp
```

in the final output.

---

# 40. UPDATED PROCESSING ALGORITHM

Recursively discover all supported text source files beneath:

```text
input/
```

Discovery must match the supported extensions listed in §1A recursively. Do not hard-code `.txt` as the only input type.

Process them deterministically, preferably by normalized relative pathname.

For each source file:

---

## Step 1 — Establish identity

Set:

```text
SOURCE_PATH
SOURCE_ID
DATASET
CHECKPOINT_KEY
SOURCE_SHA256
```

where:

```text
SOURCE_ID
```

is the bare filename and:

```text
CHECKPOINT_KEY
```

is the normalized relative source path.

---

## Step 2 — Inspect checkpoint

Load:

```text
output/checkpoint.json
```

if it exists.

Evaluate:

```text
checkpoint record
source hash
dataset status
artifact statuses
expected output files
```

---

## Step 3 — Skip decision

If the dataset is:

```text
complete
```

the source hash is unchanged, all required generated files exist, and validation status is:

```text
PASS
```

then also apply the mandatory §31A exercise-coverage gate. Skip only if that gate passes. Otherwise audit and repair Q&A under the existing timestamp for an unchanged source.

For a valid skip, report:

```text
SKIPPED — already complete
```

Move to the next source file.

---

## Step 4 — Timestamp decision

If this is a new or stale generation attempt, execute:

```bash
python generate_system_time.py
```

exactly once.

If resuming an unchanged:

```text
in_progress
failed
```

dataset, reuse the timestamp from `checkpoint.json`.

---

## Step 5 — Persist in-progress state

Before artifact generation, save:

```text
status = in_progress
```

to `checkpoint.json`.

---

## Step 6 — Read source and skills

Read the complete current source.

Read:

```text
skills/file_naming_convention.md
```

and all relevant:

```text
skills/<artifact>/SKILL.md
```

Do not rely on remembered schemas.

---

## Step 7 — Datatable

If checkpoint status for:

```text
datatables
```

is complete and its files validate, skip this artifact.

Otherwise generate and validate:

```text
output/datatables/{TIMESTAMP}.csv
output/datatables/{TIMESTAMP}.json
```

Then mark:

```text
datatables = complete
```

and save the checkpoint.

---

## Step 8 — Flashcards

Use the same resume logic.

After success:

```text
flashcards = complete
```

Save the checkpoint.

---

## Step 9 — Infographic

Use the same resume logic.

After success:

```text
infographics = complete
```

Save the checkpoint.

---

## Step 10 — Mind map

Use the same resume logic.

After success:

```text
mindmaps = complete
```

Save the checkpoint.

---

## Step 11 — Podcast

Use the same resume logic.

The single podcast episode for this source-file study set must satisfy §12,
including the estimated runtime limit of at most 20 minutes with explicit pauses.
Repair an existing overlong script or multi-episode output before marking it complete.

After validating the sole episode file, dialogue count, pauses, and runtime:

```text
podcasts = complete
```

Save the checkpoint.

---

## Step 12 — Q&A

Read and inventory the entire source under §13, then convert every exercise and required answer unit before adding generated questions. Reuse existing Q&A only when the §31A gate passes; otherwise audit and repair it with the existing timestamp for an unchanged source. Persist batch progress and complete the source-to-output reconciliation.

After the schema validation, complete exercise audit, and persisted `exercise_coverage.status: "PASS"` succeed:

```text
qandas = complete
```

Save the checkpoint.

---

## Step 13 — Quiz

Use the same resume logic.

After success:

```text
quizzes = complete
```

Save the checkpoint.

---

## Step 14 — Report

Use the same resume logic.

After success:

```text
reports = complete
```

Save the checkpoint.

---

## Step 15 — Slides

Use the same resume logic.

After success:

```text
slides = complete
```

Save the checkpoint.

---

## Step 16 — Full dataset validation

After all artifact statuses are complete, run the complete validation suite again.

Verify:

```text
all required files exist
all schemas pass
all source IDs are correct
all timestamps agree
all JSON parses
all format restrictions pass
podcast has exactly one episode and estimated runtime <= 20 minutes including explicit pauses
complete source exercise inventory reconciles with Q&A records
every exercise and required answer unit is covered
exercise_coverage policy, hashes, and PASS satisfy section 31A
answer limitations are counted separately from solved records
all skill-specific requirements pass
```

---

## Step 17 — Mark complete

Only if the full dataset validation passes:

```text
status = complete
validation.status = PASS
error = null
```

Save `checkpoint.json`.

Only at this point may future runs automatically skip this dataset.

---

## Step 18 — Move to next source

Continue to the next source file.

Do not generate timestamps for files that are skipped because they are already complete.

---

# 41. CHECKPOINT VALIDATION CHECKLIST

For every discovered source file verify:

## Checkpoint identity

* [ ] A unique checkpoint key identifies the source path.

* [ ] `source_id` contains only the bare filename.

* [ ] Checkpoint path identity is never substituted for artifact source attribution.

* [ ] SHA-256 fingerprint is recorded.

## Resume behavior

* [ ] Complete unchanged datasets are skipped only when required files and validation pass, including the current §31A exercise-coverage gate.

* [ ] Complete datasets are not skipped when required artifacts are missing.

* [ ] Changed sources are treated as stale and regenerated.

* [ ] In-progress datasets reuse their existing timestamp.

* [ ] Failed datasets reuse their existing timestamp when retried.

* [ ] Already completed and validated artifact types are not needlessly regenerated.
* [ ] Existing Q&A is reused only when the current §31A exercise-coverage gate passes.
* [ ] Historical completion without a current coverage audit triggers Q&A inspection and repair under the same timestamp for an unchanged source.

## Completion behavior

* [ ] Dataset is marked complete only after full validation.

* [ ] Every required artifact is complete.

* [ ] Every required file exists.

* [ ] Validation status is `PASS`.

* [ ] Checkpoint is persisted after completion.
* [ ] Every exercise and required answer unit reconciles with actual Q&A IDs; no omissions or accidental duplicates remain.
* [ ] Exercise coverage is `PASS`, with the current policy version and matching source/Q&A hashes.
* [ ] Limited answers are reported separately; coverage completion is not misreported as all exercises solved.

## Checkpoint JSON

* [ ] Valid JSON.

* [ ] UTF-8.

* [ ] No BOM.

* [ ] 2-space indentation.

* [ ] LF endings.

* [ ] Final newline.

* [ ] Literal Unicode.

* [ ] Atomic write procedure is used where practical.

---

# 42. UPDATED COMPLETION REPORT

After each source-file dataset, report one of:

```text
COMPLETED
RESUMED AND COMPLETED
FAILED
SKIPPED — already complete
REGENERATED — source changed
```

For completed, resumed, or regenerated datasets report:

```text
Dataset:
Source ID:
Timestamp:
Checkpoint status:
Artifacts generated:
Artifacts reused:
Podcast dialogue words:
Podcast total pause milliseconds:
Podcast estimated duration:
Exercise blocks inspected:
Exercises covered / found:
Required answer units covered / found:
Source-exercise Q&A records:
Generated Q&A records:
Q&A records with answer limitations:
Missing exercises / answer units:
Duplicate conversions / unmatched source-exercise records:
Exercise coverage policy version:
Exercise coverage validation status:
Validation status:
```

For a skipped dataset report:

```text
Dataset:
Source ID:
Timestamp:
Checkpoint status: complete
Action: SKIPPED — already complete
Exercise coverage gate: PASS (current policy, matching hashes, complete inventory mappings)
Exercises covered / found:
Required answer units covered / found:
Q&A records with answer limitations:
Validation status: PASS
```

At the end of the entire run report:

```text
Total source files discovered:
New datasets completed:
Resumed datasets completed:
Stale datasets regenerated:
Datasets skipped from checkpoint:
Datasets failed:
Total datasets complete:
Total files generated during this run:
```

---

# 43. UPDATED FINAL OPERATING PRINCIPLE

Use this model:

```text
input/**/*.{txt,md,markdown,tex,rst}
       │
       ├── inspect checkpoint
       │
       ├── complete + unchanged + required validation and section 31A gate PASS
       │       └── SKIP
       │
       ├── partial / failed + unchanged
       │       └── RESUME USING STORED TIMESTAMP
       │
       ├── changed
       │       └── NEW TIMESTAMP + REGENERATE
       │
       └── new
               └── NEW TIMESTAMP + GENERATE
```

The governing rules are:

```text
ONE FILE = ONE DATASET
        ↓
CHECKPOINT BEFORE REPROCESSING
        ↓
UNCHANGED + VALIDATED (INCLUDING SECTION 31A) + COMPLETE = SKIP
        ↓
PARTIAL + UNCHANGED = RESUME
        ↓
CHANGED SOURCE = REGENERATE
        ↓
ONE TIMESTAMP PER GENERATION ATTEMPT
        ↓
SOURCE FIDELITY
        ↓
SKILL SCHEMA COMPLIANCE
        ↓
FULL VALIDATION
        ↓
MARK CHECKPOINT COMPLETE
```

Never mark a source file complete merely because some artifacts exist.

Never skip a source whose contents have changed.

Never generate a new timestamp merely because an unchanged generation was interrupted.

Never discard successfully completed artifacts solely because a later artifact failed.

Never treat an old Q&A completion flag as proof that all exercises were converted. Apply the current inventory and coverage gate before skipping or reusing it.

The checkpoint must make the full learning-set generation process safely restartable and idempotent. A dataset is complete only when every required artifact passes validation and every source exercise and required answer unit is represented by validated Q&A, with any answer limitations explicitly reported.
