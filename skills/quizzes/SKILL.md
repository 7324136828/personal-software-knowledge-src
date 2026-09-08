---
name: quizzes
description: Build a graded multiple-choice quiz for one topic as JSON with an answer key and explanations. Use when the user wants a quiz, test, or MCQ set with correct answers.
---

# quizzes

A graded multiple-choice quiz for one topic. Scope: **topic**.
(For open-ended prompts with no answer key, use `qandas` instead.)

## File

`output/quizzes/quiz_<topic>.json`

## JSON schema

```json
{
  "title": "string",
  "description": "string — what the quiz covers and how many questions",
  "questions": [
    {
      "question": "string",
      "options": ["string", "string", "string", "string"],
      "correct": 1,
      "explanation": "why the key is right; name a tempting distractor and why it is wrong",
      "difficulty": "recall | understanding | analysis | expert",
      "sources": ["心经_en.txt"]
    }
  ]
}
```

- **Exactly 4** `options`. `correct` is the 0-based index of the right option (integer).
- Vary the position of the correct answer across the quiz.
- ~10 questions, spread across the four `difficulty` levels, roughly easy→hard.
- Distractors must be plausible; prefer ones drawn from sibling texts in the same
  dataset (a Daoist term as a wrong answer on a Buddhist text, etc.).
- `explanation` cites the text's own wording where possible and disarms one distractor.
- `sources`: bare filenames the question is based on (≥1).

## Checklist

- [ ] every question has 4 options and an integer `correct` in 0..3
- [ ] `difficulty` is one of the four allowed values; all four appear across the quiz
- [ ] `explanation` present and non-trivial; `sources` non-empty
