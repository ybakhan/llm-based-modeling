#!/usr/bin/env python3
"""
generate_models.py

Simulates a multi-turn conversation with Claude Opus 4.6 to generate
PlantUML use case models for antipattern detection research and LLM fine-tuning.

Usage:
    python generate_models.py \
        --config antipattern_config.yaml \
        --plantuml-jar /path/to/plantuml.jar \
        --num-prompts 10 \
        --task-mode detect
"""

import anthropic
import yaml
import argparse
import csv
import os
import re
import time
import subprocess
import random
import sys
from pathlib import Path
from datetime import datetime
import logging

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
))
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger().addHandler(_console_handler)
logger = logging.getLogger(__name__)


def setup_file_logging(run_dir: Path) -> None:
    """Add a DEBUG-level file handler writing to run_dir/run.log."""
    log_path = run_dir / "run.log"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logging.getLogger().addHandler(fh)
    logger.info(f"Log file      : {log_path}")

# ── Size ranges (construct counts) ────────────────────────────────────────────

SIZE_RANGES = {
    "small":  (9,  13),
    "medium": (24, 30),
    "large":  (31, 54),
}

# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Generate UML use case model pairs (antipattern + refactored) "
                    "for antipattern detection research and LLM fine-tuning."
    )
    p.add_argument("--config",        required=True,
                   help="Path to YAML antipattern/refactoring config file.")
    p.add_argument("--plantuml-jar",  required=True,
                   help="Path to plantuml.jar for PNG conversion.")
    p.add_argument("--output-dir",    default=None,
                   help="Root output directory (default: ./output next to this script).")
    p.add_argument("--num-prompts",   type=int, default=10,
                   help="Number of model pairs to generate (default: 10).")
    p.add_argument("--pct-small",     type=float, default=0.30,
                   help="Fraction of small models, 9–13 constructs (default: 0.30).")
    p.add_argument("--pct-medium",    type=float, default=0.50,
                   help="Fraction of medium models, 24–30 constructs (default: 0.50).")
    p.add_argument("--pct-large",     type=float, default=0.20,
                   help="Fraction of large models, 31–54 constructs (default: 0.20).")
    p.add_argument("--task-mode",     choices=["detect", "detect-and-refactor"],
                   default="detect",
                   help="Training sample task: 'detect' (default) or 'detect-and-refactor'.")
    p.add_argument("--rate-limit",    type=float, default=2.5,
                   help="Seconds to wait between API calls (default: 2.5).")
    p.add_argument("--api-key",       default=None,
                   help="Anthropic API key (default: ANTHROPIC_API_KEY env var).")
    p.add_argument("--seed",          type=int, default=None,
                   help="Random seed for reproducible size distribution.")
    return p.parse_args()


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert arbitrary text to snake_case with no spaces."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def determine_sizes(n: int, pct_small: float, pct_medium: float, pct_large: float) -> list[str]:
    """Return a shuffled list of size labels matching the requested distribution."""
    n_small  = round(n * pct_small)
    n_large  = round(n * pct_large)
    n_medium = n - n_small - n_large
    sizes = ["small"] * n_small + ["medium"] * n_medium + ["large"] * n_large
    random.shuffle(sizes)
    return sizes


def write_file(content: str, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.info(f"    Wrote  {path}")
    return path


def convert_to_png(puml_path: Path, jar_path: str) -> Path:
    """Run PlantUML to generate a PNG beside the .puml file."""
    png_path = puml_path.with_suffix(".png")
    try:
        proc = subprocess.run(
            ["java", "-jar", str(jar_path), "-tpng", str(puml_path)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            logger.warning(f"    PlantUML warning: {proc.stderr.strip()[:200]}")
        else:
            logger.info(f"    PNG    {png_path}")
    except FileNotFoundError:
        logger.error("    'java' not found on PATH. PNG conversion skipped.")
    except subprocess.TimeoutExpired:
        logger.error(f"    PlantUML timed out for {puml_path}")
    except Exception as exc:
        logger.error(f"    PNG conversion failed: {exc}")
    return png_path


# ── Audit log ─────────────────────────────────────────────────────────────────

def write_audit(
    run_dir: Path,
    prompt_num: int,
    system_prompt: str,
    messages: list[dict],
    raw_response: str,
    model: str,
    usage: dict | None,
) -> Path:
    """Write a human-readable audit file for one API round-trip."""
    audit_dir = run_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"prompt_{prompt_num:03d}_audit.txt"

    sep = "─" * 72
    lines = [
        f"AUDIT LOG — Prompt #{prompt_num:03d}",
        f"Timestamp : {datetime.now().isoformat()}",
        f"Model     : {model}",
    ]
    if usage:
        lines.append(
            f"Tokens    : input={usage.get('input_tokens')}  "
            f"output={usage.get('output_tokens')}"
        )
    lines += [
        "",
        sep,
        "SYSTEM PROMPT",
        sep,
        system_prompt,
        "",
    ]
    for idx, msg in enumerate(messages, start=1):
        role = msg["role"].upper()
        content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        lines += [
            sep,
            f"MESSAGE {idx} — {role}",
            sep,
            content,
            "",
        ]
    lines += [
        sep,
        "ASSISTANT RESPONSE (raw)",
        sep,
        raw_response,
        "",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    logger.debug(f"  Audit      : {path}")
    return path


# ── Prompt builders ───────────────────────────────────────────────────────────

def build_system_prompt(cfg: dict, task_mode: str) -> str:
    ap = cfg["antipattern"]
    refactorings: list[dict] = ap["refactorings"]

    task_line = (
        "detect the antipattern — name it and list the exact constructs involved"
        if task_mode == "detect"
        else "detect the antipattern (name it and list the exact constructs involved) "
             "AND provide the full refactored PlantUML model"
    )

    # Build refactoring section — supports one or many entries
    rf_lines = []
    for idx, rf in enumerate(refactorings, start=1):
        prefix = f"[{idx}] " if len(refactorings) > 1 else ""
        rf_lines.append(f"{prefix}Name        : {rf['name']}")
        rf_lines.append(f"{prefix}Description : {rf['description']}")
    rf_section = "\n".join(rf_lines)

    # For the Version 2 comment, list all refactoring names
    rf_names = ", ".join(rf["name"] for rf in refactorings)

    return f"""\
You are an expert UML use case modeling consultant specialising in antipattern \
detection and refactoring for software engineering research.

Your task: for each request, generate exactly TWO versions of a PlantUML use case \
model for the specified domain and construct count.

═══ ANTIPATTERN TO EMBED ═══════════════════════════════════════════════════════
Name        : {ap['name']}
Description : {ap['description']}

═══ REFACTORING STRATEGY ════════════════════════════════════════════════════════
{rf_section}

═══ WHAT COUNTS AS A CONSTRUCT ══════════════════════════════════════════════════
  • Actor          – each actor node
  • Use Case       – each use case ellipse
  • Include        – each <<include>> dependency arrow
  • Extend         – each <<extend>> dependency arrow
  • Generalize     – each generalisation (inheritance) arrow

  TOTAL constructs = Actors + Use Cases + Includes + Extends + Generalisations.

═══ DOMAIN DIVERSITY RULE ═══════════════════════════════════════════════════════
Track every domain used in this conversation. NEVER repeat a domain.
Choose realistic, distinct application domains (e.g. food delivery, e-learning,
hospital management, smart home, online banking, …).

═══ MANDATORY RESPONSE FORMAT ═══════════════════════════════════════════════════
Follow this template EXACTLY — including the marker lines.

DOMAIN: <snake_case_domain>
DOMAIN_DISPLAY: <Human Readable Domain Name>
SIZE: <small|medium|large>

=== VERSION 1: WITH ANTIPATTERN ===
ANTIPATTERN_DETECTED: {ap['name']}
INSTANCE_COUNT: <number of distinct antipattern instances present in this model>

[INSTANCE 1]
CONSTRUCTS_INVOLVED: <comma-separated names of the constructs forming this instance>
EXPLANATION: <clear, specific explanation of why exactly these constructs constitute \
the "{ap['name']}" antipattern — reference each construct by name>

[INSTANCE 2]   ← include only if INSTANCE_COUNT > 1; add more blocks as needed
CONSTRUCTS_INVOLVED: <comma-separated names>
EXPLANATION: <explanation for this instance>

REFACTORING_RATIONALE: <what must change and why — address each instance explicitly, \
naming the constructs to be restructured and the goal of the refactoring>
CONSTRUCT_COUNT: <integer>

```plantuml
@startuml
' Version 1 – contains {ap['name']} antipattern
<model here>
@enduml
```

=== VERSION 2: REFACTORED ===
NO_ANTIPATTERN_DETECTED
CONSTRUCT_COUNT: <integer>

```plantuml
@startuml
' Version 2 – refactored, no antipattern ({rf_names})
<model here>
@enduml
```

═══ SYSTEM CONTEXT (BOUNDARY) RULE ══════════════════════════════════════════════
Every PlantUML model MUST include a system boundary rectangle:
  • Use a `rectangle "System Name" {{ }}` block to represent the system context.
  • ALL use cases MUST be placed INSIDE the rectangle.
  • ALL actors MUST be placed OUTSIDE the rectangle.
  • The rectangle label should be the name of the system being modelled.

Example structure:
  actor Customer
  rectangle "Online Store" {{
    usecase "Browse Products" as UC1
    usecase "Place Order" as UC2
  }}
  Customer --> UC1
  Customer --> UC2

═══ QUALITY RULES ════════════════════════════════════════════════════════════════
1. Version 1 MUST be a clear, unambiguous example of the "{ap['name']}" antipattern.
2. Version 2 MUST be a correct, fully refactored model with no antipattern.
3. The construct count in each version must match CONSTRUCT_COUNT exactly.
4. Both models must be realistic and domain-appropriate.
5. Every model MUST have a system boundary rectangle with actors outside and use cases inside.
6. These models train an LLM to {task_line}. \
Make them educationally clear examples.
"""


def build_user_message(prompt_num: int, size: str, construct_count: int) -> str:
    lo, hi = SIZE_RANGES[size]
    return (
        f"Prompt #{prompt_num:03d}: Generate a {size.upper()} use case model "
        f"with exactly {construct_count} constructs "
        f"(valid range for {size}: {lo}–{hi} constructs). "
        f"Select a NEW domain not yet used in this conversation."
    )


# ── Response parser ───────────────────────────────────────────────────────────

def parse_response(text: str) -> dict:
    """Extract all structured fields from Claude's formatted response."""

    def first_group(pattern, src=text, default=""):
        m = re.search(pattern, src)
        return m.group(1).strip() if m else default

    domain_raw      = first_group(r"DOMAIN:\s*(\S+)", default="unknown_domain")
    domain_display  = first_group(r"DOMAIN_DISPLAY:\s*(.+)", default=domain_raw)
    size            = first_group(r"SIZE:\s*(\S+)", default="unknown")
    antipattern_det = first_group(r"ANTIPATTERN_DETECTED:\s*(.+)", default="Unknown")

    counts = re.findall(r"CONSTRUCT_COUNT:\s*(\d+)", text)
    count_v1 = int(counts[0]) if len(counts) > 0 else None
    count_v2 = int(counts[1]) if len(counts) > 1 else None

    def extract_puml(section: str) -> str | None:
        m = re.search(r"```plantuml\s*(.*?)```", section, re.DOTALL)
        return m.group(1).strip() if m else None

    v1_sec = re.search(r"=== VERSION 1.*?===(.*?)=== VERSION 2", text, re.DOTALL)
    v2_sec = re.search(r"=== VERSION 2.*?===(.*?)$",             text, re.DOTALL)
    v1_text = v1_sec.group(1) if v1_sec else ""

    # ── Per-instance antipattern analysis ─────────────────────────────────────
    def parse_instances(section: str) -> list[dict]:
        """Return a list of {constructs_involved, explanation} dicts, one per instance."""
        # Split on [INSTANCE n] markers
        blocks = re.split(r"\[INSTANCE\s+\d+\]", section, flags=re.IGNORECASE)
        instances = []
        for block in blocks[1:]:  # skip text before first [INSTANCE]
            constructs = re.search(r"CONSTRUCTS_INVOLVED:\s*(.+)", block)
            # EXPLANATION runs until the next ALL-CAPS keyword line or end of block
            explanation = re.search(
                r"EXPLANATION:\s*(.*?)(?=\n[A-Z_]+\s*:|$)", block, re.DOTALL
            )
            instances.append({
                "constructs_involved": constructs.group(1).strip() if constructs else "",
                "explanation":         explanation.group(1).strip() if explanation else "",
            })
        # Fallback: no [INSTANCE] markers — single instance with old-style fields
        if not instances:
            constructs = re.search(r"CONSTRUCTS_INVOLVED:\s*(.+)", section)
            explanation = re.search(
                r"EXPLANATION:\s*(.*?)(?=\n[A-Z_]+\s*:|$)", section, re.DOTALL
            )
            instances.append({
                "constructs_involved": constructs.group(1).strip() if constructs else "Unknown",
                "explanation":         explanation.group(1).strip() if explanation else "",
            })
        return instances

    instances = parse_instances(v1_text)

    # Flat constructs string for backward-compatible CSV / logging
    constructs_inv = "; ".join(
        f"[{i+1}] {inst['constructs_involved']}"
        for i, inst in enumerate(instances)
    ) if len(instances) > 1 else (instances[0]["constructs_involved"] if instances else "Unknown")

    refactoring_rationale = first_group(
        r"REFACTORING_RATIONALE:\s*(.*?)(?=CONSTRUCT_COUNT|```|$)",
        src=v1_text, default="",
    )

    return {
        "domain":                 slugify(domain_raw),
        "domain_display":         domain_display,
        "size":                   size.lower(),
        "antipattern_detected":   antipattern_det,
        "instances":              instances,
        "constructs_involved":    constructs_inv,   # flat string for CSV / logging
        "refactoring_rationale":  refactoring_rationale,
        "construct_count_v1":     count_v1,
        "construct_count_v2":     count_v2,
        "antipattern_puml":       extract_puml(v1_sec.group(1)) if v1_sec else None,
        "refactored_puml":        extract_puml(v2_sec.group(1)) if v2_sec else None,
    }


# ── Training sample content generators ───────────────────────────────────────

_TASK_INSTRUCTION = {
    "detect": (
        "Analyze the following PlantUML use case model and detect if it contains "
        "any antipatterns.\n"
        "Base your analysis solely on the model provided. "
        "Do not use any outside knowledge."
    ),
    "detect-and-refactor": (
        "Analyze the following PlantUML use case model, detect if it contains "
        "any antipatterns,\n"
        "and if an antipattern is found, provide the full refactored PlantUML model.\n"
        "Base your analysis solely on the model provided. "
        "Do not use any outside knowledge."
    ),
}


def make_jinja_antipattern(
    puml: str,
    antipattern_name: str,
    instances: list[dict],
    refactoring_rationale: str,
    task_mode: str,
    refactored_puml: str | None = None,
) -> str:
    """Return the content of a .jinja training-sample file for the antipattern version."""
    instruction = _TASK_INSTRUCTION[task_mode]

    answer_lines = [f"Antipattern Detected: {antipattern_name}", ""]
    for idx, inst in enumerate(instances, start=1):
        prefix = f"Instance {idx}: " if len(instances) > 1 else ""
        answer_lines += [
            f"{prefix}Constructs Involved: {inst['constructs_involved']}",
            f"{prefix}Explanation: {inst['explanation']}",
            "",
        ]
    if refactoring_rationale:
        answer_lines += [f"Refactoring Rationale: {refactoring_rationale}", ""]
    if task_mode == "detect-and-refactor" and refactored_puml:
        answer_lines += ["Refactored Model:", refactored_puml]

    answer = "\n".join(answer_lines).rstrip()

    return (
        f"{instruction}\n"
        "\n\n"
        "PlantUML Model:\n"
        f"{puml}"
        "\n\n\n"
        "Answer:\n"
        f"{answer}\n"
    )


def make_jinja_refactored(puml: str, task_mode: str) -> str:
    """Return the content of a .jinja training-sample file for the refactored version."""
    instruction = _TASK_INSTRUCTION[task_mode]

    return (
        f"{instruction}\n"
        "\n\n"
        "PlantUML Model:\n"
        f"{puml}"
        "\n\n\n"
        "Answer:\n"
        "No antipattern detected.\n"
    )


def make_yaml_record(
    *,
    prompt_num: int,
    domain: str,
    domain_display: str,
    size: str,
    antipattern_name: str,
    constructs_involved: str | None,
    instances: list[dict] | None,
    refactoring_rationale: str | None,
    construct_count: int | None,
    puml: str,
    expected_output: str,
    sample_type: str,           # "antipattern" | "refactored"
    task_mode: str,
    puml_path: Path,
    png_path: Path,
    jinja_path: Path,
    generated_at: str,
    raw_response: str,
) -> dict:
    excerpt_limit = 600
    excerpt = (
        raw_response[:excerpt_limit] + " …[truncated]"
        if len(raw_response) > excerpt_limit
        else raw_response
    )

    task_instr = _TASK_INSTRUCTION[task_mode].replace("\n", " ")

    return {
        "metadata": {
            "prompt_number":     prompt_num,
            "domain":            domain,
            "domain_display":    domain_display,
            "size":              size,
            "antipattern_name":  antipattern_name,
            "sample_type":       sample_type,
            "task_mode":         task_mode,
            "construct_count":        construct_count,
            "constructs_involved":    constructs_involved,
            "instances":              instances or [],
            "refactoring_rationale":  refactoring_rationale or "",
            "generated_at":           generated_at,
            "files": {
                "plantuml": str(puml_path),
                "png":       str(png_path),
                "jinja":     str(jinja_path),
            },
        },
        "training_sample": {
            "input":  f"{task_instr}\n\nPlantUML Model:\n{puml}",
            "output": expected_output,
        },
        "raw_response_excerpt": excerpt,
    }


# ── PlantUML construct counter ────────────────────────────────────────────────

def parse_puml_stats(puml: str) -> dict:
    """Count UML constructs and detect system boundary in a PlantUML source string."""
    lines = puml.splitlines()

    actors         = sum(1 for l in lines if re.match(r"^\s*actor\b", l, re.IGNORECASE))
    use_cases      = sum(1 for l in lines if re.match(r"^\s*usecase\b", l, re.IGNORECASE)
                         or re.match(r"^\s*\(", l))
    includes       = sum(1 for l in lines if re.search(r"<<include>>", l, re.IGNORECASE)
                         or re.search(r"\.include\b", l, re.IGNORECASE))
    extends        = sum(1 for l in lines if re.search(r"<<extend>>", l, re.IGNORECASE)
                         or re.search(r"\.extend\b", l, re.IGNORECASE))
    generalizations = sum(1 for l in lines if re.search(r"--|>|<\|--", l))
    has_boundary   = any(re.match(r"^\s*rectangle\b", l, re.IGNORECASE) for l in lines)
    total_parsed   = actors + use_cases + includes + extends + generalizations

    return {
        "actors":          actors,
        "use_cases":       use_cases,
        "includes":        includes,
        "extends":         extends,
        "generalizations": generalizations,
        "total_parsed":    total_parsed,
        "has_system_boundary": has_boundary,
    }


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"    Wrote  {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    cfg = load_config(args.config)
    antipattern_name = cfg["antipattern"]["name"]

    # Validate percentages sum to ~1.0
    total_pct = args.pct_small + args.pct_medium + args.pct_large
    if not (0.99 <= total_pct <= 1.01):
        logger.error(f"pct-small + pct-medium + pct-large = {total_pct:.2f}; must sum to 1.0")
        sys.exit(1)

    # Output directories
    script_dir  = Path(__file__).parent
    output_root = Path(args.output_dir) if args.output_dir else script_dir / "output"
    run_ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir     = output_root / f"run_{run_ts}"
    models_dir   = run_dir / "models"
    training_dir = run_dir / "training_samples"

    run_dir.mkdir(parents=True, exist_ok=True)
    setup_file_logging(run_dir)

    logger.info(f"Output root  : {run_dir}")
    logger.info(f"Antipattern  : {antipattern_name}")
    logger.info(f"Task mode    : {args.task_mode}")
    logger.info(f"Num prompts  : {args.num_prompts}")
    logger.info(f"Distribution : small={args.pct_small:.0%}  medium={args.pct_medium:.0%}  large={args.pct_large:.0%}")

    # Anthropic client
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("No API key. Set ANTHROPIC_API_KEY or use --api-key.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Prepare conversation
    sizes         = determine_sizes(args.num_prompts, args.pct_small, args.pct_medium, args.pct_large)
    system_prompt = build_system_prompt(cfg, args.task_mode)
    conversation: list[dict] = []          # multi-turn message history

    # ── Main generation loop ───────────────────────────────────────────────────
    all_domains: list[str] = []
    ap_stats_rows:   list[dict] = []
    ref_stats_rows:  list[dict] = []
    training_rows:   list[dict] = []

    for i, size in enumerate(sizes, start=1):
        lo, hi           = SIZE_RANGES[size]
        construct_count  = random.randint(lo, hi)
        user_msg         = build_user_message(i, size, construct_count)

        logger.info("")
        logger.info(f"{'─'*60}")
        logger.info(f"Prompt {i}/{args.num_prompts}  size={size}  target_constructs={construct_count}")

        conversation.append({"role": "user", "content": user_msg})

        # ── API call ───────────────────────────────────────────────────────────
        _model = "claude-opus-4-6"
        _max_tokens = 4096
        logger.debug(
            f"  API request  model={_model}  max_tokens={_max_tokens}"
            f"  messages_in_history={len(conversation)}"
        )
        try:
            response = client.messages.create(
                model=_model,
                max_tokens=_max_tokens,
                system=system_prompt,
                messages=conversation,
            )
            raw = response.content[0].text
        except anthropic.RateLimitError:
            logger.warning("  Rate-limited by API. Sleeping 30 s then retrying once …")
            time.sleep(30)
            try:
                response = client.messages.create(
                    model=_model,
                    max_tokens=_max_tokens,
                    system=system_prompt,
                    messages=conversation,
                )
                raw = response.content[0].text
            except Exception as exc:
                logger.error(f"  Retry failed: {exc}. Skipping prompt {i}.")
                conversation.pop()
                continue
        except Exception as exc:
            logger.error(f"  API error: {exc}. Skipping prompt {i}.")
            conversation.pop()
            time.sleep(args.rate_limit)
            continue

        usage = vars(response.usage) if hasattr(response, "usage") else None
        if usage:
            logger.debug(
                f"  API response input_tokens={usage.get('input_tokens')}  "
                f"output_tokens={usage.get('output_tokens')}"
            )

        write_audit(run_dir, i, system_prompt, list(conversation), raw, _model, usage)

        conversation.append({"role": "assistant", "content": raw})

        # ── Parse ──────────────────────────────────────────────────────────────
        parsed  = parse_response(raw)
        domain  = parsed["domain"] or f"prompt_{i:03d}"
        domain_display = parsed["domain_display"]
        gen_at  = datetime.now().isoformat()

        all_domains.append(domain_display)
        logger.info(f"  Domain       : {domain_display}")
        logger.info(f"  V1 constructs: {parsed['construct_count_v1']}")
        logger.info(f"  V2 constructs: {parsed['construct_count_v2']}")
        logger.debug(f"  Antipattern  : {parsed['antipattern_detected']}")
        logger.debug(f"  Constructs   : {parsed['constructs_involved']}")
        logger.debug(f"  V1 PUML parsed: {parsed['antipattern_puml'] is not None}")
        logger.debug(f"  V2 PUML parsed: {parsed['refactored_puml'] is not None}")

        if not parsed["antipattern_puml"] or not parsed["refactored_puml"]:
            logger.warning("  Could not parse both PlantUML blocks — skipping file output for this prompt.")
            time.sleep(args.rate_limit)
            continue

        # ── Directory & file names ─────────────────────────────────────────────
        dir_name     = f"prompt_{i:03d}_{slugify(domain)}"
        prompt_dir   = models_dir / dir_name
        domain_slug  = slugify(domain)

        # ── PlantUML files ─────────────────────────────────────────────────────
        v1_puml = write_file(parsed["antipattern_puml"], prompt_dir / f"{domain_slug}_antipattern.puml")
        v2_puml = write_file(parsed["refactored_puml"],  prompt_dir / f"{domain_slug}_refactored.puml")

        v1_png = convert_to_png(v1_puml, args.plantuml_jar)
        v2_png = convert_to_png(v2_puml, args.plantuml_jar)

        # ── Descriptive statistics ─────────────────────────────────────────────
        v1_stats = parse_puml_stats(parsed["antipattern_puml"])
        v2_stats = parse_puml_stats(parsed["refactored_puml"])

        _base = dict(
            prompt_num=i,
            domain=domain,
            domain_display=domain_display,
            size=size,
            antipattern_name=antipattern_name,
            task_mode=args.task_mode,
            generated_at=gen_at,
        )
        ap_stats_rows.append({
            **_base,
            "sample_type":            "antipattern",
            "constructs_involved":    parsed["constructs_involved"],
            "construct_count_reported": parsed["construct_count_v1"],
            **v1_stats,
            "count_matches_reported": parsed["construct_count_v1"] == v1_stats["total_parsed"],
            "puml_file":              str(v1_puml),
            "png_file":               str(v1_png),
        })
        ref_stats_rows.append({
            **_base,
            "sample_type":            "refactored",
            "constructs_involved":    "",
            "construct_count_reported": parsed["construct_count_v2"],
            **v2_stats,
            "count_matches_reported": parsed["construct_count_v2"] == v2_stats["total_parsed"],
            "puml_file":              str(v2_puml),
            "png_file":               str(v2_png),
        })

        # ── Training sample – antipattern (negative) ───────────────────────────
        jinja_ap = write_file(
            make_jinja_antipattern(
                parsed["antipattern_puml"],
                parsed["antipattern_detected"],
                parsed["instances"],
                parsed["refactoring_rationale"],
                args.task_mode,
                parsed["refactored_puml"],
            ),
            training_dir / "antipattern" / f"prompt_{i:03d}_{domain_slug}_antipattern.jinja",
        )

        # Re-use the jinja answer text as the YAML expected_output too
        ap_answer = make_jinja_antipattern(
            parsed["antipattern_puml"],
            parsed["antipattern_detected"],
            parsed["instances"],
            parsed["refactoring_rationale"],
            args.task_mode,
            parsed["refactored_puml"],
        ).split("Answer:\n", 1)[-1].strip()

        _task_instr = _TASK_INSTRUCTION[args.task_mode].replace("\n", " ")

        write_file(
            yaml.dump(
                make_yaml_record(
                    prompt_num=i,
                    domain=domain,
                    domain_display=domain_display,
                    size=size,
                    antipattern_name=antipattern_name,
                    constructs_involved=parsed["constructs_involved"],
                    instances=parsed["instances"],
                    refactoring_rationale=parsed["refactoring_rationale"],
                    construct_count=parsed["construct_count_v1"],
                    puml=parsed["antipattern_puml"],
                    expected_output=ap_answer,
                    sample_type="antipattern",
                    task_mode=args.task_mode,
                    puml_path=v1_puml,
                    png_path=v1_png,
                    jinja_path=jinja_ap,
                    generated_at=gen_at,
                    raw_response=raw,
                ),
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
            training_dir / "antipattern" / f"prompt_{i:03d}_{domain_slug}_antipattern.yaml",
        )
        training_rows.append({
            "prompt_num":      i,
            "domain":          domain,
            "domain_display":  domain_display,
            "size":            size,
            "antipattern_name": antipattern_name,
            "sample_type":     "antipattern",
            "task_mode":       args.task_mode,
            "generated_at":    gen_at,
            "input":           f"{_task_instr}\n\nPlantUML Model:\n{parsed['antipattern_puml']}",
            "output":          ap_answer,
        })

        # ── Training sample – refactored (positive) ────────────────────────────
        rf_answer = "No antipattern detected."

        jinja_rf = write_file(
            make_jinja_refactored(parsed["refactored_puml"], args.task_mode),
            training_dir / "refactored" / f"prompt_{i:03d}_{domain_slug}_refactored.jinja",
        )

        write_file(
            yaml.dump(
                make_yaml_record(
                    prompt_num=i,
                    domain=domain,
                    domain_display=domain_display,
                    size=size,
                    antipattern_name=antipattern_name,
                    constructs_involved=None,
                    instances=None,
                    refactoring_rationale=None,
                    construct_count=parsed["construct_count_v2"],
                    puml=parsed["refactored_puml"],
                    expected_output=rf_answer,
                    sample_type="refactored",
                    task_mode=args.task_mode,
                    puml_path=v2_puml,
                    png_path=v2_png,
                    jinja_path=jinja_rf,
                    generated_at=gen_at,
                    raw_response=raw,
                ),
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
            training_dir / "refactored" / f"prompt_{i:03d}_{domain_slug}_refactored.yaml",
        )
        training_rows.append({
            "prompt_num":      i,
            "domain":          domain,
            "domain_display":  domain_display,
            "size":            size,
            "antipattern_name": antipattern_name,
            "sample_type":     "refactored",
            "task_mode":       args.task_mode,
            "generated_at":    gen_at,
            "input":           f"{_task_instr}\n\nPlantUML Model:\n{parsed['refactored_puml']}",
            "output":          rf_answer,
        })

        # ── Rate limit ─────────────────────────────────────────────────────────
        if i < args.num_prompts:
            logger.info(f"  Sleeping {args.rate_limit} s …")
            time.sleep(args.rate_limit)

    # ── Domain summary ────────────────────���────────────────────────────────────
    # ── CSV statistics output ──────────────────────────────────────────────────
    _csv_fields = [
        "prompt_num", "domain", "domain_display", "size",
        "antipattern_name", "sample_type", "task_mode",
        "constructs_involved",
        "construct_count_reported",
        "actors", "use_cases", "includes", "extends", "generalizations",
        "total_parsed", "count_matches_reported", "has_system_boundary",
        "generated_at", "puml_file", "png_file",
    ]
    write_csv(run_dir / "stats_antipattern.csv", ap_stats_rows,  _csv_fields)
    write_csv(run_dir / "stats_refactored.csv",  ref_stats_rows, _csv_fields)
    write_csv(run_dir / "stats_combined.csv",    ap_stats_rows + ref_stats_rows, _csv_fields)

    _training_fields = [
        "prompt_num", "domain", "domain_display", "size",
        "antipattern_name", "sample_type", "task_mode", "generated_at",
        "input", "output",
    ]
    write_csv(run_dir / "training_samples.csv", training_rows, _training_fields)

    unique_domains = list(dict.fromkeys(all_domains))  # preserve order, deduplicate
    domains_path = run_dir / "domains.yaml"
    domains_path.write_text(
        yaml.dump(
            {
                "all_domains":    all_domains,
                "unique_domains": unique_domains,
                "total":          len(all_domains),
                "unique_count":   len(unique_domains),
            },
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    logger.info(f"  domains.yaml  → {domains_path}")

    logger.info("")
    logger.info(f"{'═'*60}")
    logger.info(f"Done. All output under: {run_dir}")
    logger.info(f"  models/           → PlantUML + PNG pairs")
    logger.info(f"  training_samples/ → .jinja + .yaml training data")


if __name__ == "__main__":
    main()