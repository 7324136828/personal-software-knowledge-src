# File Naming Convention

## Purpose

Use this skill whenever generating dataset artifacts such as data tables, flashcards, infographics, mind maps, podcasts, Q&A datasets, quizzes, reports, or slides.

The goal is to give every generated dataset a timestamp-based identifier derived from the local system time.

## Timestamp source

Before generating files for a dataset, run the Python timestamp script:

```bash
python generate_system_time.py
```

The script returns the current local system time in this format:

```text
YYYYMMDDHHMMSS
```

Example:

```text
20260907121900
```

Treat the script output as the dataset's `TIMESTAMP`.

## Critical rule: generate the timestamp once per dataset

For each dataset-generation task:

1. Run `generate_system_time.py` exactly once at the start of the dataset-generation run.
2. Store the returned value as `TIMESTAMP`.
3. Reuse that exact same `TIMESTAMP` for every related file generated for that dataset.
4. Do not independently generate a new timestamp for each file.
5. If a separate dataset is generated later, run the script again and use the new output.
6. Never invent, approximate, manually type, or derive the timestamp from the language model's own notion of the current time when the script is available.

This keeps all artifacts belonging to the same dataset grouped under the same timestamp.

## General naming rules

- Use only the timestamp emitted by `generate_system_time.py`.
- Timestamp format: `YYYYMMDDHHMMSS`.
- Do not include spaces in filenames.
- Use lowercase prefixes unless a format below specifies otherwise.
- Preserve the required file extension exactly.
- Create the target directory if it does not already exist.
- Do not overwrite an existing dataset unless explicitly instructed.
- When a timestamp collision occurs, run the timestamp script again after the system clock advances, or otherwise follow the caller's explicit collision policy.
- Files belonging to one logical dataset must use the same `TIMESTAMP`.

## Required directory and filename patterns

Assume:

```text
TIMESTAMP=20260907121900
```

### Data tables

Directory:

```text
datatables/
```

Files:

```text
datatables/20260907121900.csv
datatables/20260907121900.json
```

Template:

```text
datatables/{TIMESTAMP}.csv
datatables/{TIMESTAMP}.json
```

### Flashcards

Directory:

```text
flashcards/
```

Files:

```text
flashcards/flashcards_20260907121900.json
flashcards/flashcards_20260907121900.txt
```

Template:

```text
flashcards/flashcards_{TIMESTAMP}.json
flashcards/flashcards_{TIMESTAMP}.txt
```

The JSON dataset should contain 20 flashcards unless the user explicitly requests a different number.

### Infographics

Directory:

```text
infographics/
```

Files:

```text
infographics/infographic_20260907121900.html
infographics/infographic_20260907121900.json
infographics/infographic_20260907121900.md
infographics/infographic_20260907121900.svg
infographics/infographic_20260907121900.wireframe.txt
infographics/assets/install_flow.svg
```

Template:

```text
infographics/infographic_{TIMESTAMP}.html
infographics/infographic_{TIMESTAMP}.json
infographics/infographic_{TIMESTAMP}.md
infographics/infographic_{TIMESTAMP}.svg
infographics/infographic_{TIMESTAMP}.wireframe.txt
infographics/assets/install_flow.svg
```

The shared asset `assets/install_flow.svg` is not timestamped unless the user explicitly requests versioned assets.

### Mind maps

Directory:

```text
mindmaps/
```

Files:

```text
mindmaps/mindmap_20260907121900.mmd
mindmaps/mindmap_20260907121900.md
mindmaps/mindmap_20260907121900.json
```

Template:

```text
mindmaps/mindmap_{TIMESTAMP}.mmd
mindmaps/mindmap_{TIMESTAMP}.md
mindmaps/mindmap_{TIMESTAMP}.json
```

### Podcasts

Directory:

```text
podcasts/
```

Exactly one episode file per study set/source-file dataset:

```text
podcasts/20260907121900_episode0.json
```

Template:

```text
podcasts/{TIMESTAMP}_episode0.json
```

The `episode0` suffix is retained for compatibility and names the sole episode.
Honor a caller-specified destination or filename. Do not generate additional
episode files. The episode must have an estimated runtime of at most 20 minutes
at 150 dialogue words per minute plus explicit pauses; follow `podcast-script/SKILL.md`.

### Q&A datasets

Directory:

```text
qandas/
```

File:

```text
qandas/20260907121900.json
```

Template:

```text
qandas/{TIMESTAMP}.json
```

### Quizzes

Directory:

```text
quizzes/
```

Files:

```text
quizzes/quiz_20260907121900.json
quizzes/quiz_best_choices.json
```

Template:

```text
quizzes/quiz_{TIMESTAMP}.json
quizzes/quiz_best_choices.json
```

`quiz_best_choices.json` is intentionally a stable, non-timestamped filename unless the user explicitly requests versioning.

### Reports

Directory:

```text
reports/
```

Files:

```text
reports/report_20260907121900.md
reports/report_20260907121900.json
reports/report_20260907121900.html
```

Template:

```text
reports/report_{TIMESTAMP}.md
reports/report_{TIMESTAMP}.json
reports/report_{TIMESTAMP}.html
```

### Slides

Directory:

```text
slides/
```

Files:

```text
slides/slides_20260907121900.md
slides/slides_20260907121900.json
```

Template:

```text
slides/slides_{TIMESTAMP}.md
slides/slides_{TIMESTAMP}.json
```

## Language-model execution procedure

When a language model is instructed to generate one of these datasets, it should follow this procedure:

```text
1. Execute: python generate_system_time.py
2. Read stdout and store it as TIMESTAMP.
3. Validate that TIMESTAMP contains exactly 14 digits.
4. Create the required dataset directory/directories.
5. Generate every requested output using the filename patterns in this skill.
6. Reuse the same TIMESTAMP for all files belonging to that dataset-generation run.
7. Return or report the generated file paths.
```

## Timestamp validation

A valid timestamp:

- contains exactly 14 characters;
- contains digits only;
- follows `YYYYMMDDHHMMSS`.

Equivalent regular expression:

```regex
^\d{14}$
```

If the script does not return a valid timestamp, stop filename generation and report the error rather than inventing a timestamp.

## Example: one multi-format generation run

Suppose the script returns:

```text
20260907121900
```

If the same task generates a report, slides, and flashcards as parts of one logical dataset, all related files should reuse that timestamp:

```text
reports/report_20260907121900.md
reports/report_20260907121900.json
reports/report_20260907121900.html

slides/slides_20260907121900.md
slides/slides_20260907121900.json

flashcards/flashcards_20260907121900.json
flashcards/flashcards_20260907121900.txt
```

Do not produce filenames such as:

```text
reports/report_20260907121900.md
reports/report_20260907121901.json
slides/slides_20260907121903.md
```

for files that belong to the same logical dataset run.

## Reference Python implementation

The required script is:

```python
#!/usr/bin/env python3
from datetime import datetime

def current_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")

if __name__ == "__main__":
    print(current_timestamp())
```
