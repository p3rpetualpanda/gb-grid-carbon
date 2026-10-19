"""Copy the latest backtest numbers from docs/backtest_results.json into README.md and
docs/showcase.md.  Run after `python analysis/backtest.py`:  python scripts/update_backtest_docs.py

Each number lives between inline markers, e.g. `<!-- bt:model_mae -->26.8<!-- /bt -->`, so it
renders as plain text on GitHub. `--check` exits 1 if a file is out of date (used by the tests).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "backtest_results.json"
FILES = [ROOT / "README.md", ROOT / "docs" / "showcase.md"]
MARKER = re.compile(r"<!-- bt:(\w+) -->(.*?)<!-- /bt -->")


def values(results: dict) -> dict[str, str]:
    """Display strings for every marker key."""
    return {
        "run_date": results["run_date"],
        "model_mae": f"{results['model_mae']:.1f}",
        "best_baseline_mae": f"{results['best_baseline_mae']:.1f}",
        "neso_mae": f"{results['neso_mae']:.1f}",
        "previous_model_mae": f"{results['previous_model_mae']:.1f}",
        "v3_model_mae": f"{results['v3_model_mae']:.1f}",
        "improvement": f"{results['improvement']:.0%}",
    }


def render(text: str, vals: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in vals:
            raise KeyError(f"unknown backtest marker {m.group(1)!r}")
        return f"<!-- bt:{m.group(1)} -->{vals[m.group(1)]}<!-- /bt -->"

    return MARKER.sub(sub, text)


def main(check: bool = False) -> int:
    vals = values(json.loads(RESULTS.read_text(encoding="utf-8")))
    stale = []
    for path in FILES:
        old = path.read_text(encoding="utf-8")
        new = render(old, vals)
        if new != old:
            stale.append(path.relative_to(ROOT))
            if not check:
                path.write_text(new, encoding="utf-8")
    verb = "out of date" if check else "updated"
    print(f"{verb}: {', '.join(map(str, stale))}" if stale else "backtest numbers already current")
    return 1 if check and stale else 0


if __name__ == "__main__":
    sys.exit(main(check="--check" in sys.argv[1:]))
