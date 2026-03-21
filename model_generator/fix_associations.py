#!/usr/bin/env python3
"""
fix_associations.py

Replaces directed arrows (-->) with undirected associations (--)
in all PlantUML use-case files inside a run directory, then
re-renders the PNG images with PlantUML.

Actor/use-case associations must be undirected per UML standard.
Include/extend (..) and generalisation (--|>) are left untouched.

Usage:
    python fix_associations.py <run_dir> --plantuml-jar /path/to/plantuml.jar
    python fix_associations.py output/run_20260321_004201 --plantuml-jar ~/.vscode/...
"""

import argparse
import subprocess
import sys
from pathlib import Path


def fix_puml(path: Path) -> bool:
    """Replace --> with -- in a .puml file. Returns True if the file changed."""
    original = path.read_text(encoding="utf-8")
    fixed = original.replace("-->", "--")
    if fixed == original:
        return False
    path.write_text(fixed, encoding="utf-8")
    return True


def render_puml(puml_path: Path, plantuml_jar: Path) -> None:
    result = subprocess.run(
        ["java", "-jar", str(plantuml_jar), "-tpng", str(puml_path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"  [WARN] PlantUML failed for {puml_path.name}: {result.stderr.strip()}")


def main():
    p = argparse.ArgumentParser(description="Fix directed associations in PlantUML files.")
    p.add_argument("run_dir", help="Path to the run directory (contains models/)")
    p.add_argument("--plantuml-jar", required=True, metavar="JAR",
                   help="Path to plantuml.jar for re-rendering PNGs")
    p.add_argument("--dry-run", action="store_true",
                   help="Show which files would change without modifying them")
    args = p.parse_args()

    run_dir     = Path(args.run_dir)
    models_dir  = run_dir / "models"
    plantuml_jar = Path(args.plantuml_jar)

    if not models_dir.exists():
        print(f"ERROR: models/ not found in {run_dir}", file=sys.stderr)
        sys.exit(1)

    puml_files = sorted(models_dir.rglob("*.puml"))
    if not puml_files:
        print("No .puml files found.")
        return

    changed = []
    for puml in puml_files:
        if args.dry_run:
            original = puml.read_text(encoding="utf-8")
            if "-->" in original:
                count = original.count("-->")
                print(f"  would fix  {puml.relative_to(run_dir)}  ({count} arrow(s))")
                changed.append(puml)
        else:
            if fix_puml(puml):
                changed.append(puml)

    if args.dry_run:
        print(f"\nDry run: {len(changed)} file(s) would be modified.")
        return

    print(f"Fixed {len(changed)} file(s).")

    if not changed:
        print("Nothing to re-render.")
        return

    print(f"Re-rendering {len(changed)} file(s)…")
    for puml in changed:
        render_puml(puml, plantuml_jar)
        print(f"  rendered  {puml.relative_to(run_dir)}")

    print(f"\nDone. {len(changed)} diagram(s) updated.")


if __name__ == "__main__":
    main()