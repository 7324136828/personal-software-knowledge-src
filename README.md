# Skill-Driven Python Content Generator

This Python 3.11+ application turns a source document into a learning artifact. Each
action is governed by the complete corresponding `skills/<name>/SKILL.md`; action
modules add only small task-specific context and contain no provider API code.

## Quickstart (Web UI & Backend)

### 1. Setup Environment
Automatically create the Python virtual environment (`.venv`), install backend dependencies, and install React frontend npm dependencies:

- **Windows**:
  ```cmd
  setup.bat
  ```
- **Linux / macOS**:
  ```bash
  chmod +x setup.sh run.sh
  ./setup.sh
  ```
*(Both scripts dispatch `setup.py`.)*

### 2. Run Application
Concurrently launch the FastAPI backend (`http://127.0.0.1:8000`) and the Vite React frontend (`http://localhost:5173`):

- **Windows**:
  ```cmd
  run.bat
  ```
- **Linux / macOS**:
  ```bash
  ./run.sh
  ```
*(Both scripts dispatch `run.py`.)*

On Windows, use `run_lan.bat` to allow access from other devices on your LAN.
It binds both servers to `0.0.0.0`, starting with UI port **5110** and API port
**8310**. Open `http://<your-PC-LAN-IP>:5110` on another device; if a port is busy,
use the actual port printed at startup. `run_lan.bat -Local` (also used by
`run_default.bat`) uses the same ports with access limited to this PC.
Set `FRONTEND_PORT` or `BACKEND_PORT` before launching to override these defaults.

Open your browser at **http://localhost:5173**. In the UI you can:
- **Select or drop multiple PDF files** (or Word, Markdown, plain text).
- **Paste PDF files** directly from your clipboard (or paste raw text).
- All 9 content-generation skills are selected by default; deselect any you do not need. Every selected artifact is generated for every selected source.
- The Connector is the default local provider. Select an active library model from the model picker, or refresh it after updating The Connector's configurations.
- Persist each job in an isolated `conversion_history/` folder so completed and interrupted conversions survive restarts.
- Open **File History** to expand each source into its artifact jobs, inspect retry logs,
  retry failures with a chosen character-based chunk size, cancel work, or download
  completed artifacts as ZIP archives. Queued files can be dragged to change priority.

## Web interface

The landing page is the starting point for a conversion. Add one or more source files,
select the learning artifacts to generate, choose a connector and model, then queue the
work. Each selected artifact is generated for every selected file.

![Landing page: source upload, artifact selection, and generation settings](images/landing_page.png)

**File History** keeps related artifact jobs together under their source file. Expand a
file to see individual statuses and recent logs. Failed artifacts can be retried with a
custom character-based chunk size; completed artifacts can be downloaded as ZIP files.
Use **Download completed** on a file to download its finished study artifacts together,
even while other types are queued, running, or failed. The ZIP includes the source,
completed outputs and their metadata, plus a `manifest.json` listing the status of
every artifact at download time. Download again later to include newly completed types.
Queued files can be moved higher or lower in the processing order by dragging them.

![File History: grouped artifact jobs, logs, and queue controls](images/history_page.png)

---

## Installation (CLI Mode)

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
| `the_connector` | running local The Connector backend; no API key | `THE_CONNECTOR_MODEL` / first discovered active library model |
| `openai` | `OPENAI_API_KEY` | `OPENAI_MODEL` / `gpt-4.1-mini` |
| `anthropic` / `claude` | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` / `claude-sonnet-4-5` |
| `openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` / `openai/gpt-4.1-mini` |
| `ollama` | local Ollama server | `OLLAMA_MODEL` / `llama3.2` |

The Connector defaults to `http://127.0.0.1:8301/v1` (`THE_CONNECTOR_BASE_URL`).
The app discovers active saved configuration IDs with `GET /v1/models` and generates
study artifacts with `POST /v1/chat/completions`. Start The Connector and save/activate
at least one library configuration first. Use its library `model_id`, rather than a
raw upstream model name. Upstream credentials and routing remain in The Connector.
`THE_CONNECTOR_TIMEOUT` defaults to 600 seconds; model discovery uses at most 10 seconds.
The web UI proxies discovery through this app's backend, including for LAN clients.
Unknown library aliases use conservative pipeline budgets and prompt-based JSON so
optional provider settings do not override the saved route. Deployment-specific
budget overrides remain available through the existing pipeline controls.

For CLI generation, use `--connector the_connector --model <active-library-model-id>`;
omit `--model` to use `THE_CONNECTOR_MODEL` or discover the first active model.
`config.yaml` now selects `the_connector` by default, with `url`, `model`, and `timeout`
settings in its `the_connector` section. Other providers remain selectable.

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

Podcasts produce exactly one self-contained episode per study set, with an estimated
runtime of at most 20 minutes at 150 dialogue words per minute plus explicit pauses.
Long sources are summarized to fit; there is no minimum length. Chunked podcast
drafts use a final model assembly pass even with `--aggregation deterministic` so
the episode has one introduction and sign-off. Overlong results fail validation.

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
  extraction, capped exponential retry/recovery, hierarchical aggregation, checkpoints,
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

API failures retry by default until a successful response is received. Delays follow
`1s, 2s, 4s, 8s, ... 64s, 128s, 240s, 240s, ...`. For HTTP 400, 413, or 422 responses,
the next four attempts use a smaller prompt and output budget; the second attempt also
falls back from provider-side JSON formatting while retaining the JSON instruction. If
all four variants are rejected, the pipeline regenerates that stage from smaller source
chunks. Authentication and authorization failures, plus local token-budget errors,
remain terminal. Use `--retries N` when a finite retry limit is required; `--retries 0`
disables provider retries.

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
than overwriting one `output_<timestamp>.txt` file. The version-2 batch checkpoint is
atomically saved before and after every artifact. It records completed artifacts for each
source plus the artifact work folder, pre-separated chunk paths, next chunk number, and
completed chunk outputs. Every artifact folder also has its own content-addressed
`checkpoint.json`, updated after each successful chunk. Version-1 checkpoints whose
`in_progress` field is a list of filenames are migrated on load. A failed or interrupted
run resumes the unfinished artifact and reuses its valid chunk outputs.

The batch checkpoint uses this shape (chunk text is stored in the referenced files):

```json
{
  "version": 2,
  "completed": ["Chapter 003. Statistical Learning.txt"],
  "in_progress": {
    "Chapter 004. Linear Regression.txt": {
      "timestamp": "20260909190548",
      "completed": ["datatables", "flashcards"],
      "in_progress": {
        "infographics": {
          "status": "in_progress",
          "folder": "tmp/Chapter 004. Linear Regression/infographics",
          "checkpoint": "tmp/Chapter 004. Linear Regression/infographics/checkpoint.json",
          "text": ["chunks/chunk_0001.txt", "chunks/chunk_0002.txt"],
          "current_at": 2,
          "outputs": ["artifacts/generated/chunk_0001.json"]
        }
      }
    }
  }
}
```

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
