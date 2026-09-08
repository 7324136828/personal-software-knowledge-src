---
name: mindmaps
description: Build a hierarchical mind map for one topic as JSON tree + Markdown outline + Mermaid diagram. Use when the user wants a mind map, concept map, or tree outline of a text.
---

# mindmaps

A single rooted tree that decomposes one topic. Scope: **topic**.

## Files

| Path | Format |
|------|--------|
| `output/mindmaps/mindmap_<topic>.json` | schema below |
| `output/mindmaps/mindmap_<topic>.md`   | derived — heading outline |
| `output/mindmaps/mindmap_<topic>.mmd`  | derived — Mermaid `mindmap` |

## JSON schema

Recursive node:

```json
{ "name": "string", "children": [ <node>, ... ] }
```

- Exactly one root object. Root `name` = the topic title (may include the original
  script, e.g. `"The Heart Sutra (心经)"`).
- Leaves carry `"children": []` (always present, never omitted).
- Depth 3–4 including the root. Level 1 = major themes; deepest level = concrete
  claims, terms, or short quotations.
- Aim for 3–7 children per non-leaf; keep `name` to a phrase, not a sentence.

## Markdown derivation

ATX headings, one node per line, pre-order (depth-first, children in array order):

```
# <root name>
## <level-1 name>
### <level-2 name>
#### <level-3 name>
```

Levels deeper than `####` are flattened to `####`.

## Mermaid derivation

```
mindmap
  root(("<root name>"))
    n1["<name>"]
      n2["<name>"]
    n3["<name>"]
```

- First line literally `mindmap`. Root line: `  root(("<name>"))` (two-space indent).
- Every other node: indent = `2 * depth` spaces, then `nN["<name>"]` where `N` is a
  1-based counter in pre-order (`n1`, `n2`, …).
- Escape `"` inside a label as `&quot;`. Do not put `()` `[]` `{}` in non-root labels
  (Mermaid parses them) — reword or use `—`.

## Checklist

- [ ] one root; every node has `name` + `children` array
- [ ] `.md` line count = node count; heading depth tracks tree depth
- [ ] `.mmd` starts with `mindmap`, ids sequential in pre-order, indent = 2×depth
