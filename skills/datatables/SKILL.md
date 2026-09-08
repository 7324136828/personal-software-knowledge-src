---
name: datatables
description: Build one comparative matrix across every source text in a dataset, as paired JSON + CSV. Use when the user wants a data table, comparison table, or feature matrix over a set of documents.
---

# datatables

One analytic table whose **rows are the source texts** of a dataset and whose
**columns are comparative axes** (not raw excerpts). Scope: **dataset**.

## Files

| Path | Format |
|------|--------|
| `output/datatables/<dataset>.json` | schema below |
| `output/datatables/<dataset>.csv`  | derived from the JSON |

## JSON schema

```json
{
  "title": "string — names the matrix",
  "fields": [
    { "name": "snake_case", "description": "what the column captures", "example": "one sample cell value" }
  ],
  "data": [
    { "<field.name>": "string", "...": "one key per field, in fields order",
      "source_id": "钱氏家训_en.txt", "page": "N/A" }
  ]
}
```

- 10–15 `fields`. Each is a *comparative dimension* — `central_concept`,
  `core_obstruction`, `prescribed_method`, `goal_state`, `scope`, `authority_basis`,
  `reward_promised`. At most one may be a short quotation column (`signature_line`).
- One `data` object per source text. Every field value is a **string**; use `"N/A"`
  when the dimension does not apply to that text. Lists inside a cell are
  `"semicolon; separated"`.
- Each `data` object also carries `source_id` (bare filename) and `page`
  (`"N/A"` unless the source has real page numbers).

## CSV derivation

- Header row: every `fields[].name` in order, then `source_id`, then `page`.
- One row per `data` object, same column order.
- RFC 4180 quoting: wrap a cell in `"` if it contains a comma, quote, or newline;
  double embedded quotes. LF line endings, trailing newline, no BOM.

## Checklist

- [ ] `fields` count 10–15, all `name`s snake_case and unique
- [ ] one `data` row per source `.txt`, keys in `fields` order
- [ ] every cell a string; `"N/A"` where inapplicable
- [ ] CSV header = field names + `source_id,page`; row count matches `data`
