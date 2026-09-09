# Skill-Driven Python Content Generator

This Python 3.11+ application turns a source document into a learning artifact. Each
action is governed by the complete corresponding `skills/<name>/SKILL.md`; action
modules add only small task-specific context and contain no provider API code.

## Installation

Create and activate a virtual environment, then install the provider SDKs and binary
document readers:

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Copy `.env.example` to a private location or set the variables in your shell. The
application reads the process environment directly; it does not automatically load a
`.env` file and never stores credentials.

## Usage

The main interface requires a connector, input, action, and exact output path:

```bash
python orchestrator.py \
  --connector openai \
  --input "input/book1.pdf" \
  --action create_datatables \
  --output "tmp/output_20260907115400.txt"
```

Use `--model` to override the connector's environment/default model for one run:

```bash
python orchestrator.py \
  --connector ollama \
  --model llama3.2 \
  --input "input/book1.tex" \
  --action create_flashcards \
  --output "tmp/flashcards.txt"
```

Large inputs use a token-aware semantic chunk pipeline by default. The legacy command
shape remains valid; advanced controls are optional:

```bash
python orchestrator.py \
  --connector anthropic \
  --model MODEL_NAME \
  --input "input/Chapter 005. Variable Selection.txt" \
  --action create_qandas \
  --strategy multi_pass \
  --chunk-tokens 12000 \
  --chunk-overlap 500 \
  --max-output-tokens 8000 \
  --aggregation hierarchical \
  --retries 3 \
  --checkpoint \
  --validate \
  --keep-raw \
  --output "tmp/Chapter005/qandas/final.json"
```

`--model-profile profile.json`, `--context-window`, and
`--profile-max-output-tokens` let deployments provide verified limits for unknown or
locally configured models. `--force` regenerates completed stages but never overwrites
old raw responses. `--experiment NAME` loads one preset from `experiments/configs/`;
explicit flags take precedence.

Action modules are directly executable through the same shared runtime:

```bash
python create_qandas.py \
  --connector claude \
  --input "input/book1.md" \
  --output "tmp/qandas.json"
```

The requested `--output` filename is never changed. Missing parent directories are
created automatically, the model response is not printed to the terminal, and output
is UTF-8. For skills that describe several related representations, run once per
desired output extension; the explicit destination tells the model which
representation to return.

If an output extension is not one of the skill's named formats (for example, a
data-table result requested as `.txt`), the prompt requests the skill's canonical
source-of-truth representation as plain UTF-8 text.

## Connectors

| Connector | Required configuration | Model setting/default |
|---|---|---|
| `openai` | `OPENAI_API_KEY` | `OPENAI_MODEL` / `gpt-4.1-mini` |
| `anthropic` / `claude` | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` / `claude-sonnet-4-5` |
| `openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` / `openai/gpt-4.1-mini` |
| `ollama` | local Ollama server | `OLLAMA_MODEL` / `llama3.2` |

Ollama defaults to `http://localhost:11434` (`OLLAMA_BASE_URL`). Its request timeout is
controlled by `OLLAMA_TIMEOUT`. OpenRouter also supports `OPENROUTER_BASE_URL`,
`OPENROUTER_HTTP_REFERER`, and `OPENROUTER_APP_NAME`.

Provider failures, rate limits, unavailable models, and context-limit errors use exit
code 6. Provider exception bodies are not echoed because SDK errors can contain prompt
text or sensitive request details.

## Actions

| Action | Authoritative skill |
|---|---|
| `create_datatables` | `skills/datatables/SKILL.md` |
| `create_flashcards` | `skills/flashcards/SKILL.md` |
| `create_infographics` | `skills/infographics/SKILL.md` |
| `create_mindmaps` | `skills/mindmaps/SKILL.md` |
| `create_podcasts` | `skills/podcast-script/SKILL.md` (legacy supplied folder name) |
| `create_qandas` | `skills/qandas/SKILL.md` |
| `create_quizzes` | `skills/quizzes/SKILL.md` |
| `create_reports` | `skills/reports/SKILL.md` |
| `create_slides` | `skills/slides/SKILL.md` |

## Input formats

Supported formats are `.txt`, `.md`, `.markdown`, `.tex`, `.rst`, `.json`, `.csv`,
`.html`, `.htm`, `.pdf`, and `.docx`. Plain text and LaTeX are decoded without
rewriting mathematical notation. PDF extraction retains page markers; DOCX extraction
retains headings, lists, and tables where the document structure exposes them.

The loader is a registry. Add a new format without modifying the orchestrator:

```python
from pathlib import Path
from document_loader import register_loader

@register_loader(".custom")
def load_custom(path: Path) -> str:
    return path.read_text(encoding="utf-8")
```

Documents are split losslessly at chapters, sections, exercises, paragraphs, lists,
tables, and token-based fallbacks. Code fences, LaTeX environments/equations, tables,
lists, and recognized exercise blocks are atomic whenever they fit the verified model
budget. Oversized protected blocks fail explicitly instead of being silently cut.

## Architecture

- `orchestrator.py` parses and coordinates the primary CLI.
- `cli_runtime.py` holds reusable dispatch logic for both CLI forms.
- `document_loader.py` extracts normalized source text.
- `skill_loader.py` reads the complete authoritative skill.
- `action_base.py` constructs the grounded prompt shared by the small `create_*.py`
  modules.
- `connectors/` contains all provider-specific code behind `LLMConnector`.
- `pipeline/` contains model profiles, token budgeting, semantic chunking, normalized
  extraction, bounded retry/recovery, hierarchical aggregation, checkpoints,
  provenance, and validation.
- `experiment.py` and `experiments/` define reproducible strategy/model comparisons;
  see [experiments/README.md](experiments/README.md).
- `result_processor.py` removes accidental whole-response code fences and validates
  `.json` output before it is written.
- `output_writer.py` writes only to the caller's requested destination.

Intermediates are written to `tmp/<source>/<artifact>/` by default (or `--work-dir`):
source chunks and metadata under `chunks/`, per-chunk extractions/artifacts, append-only
request/response evidence under `raw/`, aggregation tree nodes, validation metrics, and
`checkpoint.json`. Restarts reuse only content-addressed complete stages; changed source,
skill, model, profile, or generation settings invalidate the relevant checkpoint.

## Batch generation from `config.yaml`

Use `main.py` to run every action for source files in `input/` that are not listed in
`.checkpoint.json`'s `completed` array:

```bash
python main.py
python main.py --dry-run
python main.py --actions create_flashcards create_qandas
```

`main.py` resolves `${ENVIRONMENT_VARIABLE}` values in the selected provider's
configuration before starting child processes. This lets one `config.yaml` retain
credentials placeholders for several providers without requiring keys for providers
that are not selected. It reads `provider.default` (or the compatible
`default.provider`) to select the connector, maps that provider's YAML settings into
the connector environment variables, and then invokes `orchestrator.py` once per
unfinished source/action pair.

For one input source, all actions share a timestamp. Output paths use
`tmp/output_<action>_<YYYYMMDDHHmmss>.txt`, keeping the nine responses separate rather
than overwriting one `output_<timestamp>.txt` file. The checkpoint is atomically saved
before work begins and after all actions for a source succeed; failed or interrupted
sources remain unfinished for the next run.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success |
| 1 | Unexpected/general failure or interruption |
| 2 | Invalid CLI argument, action, or connector |
| 3 | Missing, unsupported, unreadable, or empty input |
| 4 | Missing or unreadable skill file |
| 5 | Missing connector configuration or SDK |
| 6 | Provider/API or invalid model-response failure |
| 7 | Output directory/file write failure |
