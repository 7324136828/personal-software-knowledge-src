# Generation pipeline experiments

The experiment runner compares repeatable pipeline strategies without changing the
artifact generators. Every run is isolated under `experiments/results/<run-id>/` and
stores its resolved configuration, final aggregate, validation metrics, append-only
raw requests/responses, checkpoints, and generation log.

## Scenarios

| Scenario | What it tests |
|---|---|
| `baseline` | One full-source request. This is the compatibility/control case and may fail when the source does not fit. |
| `chunked` | Semantic section/exercise/paragraph chunking plus deterministic merge. |
| `chunked_with_overlap` | Chunking with bounded preceding context for continuity. |
| `hierarchical_aggregation` | Bounded fan-in tree aggregation for many chunk artifacts. |
| `extraction_then_generation` | A normalized knowledge inventory before artifact generation. |
| `token_optimized` | Conservative small chunks and output reservations for smaller models. |
| `truncation_recovery` | Finish-reason detection, JSON repair, continuation, and smaller-chunk fallback. |
| `multi_pass` | Extract, generate, validate/enrich, hierarchically aggregate, and validate. |
| `model_comparison` | The same source/action/scenario across explicit provider:model pairs. |

The corresponding JSON files in `configs/` are the authoritative definitions. The
folders in `scenarios/` make it easy to add scenario-specific fixtures later without
mixing them with measured results.

## Run

List presets:

```bash
python experiment.py --list
```

Run one preset (CLI values override the preset):

```bash
python experiment.py \
  --scenario hierarchical_aggregation \
  --connector openai \
  --model MODEL_NAME \
  --input input/Chapter005.txt \
  --action create_qandas
```

Compare models with identical settings. Model names may contain additional colons:

```bash
python experiment.py \
  --scenario model_comparison \
  --models openai:MODEL_A anthropic:MODEL_B ollama:llama3.2:3b \
  --input input/Chapter005.txt \
  --action create_qandas
```

Use `--dry-run` to inspect resolved configurations without creating files or calling a
provider. Resume an interrupted run with `--resume <run-directory>`; add `--force` to
regenerate complete stages while retaining prior raw evidence.

## Outputs and validation

Each run contains:

```text
config.json
aggregate.json (or the requested text extension)
metrics.json
validation.json
generation.log
pipeline/
  chunks/
  knowledge_and_artifacts/ or artifacts/
  aggregate/
  raw/
  checkpoint.json
```

Validation reports lexical section, exercise, and formula coverage; schema validity;
finish-reason/incomplete-output evidence; duplicate ratio; explicit source-reference
grounding; LaTeX delimiter integrity; ordering; bytes; estimated/provider token counts;
runtime; and cost only when verified prices are supplied in a model profile. A missing
measurement is `null`/`n/a`, never silently treated as zero or success. These heuristics
do not prove semantic correctness, so inspect low-scoring or high-stakes artifacts.

Compare every run below a results directory:

```bash
python experiment.py --compare experiments/results/
```

## Production recommendation

Start with `multi_pass` for large or heterogeneous sources and
`extraction_then_generation` for small local models. Use `chunked` when latency/cost is
more important and the selected model already follows the artifact schema reliably.
Treat `baseline` as a control only. Choose the winner for each model class from measured
coverage, exercise coverage, truncation, duplication, cost, and runtime rather than
advertised context size alone.
