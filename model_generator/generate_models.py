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
CONSTRUCTS_INVOLVED: <comma-separated list of the specific construct names that \
exhibit the antipattern>
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

═══ QUALITY RULES ════════════════════════════════════════════════════════════════
1. Version 1 MUST be a clear, unambiguous example of the "{ap['name']}" antipattern.
2. Version 2 MUST be a correct, fully refactored model with no antipattern.
3. The construct count in each version must match CONSTRUCT_COUNT exactly.
4. Both models must be realistic and domain-appropriate.
5. These models train an LLM to {task_line}. \
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

    def first_group(pattern, default=""):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else default

    domain_raw      = first_group(r"DOMAIN:\s*(\S+)", "unknown_domain")
    domain_display  = first_group(r"DOMAIN_DISPLAY:\s*(.+)", domain_raw)
    size            = first_group(r"SIZE:\s*(\S+)", "unknown")
    antipattern_det = first_group(r"ANTIPATTERN_DETECTED:\s*(.+)", "Unknown")
    constructs_inv  = first_group(r"CONSTRUCTS_INVOLVED:\s*(.+)", "Unknown")

    counts = re.findall(r"CONSTRUCT_COUNT:\s*(\d+)", text)
    count_v1 = int(counts[0]) if len(counts) > 0 else None
    count_v2 = int(counts[1]) if len(counts) > 1 else None

    def extract_puml(section: str) -> str | None:
        m = re.search(r"```plantuml\s*(.*?)```", section, re.DOTALL)
        return m.group(1).strip() if m else None

    v1_sec = re.search(r"=== VERSION 1.*?===(.*?)=== VERSION 2", text, re.DOTALL)
    v2_sec = re.search(r"=== VERSION 2.*?===(.*?)$",             text, re.DOTALL)

    return {
        "domain":            slugify(domain_raw),
        "domain_display":    domain_display,
        "size":              size.lower(),
        "antipattern_detected": antipattern_det,
        "constructs_involved":  constructs_inv,
        "construct_count_v1":   count_v1,
        "construct_count_v2":   count_v2,
        "antipattern_puml":  extract_puml(v1_sec.group(1)) if v1_sec else None,
        "refactored_puml":   extract_puml(v2_sec.group(1)) if v2_sec else None,
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
    constructs_involved: str,
    task_mode: str,
    refactored_puml: str | None = None,
) -> str:
    """Return the content of a .jinja training-sample file for the antipattern version."""
    instruction = _TASK_INSTRUCTION[task_mode]

    answer_lines = [
        f"Antipattern Detected: {antipattern_name}",
        f"Constructs Involved: {constructs_involved}",
    ]
    if task_mode == "detect-and-refactor" and refactored_puml:
        answer_lines += ["", "Refactored Model:", refactored_puml]

    answer = "\n".join(answer_lines)

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
            "construct_count":   construct_count,
            "constructs_involved": constructs_involved,
            "generated_at":      generated_at,
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

        # ── Training sample – antipattern (negative) ───────────────────────────
        ap_answer_lines = [
            f"Antipattern Detected: {parsed['antipattern_detected']}",
            f"Constructs Involved: {parsed['constructs_involved']}",
        ]
        if args.task_mode == "detect-and-refactor":
            ap_answer_lines += ["", "Refactored Model:", parsed["refactored_puml"]]
        ap_answer = "\n".join(ap_answer_lines)

        jinja_ap = write_file(
            make_jinja_antipattern(
                parsed["antipattern_puml"],
                parsed["antipattern_detected"],
                parsed["constructs_involved"],
                args.task_mode,
                parsed["refactored_puml"],
            ),
            training_dir / "antipattern" / f"prompt_{i:03d}_{domain_slug}_antipattern.jinja",
        )

        write_file(
            yaml.dump(
                make_yaml_record(
                    prompt_num=i,
                    domain=domain,
                    domain_display=domain_display,
                    size=size,
                    antipattern_name=antipattern_name,
                    constructs_involved=parsed["constructs_involved"],
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

        # ── Rate limit ─────────────────────────────────────────────────────────
        if i < args.num_prompts:
            logger.info(f"  Sleeping {args.rate_limit} s …")
            time.sleep(args.rate_limit)

    logger.info("")
    logger.info(f"{'═'*60}")
    logger.info(f"Done. All output under: {run_dir}")
    logger.info(f"  models/           → PlantUML + PNG pairs")
    logger.info(f"  training_samples/ → .jinja + .yaml training data")


if __name__ == "__main__":
    main()