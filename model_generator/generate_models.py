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
import json

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

# ── Antipattern instance caps by size ─────────────────────────────────────────

MAX_INSTANCES_BY_SIZE = {
    "small":  1,
    "medium": 3,
    "large":  5,
}

# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Generate UML use case model pairs (antipattern + refactored) "
                    "for antipattern detection research and LLM fine-tuning."
    )
    p.add_argument("--config",        required=True,
                   help="Path to YAML antipattern/refactoring config file.")
    p.add_argument("--domains-config", default=None,
                   help="Path to YAML domains file (domains.yaml). "
                        "When provided, domains are assigned sequentially from the list. "
                        "When omitted, Claude selects domains freely.")
    p.add_argument("--plantuml-jar",  required=True,
                   help="Path to plantuml.jar for image conversion.")
    p.add_argument("--png", action="store_true",
                   help="Output images as PNG instead of the default JPEG.")
    p.add_argument("--output-dir",    default=None,
                   help="Root output directory (default: ./output next to this script).")
    p.add_argument("--num-prompts",   type=int, default=10,
                   help="Number of model pairs to generate (default: 10).")
    p.add_argument("--sizes", nargs="+", choices=["small", "medium", "large"],
                   default=["small"],
                   help="Which model sizes to generate (default: small). "
                        "Allowed values: small medium large. "
                        "Example: --sizes small medium")
    p.add_argument("--size-weights", nargs="+", type=float, default=None,
                   help="Sampling weights for each size in --sizes (default: equal). "
                        "Must have the same number of values as --sizes. "
                        "Values are relative (need not sum to 1). "
                        "Example: --sizes small medium --size-weights 0.7 0.3")
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


def load_domains(path: str) -> list[dict]:
    """Load the ordered domain list from a domains YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["domains"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert arbitrary text to snake_case with no spaces."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def determine_sizes(n: int, allowed_sizes: list[str], weights: list[float] | None = None) -> list[str]:
    """Return a shuffled list of n size labels drawn from allowed_sizes.

    weights: relative sampling weights (same length as allowed_sizes).
             If None, all sizes are equally likely.
    """
    sizes = random.choices(allowed_sizes, weights=weights, k=n)
    random.shuffle(sizes)
    return sizes


def assign_antipatterns_for_prompt(
    all_antipatterns: list[dict],
    size: str,
    usage_counts: dict[str, int],
) -> list[dict]:
    """Select antipatterns and instance counts for one prompt.

    Returns a list of {"antipattern": {...}, "instance_count": int}.
    Prefers antipattern types with lower usage counts to keep balance across the run.
    """
    max_instances = MAX_INSTANCES_BY_SIZE[size]
    n_available   = len(all_antipatterns)

    # Number of distinct antipattern types to embed
    if size == "small":
        n_types = 1
    elif size == "medium":
        n_types = random.randint(1, min(2, n_available))
    else:  # large
        n_types = random.randint(1, min(3, n_available))

    # Can't have more types than the instance budget allows
    n_types = min(n_types, max_instances)

    # Sort by usage count ascending; random.random() breaks ties
    ranked = sorted(
        range(n_available),
        key=lambda idx: (usage_counts.get(all_antipatterns[idx]["name"], 0), random.random()),
    )
    selected = [all_antipatterns[idx] for idx in ranked[:n_types]]

    # Give each selected type at least 1 instance, then distribute remainder randomly
    counts    = [1] * n_types
    remaining = max_instances - n_types
    for _ in range(remaining):
        counts[random.randrange(n_types)] += 1

    return [{"antipattern": ap, "instance_count": c} for ap, c in zip(selected, counts)]


def write_file(content: str, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.info(f"    Wrote  {path}")
    return path


def convert_to_image(puml_path: Path, jar_path: str, use_png: bool = False) -> Path:
    """Run PlantUML to generate a JPEG (default) or PNG beside the .puml file."""
    fmt = "png" if use_png else "jpg"
    img_path = puml_path.with_suffix(f".{fmt}")
    try:
        proc = subprocess.run(
            ["java", "-jar", str(jar_path), f"-t{fmt}", str(puml_path)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            logger.warning(f"    PlantUML warning: {proc.stderr.strip()[:200]}")
        else:
            logger.info(f"    {fmt.upper():<4} {img_path}")
    except FileNotFoundError:
        logger.error("    'java' not found on PATH. Image conversion skipped.")
    except subprocess.TimeoutExpired:
        logger.error(f"    PlantUML timed out for {puml_path}")
    except Exception as exc:
        logger.error(f"    Image conversion failed: {exc}")
    return img_path


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

def build_system_prompt(assigned_antipatterns: list[dict], task_mode: str) -> str:
    """Build the system prompt for one prompt turn.

    assigned_antipatterns: list of {"antipattern": {...}, "instance_count": int}
    """
    task_line = (
        "detect antipatterns — name each one and list the exact constructs involved"
        if task_mode == "detect"
        else "detect antipatterns (name each one and list the exact constructs involved) "
             "AND provide the full refactored PlantUML model"
    )

    # ── Antipattern + refactoring sections ────────────────────────────────────
    ap_section_lines = []
    for entry in assigned_antipatterns:
        ap = entry["antipattern"]
        n  = entry["instance_count"]
        ap_section_lines += [
            f"─── Antipattern: {ap['name']} ({'%d instance' % n if n == 1 else '%d instances' % n}) ───",
            f"Description : {ap['description'].strip()}",
            "",
            "Refactoring strategy:",
        ]
        for rf in ap["refactorings"]:
            ap_section_lines += [
                f"  Name        : {rf['name']}",
                f"  Description : {rf['description'].strip()}",
            ]
        ap_section_lines.append("")
    ap_section = "\n".join(ap_section_lines).rstrip()

    # ── VERSION 1 response template (dynamic per assignment) ──────────────────
    v1_lines = []
    for k, entry in enumerate(assigned_antipatterns, start=1):
        ap = entry["antipattern"]
        n  = entry["instance_count"]
        v1_lines.append(f"[ANTIPATTERN {k}]")
        v1_lines.append(f"ANTIPATTERN_DETECTED: {ap['name']}")
        v1_lines.append(f"INSTANCE_COUNT: {n}")
        v1_lines.append("")
        for inst_num in range(1, n + 1):
            v1_lines.append(f"[INSTANCE {inst_num}]")
            v1_lines.append("CONSTRUCTS_INVOLVED: <comma-separated names of the constructs forming this instance>")
            v1_lines.append(
                f"EXPLANATION: <clear, specific explanation of why exactly these constructs constitute "
                f"the \"{ap['name']}\" antipattern — reference each construct by name>"
            )
            v1_lines.append("")
    v1_template = "\n".join(v1_lines).rstrip()

    # All refactoring names for the Version 2 comment
    rf_names = ", ".join(
        rf["name"]
        for entry in assigned_antipatterns
        for rf in entry["antipattern"]["refactorings"]
    )

    # All antipattern names for quality rule
    ap_names_list = "; ".join(e["antipattern"]["name"] for e in assigned_antipatterns)

    return f"""\
You are an expert UML use case modeling consultant specialising in antipattern \
detection and refactoring for software engineering research.

Your task: for each request, generate exactly TWO versions of a PlantUML use case \
model for the specified domain and construct count.

═══ ANTIPATTERNS TO EMBED ═══════════════════════════════════════════════════════
{ap_section}

═══ WHAT COUNTS AS A CONSTRUCT ══════════════════════════════════════════════════
  • Actor          – each actor node
  • Use Case       – each use case ellipse
  • Include        – each <<include>> dependency arrow
  • Extend         – each <<extend>> dependency arrow
  • Generalize     – each generalisation (inheritance) arrow

  TOTAL constructs = Actors + Use Cases + Includes + Extends + Generalisations.

═══ DOMAIN RULE ═════════════════════════════════════════════════════════════════
Use the exact domain specified in the user message. Do not substitute a different
domain. Set DOMAIN_DISPLAY to the domain name as given.

═══ MANDATORY RESPONSE FORMAT ═══════════════════════════════════════════════════
Follow this template EXACTLY — including the marker lines.

DOMAIN: <snake_case_domain>
DOMAIN_DISPLAY: <Human Readable Domain Name>
SIZE: <small|medium|large>

=== VERSION 1: WITH ANTIPATTERNS ===
{v1_template}

REFACTORING_RATIONALE: <what must change and why — address every antipattern instance \
explicitly, naming the constructs to be restructured and the goal of the refactoring>
CONSTRUCT_COUNT: <integer>

```plantuml
@startuml
' Version 1 – contains {ap_names_list}
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
  • Use a `rectangle "System Name" {{{{ }}}}` block to represent the system context.
  • ALL use cases MUST be placed INSIDE the rectangle.
  • ALL actors MUST be placed OUTSIDE the rectangle.
  • The rectangle label should be the name of the system being modelled.

Example structure:
  actor Customer
  rectangle "Online Store" {{{{
    usecase "Browse Products" as UC1
    usecase "Place Order" as UC2
  }}}}
  Customer --> UC1
  Customer --> UC2

═══ QUALITY RULES ════════════════════════��═══════════════════════════════════════
1. Version 1 MUST contain clear, unambiguous instances of every assigned antipattern.
2. Version 2 MUST be a correct, fully refactored model with no antipattern.
3. The construct count in each version must match CONSTRUCT_COUNT exactly.
4. Both models must be realistic and domain-appropriate.
5. Every model MUST have a system boundary rectangle with actors outside and use cases inside.
6. These models train an LLM to {task_line}. \
Make them educationally clear examples.
"""


def build_user_message(
    prompt_num: int, size: str, construct_count: int, domain_name: str | None = None
) -> str:
    lo, hi = SIZE_RANGES[size]
    domain_clause = (
        f"Domain: {domain_name}."
        if domain_name
        else "Select a NEW domain not yet used in this conversation."
    )
    return (
        f"Prompt #{prompt_num:03d}: Generate a {size.upper()} use case model "
        f"with exactly {construct_count} constructs "
        f"(valid range for {size}: {lo}–{hi} constructs). "
        f"{domain_clause}"
    )


# ── Response parser ───────────────────────────────────────────────────────────

def parse_response(text: str) -> dict:
    """Extract all structured fields from Claude's formatted response."""

    def first_group(pattern, src=text, default=""):
        m = re.search(pattern, src)
        return m.group(1).strip() if m else default

    domain_raw     = first_group(r"DOMAIN:\s*(\S+)", default="unknown_domain")
    domain_display = first_group(r"DOMAIN_DISPLAY:\s*(.+)", default=domain_raw)
    size           = first_group(r"SIZE:\s*(\S+)", default="unknown")

    counts   = re.findall(r"CONSTRUCT_COUNT:\s*(\d+)", text)
    count_v1 = int(counts[0]) if len(counts) > 0 else None
    count_v2 = int(counts[1]) if len(counts) > 1 else None

    def extract_puml(section: str) -> str | None:
        m = re.search(r"```plantuml\s*(.*?)```", section, re.DOTALL)
        return m.group(1).strip() if m else None

    v1_sec = re.search(r"=== VERSION 1.*?===(.*?)=== VERSION 2", text, re.DOTALL)
    v2_sec = re.search(r"=== VERSION 2.*?===(.*?)$",             text, re.DOTALL)
    v1_text = v1_sec.group(1) if v1_sec else ""

    # ── Per-instance parsing ────────���──────────────────────────────────────────
    def parse_instances(section: str) -> list[dict]:
        """Return [{constructs_involved, explanation}, …], one dict per instance."""
        blocks = re.split(r"\[INSTANCE\s+\d+\]", section, flags=re.IGNORECASE)
        instances = []
        for block in blocks[1:]:
            constructs  = re.search(r"CONSTRUCTS_INVOLVED:\s*(.+)", block)
            explanation = re.search(
                r"EXPLANATION:\s*(.*?)(?=\n[A-Z_]+\s*:|$)", block, re.DOTALL
            )
            instances.append({
                "constructs_involved": constructs.group(1).strip() if constructs else "",
                "explanation":         explanation.group(1).strip() if explanation else "",
            })
        # Fallback for single instance without markers
        if not instances:
            constructs  = re.search(r"CONSTRUCTS_INVOLVED:\s*(.+)", section)
            explanation = re.search(
                r"EXPLANATION:\s*(.*?)(?=\n[A-Z_]+\s*:|$)", section, re.DOTALL
            )
            instances.append({
                "constructs_involved": constructs.group(1).strip() if constructs else "Unknown",
                "explanation":         explanation.group(1).strip() if explanation else "",
            })
        return instances

    # ── Parse multiple [ANTIPATTERN k] blocks ─────────────────────────────────
    ap_blocks = re.split(r"\[ANTIPATTERN\s+\d+\]", v1_text, flags=re.IGNORECASE)
    antipatterns_detected: list[dict] = []
    for block in ap_blocks[1:]:
        name_m = re.search(r"ANTIPATTERN_DETECTED:\s*(.+)", block)
        name   = name_m.group(1).strip() if name_m else "Unknown"
        antipatterns_detected.append({
            "name":      name,
            "instances": parse_instances(block),
        })

    # Fallback: no [ANTIPATTERN k] markers — treat the whole v1 section as one antipattern
    if not antipatterns_detected:
        name_m = re.search(r"ANTIPATTERN_DETECTED:\s*(.+)", v1_text)
        name   = name_m.group(1).strip() if name_m else "Unknown"
        antipatterns_detected.append({
            "name":      name,
            "instances": parse_instances(v1_text),
        })

    refactoring_rationale = first_group(
        r"REFACTORING_RATIONALE:\s*(.*?)(?=CONSTRUCT_COUNT|```|$)",
        src=v1_text, default="",
    )

    # Flat summary strings for logging / CSV
    antipattern_names          = "; ".join(ap["name"] for ap in antipatterns_detected)
    antipattern_instance_counts = "; ".join(str(len(ap["instances"])) for ap in antipatterns_detected)
    total_antipattern_instances = sum(len(ap["instances"]) for ap in antipatterns_detected)

    return {
        "domain":                       slugify(domain_raw),
        "domain_display":               domain_display,
        "size":                         size.lower(),
        "antipatterns_detected":        antipatterns_detected,
        "antipattern_names":            antipattern_names,
        "antipattern_instance_counts":  antipattern_instance_counts,
        "total_antipattern_instances":  total_antipattern_instances,
        "refactoring_rationale":        refactoring_rationale,
        "construct_count_v1":           count_v1,
        "construct_count_v2":           count_v2,
        "antipattern_puml":             extract_puml(v1_sec.group(1)) if v1_sec else None,
        "refactored_puml":              extract_puml(v2_sec.group(1)) if v2_sec else None,
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
    antipatterns_detected: list[dict],
    refactoring_rationale: str,
    task_mode: str,
    refactored_puml: str | None = None,
) -> str:
    """Return the content of a .jinja training-sample file for the antipattern version."""
    instruction = _TASK_INSTRUCTION[task_mode]

    answer_lines: list[str] = []
    for ap_idx, ap in enumerate(antipatterns_detected, start=1):
        prefix = f"{ap_idx}. " if len(antipatterns_detected) > 1 else ""
        answer_lines.append(f"{prefix}Antipattern Detected: {ap['name']}")
        answer_lines.append("")
        for inst_idx, inst in enumerate(ap["instances"], start=1):
            inst_prefix = f"   Instance {inst_idx}: " if len(ap["instances"]) > 1 else "   "
            answer_lines += [
                f"{inst_prefix}Constructs Involved: {inst['constructs_involved']}",
                f"{inst_prefix}Explanation: {inst['explanation']}",
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
    antipatterns_detected: list[dict],
    refactoring_rationale: str | None,
    construct_count: int | None,
    puml: str,
    expected_output: str,
    sample_type: str,           # "antipattern" | "refactored"
    task_mode: str,
    puml_path: Path,
    img_path: Path,
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
            "prompt_number":          prompt_num,
            "domain":                 domain,
            "domain_display":         domain_display,
            "size":                   size,
            "antipatterns_detected":  antipatterns_detected,
            "sample_type":            sample_type,
            "task_mode":              task_mode,
            "construct_count":        construct_count,
            "refactoring_rationale":  refactoring_rationale or "",
            "generated_at":           generated_at,
            "files": {
                "plantuml": str(puml_path),
                "image":    str(img_path),
                "jinja":    str(jinja_path),
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

    actors          = sum(1 for l in lines if re.match(r"^\s*actor\b", l, re.IGNORECASE))
    use_cases       = sum(1 for l in lines if re.match(r"^\s*usecase\b", l, re.IGNORECASE)
                          or re.match(r"^\s*\(", l))
    includes        = sum(1 for l in lines if re.search(r"<<include>>", l, re.IGNORECASE)
                          or re.search(r"\.include\b", l, re.IGNORECASE))
    extends         = sum(1 for l in lines if re.search(r"<<extend>>", l, re.IGNORECASE)
                          or re.search(r"\.extend\b", l, re.IGNORECASE))
    generalizations = sum(1 for l in lines if re.search(r"<\|--|--\|>", l))
    has_boundary    = any(re.match(r"^\s*rectangle\b", l, re.IGNORECASE) for l in lines)
    total_parsed    = actors + use_cases + includes + extends + generalizations

    return {
        "actors":              actors,
        "use_cases":           use_cases,
        "includes":            includes,
        "extends":             extends,
        "generalizations":     generalizations,
        "total_parsed":        total_parsed,
        "has_system_boundary": has_boundary,
    }


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"    Wrote  {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    cfg              = load_config(args.config)
    all_antipatterns = cfg["antipatterns"]
    ap_code_lookup   = {ap["name"]: ap.get("code", ap["name"]) for ap in all_antipatterns}

    domain_pool: list[dict] | None = None
    if args.domains_config:
        domain_pool = load_domains(args.domains_config)
        logger.info(f"Domains      : {len(domain_pool)} loaded from {args.domains_config}")

    # Validate size weights
    if args.size_weights is not None:
        if len(args.size_weights) != len(args.sizes):
            logger.error(
                f"--size-weights has {len(args.size_weights)} value(s) "
                f"but --sizes has {len(args.sizes)}; they must match."
            )
            sys.exit(1)
        if any(w < 0 for w in args.size_weights):
            logger.error("--size-weights values must be non-negative.")
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
    logger.info(f"Antipatterns : {', '.join(ap['name'] for ap in all_antipatterns)}")
    logger.info(f"Task mode    : {args.task_mode}")
    logger.info(f"Num prompts  : {args.num_prompts}")
    if args.size_weights:
        size_dist = "  ".join(f"{s}={w}" for s, w in zip(args.sizes, args.size_weights))
        logger.info(f"Sizes        : {size_dist} (weighted)")
    else:
        logger.info(f"Sizes        : {', '.join(args.sizes)} (equal weight)")

    # Anthropic client
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("No API key. Set ANTHROPIC_API_KEY or use --api-key.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Prepare conversation
    sizes = determine_sizes(args.num_prompts, args.sizes, args.size_weights)

    # Tracks how many times each antipattern type has been assigned (for balance)
    ap_usage_counts: dict[str, int] = {ap["name"]: 0 for ap in all_antipatterns}

    # ── Main generation loop ───────────────────────────────────────────────────
    all_domains:   list[str]  = []
    ap_stats_rows:  list[dict] = []
    ref_stats_rows: list[dict] = []
    training_rows:  list[dict] = []

    for i, size in enumerate(sizes, start=1):
        lo, hi          = SIZE_RANGES[size]
        construct_count = random.randint(lo, hi)

        # Select antipatterns for this prompt and build system prompt
        assigned      = assign_antipatterns_for_prompt(all_antipatterns, size, ap_usage_counts)
        system_prompt = build_system_prompt(assigned, args.task_mode)

        assigned_summary = ", ".join(
            f"{e['antipattern']['name']} ×{e['instance_count']}" for e in assigned
        )

        domain_name: str | None = None
        if domain_pool:
            domain_entry = domain_pool[(i - 1) % len(domain_pool)]
            domain_name  = domain_entry["name"]

        user_msg = build_user_message(i, size, construct_count, domain_name)
        messages = [{"role": "user", "content": user_msg}]

        logger.info("")
        logger.info(f"{'─'*60}")
        logger.info(f"Prompt {i}/{args.num_prompts}  size={size}  target_constructs={construct_count}")
        logger.info(f"  Antipatterns : {assigned_summary}")

        # ── API call ───────────────────────────────────────────────────────────
        _model      = "claude-opus-4-6"
        _max_tokens = 4096
        logger.debug(f"  API request  model={_model}  max_tokens={_max_tokens}")
        try:
            response = client.messages.create(
                model=_model,
                max_tokens=_max_tokens,
                system=system_prompt,
                messages=messages,
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
                    messages=messages,
                )
                raw = response.content[0].text
            except Exception as exc:
                logger.error(f"  Retry failed: {exc}. Skipping prompt {i}.")
                continue
        except Exception as exc:
            logger.error(f"  API error: {exc}. Skipping prompt {i}.")
            time.sleep(args.rate_limit)
            continue

        usage = vars(response.usage) if hasattr(response, "usage") else None
        if usage:
            logger.debug(
                f"  API response input_tokens={usage.get('input_tokens')}  "
                f"output_tokens={usage.get('output_tokens')}"
            )

        write_audit(run_dir, i, system_prompt, messages, raw, _model, usage)

        # Update usage counts now that this prompt succeeded
        for entry in assigned:
            ap_usage_counts[entry["antipattern"]["name"]] += 1

        # ── Parse ──────────────────────────────────────────────────────────────
        parsed         = parse_response(raw)
        domain         = parsed["domain"] or f"prompt_{i:03d}"
        domain_display = parsed["domain_display"]
        gen_at         = datetime.now().isoformat()

        all_domains.append(domain_display)
        logger.info(f"  Domain       : {domain_display}")
        logger.info(f"  V1 constructs: {parsed['construct_count_v1']}")
        logger.info(f"  V2 constructs: {parsed['construct_count_v2']}")
        logger.debug(f"  Antipatterns : {parsed['antipattern_names']}")
        logger.debug(f"  Instances    : {parsed['antipattern_instance_counts']}")
        logger.debug(f"  V1 PUML parsed: {parsed['antipattern_puml'] is not None}")
        logger.debug(f"  V2 PUML parsed: {parsed['refactored_puml'] is not None}")

        if not parsed["antipattern_puml"] or not parsed["refactored_puml"]:
            logger.warning("  Could not parse both PlantUML blocks — skipping file output for this prompt.")
            time.sleep(args.rate_limit)
            continue

        # ── Directory & file names ─────────────────────────────────────────────
        dir_name    = f"prompt_{i:03d}_{slugify(domain)}"
        prompt_dir  = models_dir / dir_name
        domain_slug = slugify(domain)

        # ── Raw response ───────────────────────────────────────────────────────
        write_file(raw, prompt_dir / f"prompt_{i:03d}_response.txt")

        # ── PlantUML files ─────────────────────────────────────────────────────
        v1_puml = write_file(parsed["antipattern_puml"], prompt_dir / f"{domain_slug}_antipattern.puml")
        v2_puml = write_file(parsed["refactored_puml"],  prompt_dir / f"{domain_slug}_refactored.puml")

        v1_img = convert_to_image(v1_puml, args.plantuml_jar, use_png=args.png)
        v2_img = convert_to_image(v2_puml, args.plantuml_jar, use_png=args.png)

        # ── Descriptive statistics ─────────────────────────────────────────────
        v1_stats = parse_puml_stats(parsed["antipattern_puml"])
        v2_stats = parse_puml_stats(parsed["refactored_puml"])

        antipattern_codes = "; ".join(
            ap_code_lookup.get(ap["name"], ap["name"])
            for ap in parsed["antipatterns_detected"]
        )
        _base = dict(
            prompt_num=i,
            domain=domain,
            domain_display=domain_display,
            size=size,
            antipattern_codes=antipattern_codes,
            antipattern_instance_counts=parsed["antipattern_instance_counts"],
            total_antipattern_instances=parsed["total_antipattern_instances"],
            task_mode=args.task_mode,
            generated_at=gen_at,
        )
        ap_stats_rows.append({
            **_base,
            "sample_type":              "antipattern",
            "construct_count_reported": parsed["construct_count_v1"],
            **v1_stats,
            "count_matches_reported":   parsed["construct_count_v1"] == v1_stats["total_parsed"],
            "puml_file":                str(v1_puml),
            "img_file":                 str(v1_img),
        })
        ref_stats_rows.append({
            **_base,
            "sample_type":              "refactored",
            "construct_count_reported": parsed["construct_count_v2"],
            **v2_stats,
            "count_matches_reported":   parsed["construct_count_v2"] == v2_stats["total_parsed"],
            "puml_file":                str(v2_puml),
            "img_file":                 str(v2_img),
        })

        # ── Training sample – antipattern (negative) ───────────────────────────
        jinja_ap = write_file(
            make_jinja_antipattern(
                parsed["antipattern_puml"],
                parsed["antipatterns_detected"],
                parsed["refactoring_rationale"],
                args.task_mode,
                parsed["refactored_puml"],
            ),
            training_dir / "antipattern" / f"prompt_{i:03d}_{domain_slug}_antipattern.jinja",
        )

        ap_answer = make_jinja_antipattern(
            parsed["antipattern_puml"],
            parsed["antipatterns_detected"],
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
                    antipatterns_detected=parsed["antipatterns_detected"],
                    refactoring_rationale=parsed["refactoring_rationale"],
                    construct_count=parsed["construct_count_v1"],
                    puml=parsed["antipattern_puml"],
                    expected_output=ap_answer,
                    sample_type="antipattern",
                    task_mode=args.task_mode,
                    puml_path=v1_puml,
                    img_path=v1_img,
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
            "sample_id":                f"p{i:03d}_antipattern",
            "prompt_num":               i,
            "domain_display":           domain_display,
            "size":                     size,
            "antipattern_names":        parsed["antipattern_names"],
            "antipattern_instance_counts": parsed["antipattern_instance_counts"],
            "total_antipattern_instances": parsed["total_antipattern_instances"],
            "sample_type":              "antipattern",
            "task_mode":                args.task_mode,
            "generated_at":             gen_at,
            "input":                    f"{_task_instr}\n\nPlantUML Model:\n{parsed['antipattern_puml']}",
            "output":                   ap_answer,
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
                    antipatterns_detected=[],
                    refactoring_rationale=None,
                    construct_count=parsed["construct_count_v2"],
                    puml=parsed["refactored_puml"],
                    expected_output=rf_answer,
                    sample_type="refactored",
                    task_mode=args.task_mode,
                    puml_path=v2_puml,
                    img_path=v2_img,
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
            "sample_id":                f"p{i:03d}_refactored",
            "prompt_num":               i,
            "domain_display":           domain_display,
            "size":                     size,
            "antipattern_names":        "",
            "antipattern_instance_counts": "",
            "total_antipattern_instances": 0,
            "sample_type":              "refactored",
            "task_mode":                args.task_mode,
            "generated_at":             gen_at,
            "input":                    f"{_task_instr}\n\nPlantUML Model:\n{parsed['refactored_puml']}",
            "output":                   rf_answer,
        })

        # ── Rate limit ─────────────────────────────────────────────────────────
        if i < args.num_prompts:
            logger.info(f"  Sleeping {args.rate_limit} s …")
            time.sleep(args.rate_limit)

    size_counts = {s: sizes.count(s) for s in args.sizes}
    logger.info("Size distribution: " + "  ".join(f"{s}={c}" for s, c in size_counts.items()))

    # ── CSV statistics output ──────────────────────────────────────────────────
    _csv_fields = [
        "prompt_num", "domain_display", "size",
        "antipattern_codes", "antipattern_instance_counts", "total_antipattern_instances",
        "sample_type", "task_mode",
        "construct_count_reported",
        "actors", "use_cases", "includes", "extends", "generalizations",
        "total_parsed", "count_matches_reported", "has_system_boundary",
    ]
    write_csv(run_dir / "stats_antipattern.csv", ap_stats_rows,  _csv_fields)
    write_csv(run_dir / "stats_refactored.csv",  ref_stats_rows, _csv_fields)
    interleaved = [row for pair in zip(ap_stats_rows, ref_stats_rows) for row in pair]
    interleaved += ap_stats_rows[len(ref_stats_rows):] + ref_stats_rows[len(ap_stats_rows):]
    write_csv(run_dir / "stats_combined.csv",    interleaved, _csv_fields)

    _training_fields = [
        "sample_id", "prompt_num", "domain_display", "size",
        "antipattern_names", "antipattern_instance_counts", "total_antipattern_instances",
        "sample_type", "task_mode", "generated_at",
        "input", "output",
    ]
    training_yaml_rows = [{k: row[k] for k in _training_fields if k in row} for row in training_rows]
    training_yaml_path = run_dir / "training_samples.yaml"
    training_yaml_path.write_text(
        yaml.dump(training_yaml_rows, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    logger.info(f"    Wrote  {training_yaml_path}")

    training_jsonl_path = run_dir / "training_samples.jsonl"
    with open(training_jsonl_path, "w", encoding="utf-8") as f:
        for row in training_yaml_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info(f"    Wrote  {training_jsonl_path}")

    # ── Antipattern usage summary ──────────────────────────────────────────────
    logger.info("")
    logger.info("Antipattern usage across run:")
    for name, count in ap_usage_counts.items():
        logger.info(f"  {count:3d}×  {name}")

    # ── Domain summary ─────────────────────────────────────────────────────────
    unique_domains = list(dict.fromkeys(all_domains))
    domains_path   = run_dir / "domains.yaml"
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
    logger.info(f"  models/           → PlantUML + image pairs")
    logger.info(f"  training_samples/ → .jinja + .yaml training data")


if __name__ == "__main__":
    main()