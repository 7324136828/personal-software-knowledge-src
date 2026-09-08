---
name: qandas
description: Build a source-grounded study Q&A set for one dataset, including source exercises with answers when present. Use for study questions, worked review questions, and end-of-section/end-of-chapter exercises.
---

# qandas

A source-grounded question-and-answer study set for one source-file dataset. Scope: **dataset**.

When the source contains exercises, review questions, practice problems, or end-of-section/end-of-chapter questions, include them. Preserve meaningful LaTeX mathematics.

## File

`output/qandas/<dataset>.json`

The project README may override `<dataset>` with the run timestamp naming convention.

## JSON schema

```json
{
  "title": "string",
  "description": "string — scope and study instructions",
  "questions": [
    {
      "id": "kebab-case-slug",
      "kind": "source_exercise | generated",
      "exercise_label": "string | null",
      "question": "string — may contain JSON-escaped LaTeX",
      "answer": "string — concise or worked answer; may contain JSON-escaped LaTeX",
      "placeholder": "string — short learner hint ending with …",
      "required": true,
      "source_ids": ["source-file.tex"]
    }
  ]
}
```

## Content rules

- Include **all identifiable source exercises** when present. Multi-part exercises may become separate records such as `exercise-8-2-a`, `exercise-8-2-b`, etc.
- Keep the original exercise number in `exercise_label`; use `null` for generated questions.
- `kind` is `source_exercise` for a question taken or faithfully adapted from the source, otherwise `generated`.
- Answers must be grounded in the source plus mathematics directly implied by it. Do not invent missing data.
- For calculations, give the key formula and substitution, not only the final number.
- Preserve mathematics using LaTeX delimiters such as `\( ... \)` and `\[ ... \]` (properly JSON-escaped in the actual JSON file).
- Add generated questions as useful for conceptual coverage; order easier material before harder synthesis/application items when practical.
- `placeholder` is one short hint and ends with the literal character `…`.
- `required` is a boolean. Source exercises are normally `true`; optional extension questions may be `false`.
- `source_ids` contains the current source's bare filename, never a filesystem path.

## Exercise-answer policy

If the source exercise is solvable from the chapter, provide a worked or concise answer. If it genuinely requires information unavailable from the source or an external tool/data set, state that limitation in `answer` and describe the method rather than fabricating a result.

## Checklist

- [ ] every identifiable source exercise is represented when exercises are present
- [ ] every record has a non-empty `question`, `answer`, and at least one `source_id`
- [ ] `kind` is one of `source_exercise | generated`
- [ ] source exercises preserve numbering in `exercise_label`
- [ ] LaTeX is valid and JSON backslashes are escaped correctly
- [ ] every `placeholder` ends with `…`
- [ ] all `id`s are unique kebab-case
