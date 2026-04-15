#!/usr/bin/env python3
"""
Build obfuscated copies of selected packages for distribution.

Requires: pip install -r requirements-obfuscate.txt

Example:
  python scripts/obfuscate_release.py -O dist/obfuscated
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# High-value IP: strategy + execution; extend as needed.
DEFAULT_RECURSIVE_TARGETS = (
    "src/strategy",
    "src/execution",
)


def _pyarmor_base() -> list[str]:
    exe = shutil.which("pyarmor")
    if exe:
        return [exe]
    return [sys.executable, "-m", "pyarmor"]


def _check_pyarmor() -> None:
    try:
        import pyarmor  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "PyArmor is not available. Install with:\n"
            "  pip install -r requirements-obfuscate.txt\n"
        )
        raise SystemExit(1) from None


def main() -> None:
    ap = argparse.ArgumentParser(description="Obfuscate Shark Quant Python packages with PyArmor.")
    ap.add_argument(
        "-O",
        "--output",
        type=Path,
        default=ROOT / "dist" / "obfuscated",
        help="Output directory (default: dist/obfuscated)",
    )
    ap.add_argument(
        "extra",
        nargs="*",
        help="Additional paths to pass to pyarmor gen -r (optional)",
    )
    args = ap.parse_args()
    out: Path = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)

    _check_pyarmor()
    cmd: list[str] = [
        *_pyarmor_base(),
        "gen",
        "-O",
        str(out),
    ]
    for rel in DEFAULT_RECURSIVE_TARGETS:
        cmd.extend(["-r", str(ROOT / rel)])
    for rel in args.extra:
        p = Path(rel)
        cmd.extend(["-r", str(p if p.is_absolute() else ROOT / p)])

    print("Running:", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)
    print(f"Done. Obfuscated tree: {out}")
    print("Ship this folder plus pyarmor_runtime_* and remaining non-obfuscated src/ as needed.")


if __name__ == "__main__":
    main()
