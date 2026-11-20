#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify the repository's SPDX/License hygiene.

Checks:
  1. LICENSE carries the AGPL-3.0 text.
  2. Every tracked Python file has both SPDX header lines within its first five
     lines, and a shebang (when present) stays on line 1.
  3. The README names the license.

    python scripts/check_spdx.py           # human summary
    python scripts/check_spdx.py --json    # machine-readable

Exit code 0 when clean, 1 otherwise. Runs in CI.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COPYRIGHT = "SPDX-FileCopyrightText:"
LICENSE_ID = "SPDX-License-Identifier: AGPL-3.0-or-later"
SKIP_DIRS = {".git", "__pycache__", "outputs", "assets", ".venv", "node_modules", ".pytest_cache"}


def _python_files() -> list:
    try:
        out = subprocess.run(
            ["git", "-C", ROOT, "ls-files", "*.py"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        files = [os.path.join(ROOT, line) for line in out.splitlines() if line]
        if files:
            return sorted(files)
    except Exception:
        pass
    found = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if name.endswith(".py"):
                found.append(os.path.join(base, name))
    return sorted(found)


def check_license() -> dict:
    path = os.path.join(ROOT, "LICENSE")
    if not os.path.exists(path):
        return {"ok": False, "error": "LICENSE missing"}
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    ok = (
        "GNU AFFERO GENERAL PUBLIC LICENSE" in text
        and "Version 3, 19 November 2007" in text
        and "TERMS AND CONDITIONS" in text
    )
    return {"ok": ok, "bytes": len(text), "spdx": "AGPL-3.0-or-later" if ok else None}


def check_file(path: str) -> list:
    problems = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()
    if not lines:
        return ["empty file"]
    shebang = next((i for i, line in enumerate(lines[:6]) if line.startswith("#!")), None)
    if shebang is not None and shebang > 0:
        problems.append("header precedes shebang")
    head = lines[1:6] if shebang == 0 else lines[:5]
    if not any(COPYRIGHT in line for line in head):
        problems.append("missing SPDX-FileCopyrightText")
    if not any(LICENSE_ID in line for line in head):
        problems.append("missing/mismatched SPDX-License-Identifier")
    return problems


def check_readme() -> dict:
    path = os.path.join(ROOT, "README.md")
    if not os.path.exists(path):
        return {"ok": False, "error": "README.md missing"}
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    return {"ok": "AGPL-3.0" in text, "mentions_license": "AGPL-3.0" in text}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="verify SPDX headers and AGPL licensing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    files = _python_files()
    failures = {f: check_file(f) for f in files}
    failures = {k: v for k, v in failures.items() if v}

    report = {
        "license": check_license(),
        "readme": check_readme(),
        "python_files": len(files),
        "files_missing_headers": failures,
        "ok": False,
    }
    report["ok"] = report["license"].get("ok", False) and report["readme"].get("ok", False) and not failures

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        lic = report["license"]
        print(
            f"LICENSE      {'ok' if lic.get('ok') else 'FAIL'} "
            f"({lic.get('spdx') or lic.get('error')}, {lic.get('bytes', 0)} bytes)"
        )
        print(f"README       {'ok' if report['readme'].get('ok') else 'FAIL'} (mentions AGPL-3.0)")
        print(f"Python files {report['python_files']} checked")
        if failures:
            print("\nMissing SPDX headers:")
            for path, problems in failures.items():
                rel = os.path.relpath(path, ROOT)
                print(f"  {rel}: {', '.join(problems)}")
        print(f"\nResult: {'PASS' if report['ok'] else 'FAIL'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
