# Model Generator

A research toolkit for generating synthetic PlantUML use case model pairs (antipattern + refactored) for training LLM-based antipattern detection systems.

Each prompt produces two UML diagrams of the same system: **Version 1** contains one or more embedded antipatterns; **Version 2** is the correctly refactored version. Pairs are used as training data for a classifier that detects antipatterns in use case models.

---

## Requirements

- Python 3.12+
- Java (for PlantUML rendering)
- `plantuml.jar` — bundled with the [PlantUML VSCode extension](https://marketplace.visualstudio.com/items?itemName=jebbs.plantuml) or downloadable from [plantuml.com](https://plantuml.com/download)
- An [Anthropic API key](https://console.anthropic.com/)

Install Python dependencies:

```bash
uv sync
# or: pip install anthropic pyyaml pillow
```

---

## Scripts

### 1. `generate_models.py` — Generate model pairs

Calls the Claude API to generate paired PlantUML use case models, renders images, and writes training data.

**New run:**

```bash
python generate_models.py \
  --config antipattern_config.yaml \
  --domains-config domains.yaml \
  --plantuml-jar /path/to/plantuml.jar \
  --output-dir output \
  --num-prompts 194 \
  --sizes small medium \
  --size-weights 0.4 0.6 \
  --task-mode detect \
  --rate-limit 2.5
```

**Resume an interrupted run:**

```bash
python generate_models.py \
  --resume output/run_20260321_004201 \
  --plantuml-jar /path/to/plantuml.jar \
  --rate-limit 2.5
```

**Resume and regenerate flagged prompts** (marked `bad` or `needs-rework` in the reviewer):

```bash
python generate_models.py \
  --resume output/run_20260321_004201 \
  --reprocess-flagged \
  --plantuml-jar /path/to/plantuml.jar \
  --rate-limit 2.5
```

**Key arguments:**

| Argument | Description |
|---|---|
| `--config` | YAML file defining antipatterns and refactoring strategies |
| `--domains-config` | Optional ordered domain pool (assigned sequentially) |
| `--plantuml-jar` | Path to `plantuml.jar` |
| `--num-prompts` | Number of pairs to generate (default: 10) |
| `--sizes` | One or more of `small`, `medium`, `large` |
| `--size-weights` | Sampling weights for each size (must match `--sizes` count) |
| `--task-mode` | `detect` or `detect-and-refactor` |
| `--output-dir` | Root output directory (default: `./output`) |
| `--rate-limit` | Seconds to wait between API calls (default: 2.5) |
| `--seed` | Random seed for reproducibility |
| `--resume` | Path to an existing run directory to resume |
| `--reprocess-flagged` | With `--resume`: regenerate prompts flagged in review |

**Size ranges (construct counts):**

| Size | Constructs | Max antipattern instances |
|---|---|---|
| small | 9 – 13 | 1 |
| medium | 24 – 30 | 3 |
| large | 31 – 54 | 5 |

**Environment variable:** Set `ANTHROPIC_API_KEY` or pass `--api-key`.

---

### 2. `review_models.py` — Review model pairs (GUI)

A desktop Tkinter GUI for reviewing generated pairs side-by-side.

```bash
python review_models.py output/run_20260321_004201
# or omit the path to open a folder picker on launch
```

**Features:**

- Side-by-side antipattern (red) / refactored (green) image panels
- Draggable splitter between panels
- Zoom controls: fit-to-panel toggle, `−` / `+` buttons, scroll wheel
- Click image to open a popup sized to the image
- Mark each pair: **Approved** (green) / **Needs Rework** (orange) / **Clear**
- **Next Flagged** button — jumps to the next `needs-rework` prompt
- Notes field for reviewer feedback (saved on focus-out)
- Jump-to-prompt number field
- Progress summary (reviewed / total, counts per status)

State is persisted to `review.json` inside the run directory and restored on reopen. Reviewer notes are passed back to Claude when reprocessing with `--reprocess-flagged`.

---

### 3. `fix_associations.py` — Fix directed actor associations

Replaces directed arrows (`-->`) between actors and use cases with undirected lines (`--`) as required by the UML standard, then re-renders images.

```bash
python fix_associations.py output/run_20260321_004201 \
  --plantuml-jar /path/to/plantuml.jar
```

Preview changes without modifying files:

```bash
python fix_associations.py output/run_20260321_004201 \
  --plantuml-jar /path/to/plantuml.jar \
  --dry-run
```

Run this after generation if any diagrams have directed actor-to-use-case associations. Include/extend (`..>`) and generalisation (`--|>`) are left untouched.

---

## Configuration files

### `antipattern_config.yaml`

Defines the antipatterns to embed and their refactoring strategies. Each entry has a `code`, human-readable `name`, `description`, and one or more `refactorings` with explicit criteria.

```yaml
antipatterns:
  - code: a4
    name: "Functional Decomposition: Using the include relationship"
    description: |
      ...
    refactorings:
      - name: "Drop Functional Decomposition"
        description: |
          ...
```

Add new antipatterns here to expand the training set.

### `domains.yaml`

An ordered pool of 194 application domains assigned sequentially to prompts for reproducible, diverse coverage. Grouped into everyday, moderately common, and niche domains. Pass via `--domains-config`.

---

## Output structure

Each run produces a timestamped directory:

```
output/run_YYYYMMDD_HHMMSS/
│
├── models/                              # One folder per prompt
│   ├── 001_banking_system/
│   │   ├── banking_system_antipattern.puml
│   │   ├── banking_system_antipattern.jpg
│   │   ├── banking_system_refactored.puml
│   │   ├── banking_system_refactored.jpg
│   │   └── 001_response.txt             # Raw Claude response
│   ├── 002_restaurant_management_system/
│   └── ...
│
├── training_samples/                    # Per-prompt training data
│   ├── antipattern/
│   │   ├── 001_banking_system_antipattern.jinja
│   │   ├── 001_banking_system_antipattern.yaml
│   │   └── ...
│   └── refactored/
│       ├── 001_banking_system_refactored.jinja
│       ├── 001_banking_system_refactored.yaml
│       └── ...
│
├── audit/                               # Full API trace per prompt
│   ├── 001_audit.txt
│   └── ...
│
├── stats_antipattern.csv                # Per-prompt antipattern model stats
├── stats_refactored.csv                 # Per-prompt refactored model stats
├── stats_combined.csv                   # Merged, sorted by prompt_num
├── training_samples.jsonl               # Unified JSONL for fine-tuning
├── training_samples.yaml                # Same data in YAML format
├── run.log                              # Full debug log
├── run_state.json                       # Resumable state
├── review.json                          # Reviewer status + notes (if reviewed)
└── domains.yaml                         # Domain usage summary
```

**CSV columns:** `prompt_num`, `domain_display`, `size`, `antipattern_codes`, `antipattern_instance_counts`, `total_antipattern_instances`, `sample_type`, `task_mode`, `actors`, `use_cases`, `includes`, `extends`, `generalizations`, `total_parsed`

**JSONL columns:** `sample_id`, `prompt_num`, `domain_display`, `size`, `antipattern_names`, `antipattern_instance_counts`, `total_antipattern_instances`, `sample_type`, `task_mode`, `generated_at`, `input`, `output`

---

## Typical workflow

```
1. generate_models.py          →  generate pairs for all domains
2. review_models.py            →  mark quality, add notes for bad/needs-rework prompts
3. generate_models.py --resume --reprocess-flagged  →  regenerate flagged prompts
4. review_models.py            →  re-review regenerated prompts
5. fix_associations.py         →  fix any directed actor associations (optional)
```

Repeat steps 2–4 until satisfied. The final `training_samples.jsonl` is ready for fine-tuning.