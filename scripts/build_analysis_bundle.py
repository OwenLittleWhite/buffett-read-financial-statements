#!/usr/bin/env python3
"""Build detailed and colored scorecard reports with one market-price refresh."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "eastmoney_financials.sqlite3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="Normalized A-share symbols")
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    parser.add_argument(
        "--output-prefix",
        type=Path,
        required=True,
        help="Path prefix; suffixes _metrics and _scorecard are added",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--financial-symbol", action="append", default=[])
    parser.add_argument("--skip-current-price", action="store_true")
    parser.add_argument("--price-timeout", type=float, default=10.0)
    parser.add_argument("--price-retries", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start_year > args.end_year:
        raise SystemExit("--start-year cannot be later than --end-year")
    prefix = args.output_prefix.expanduser().resolve()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    detailed_json = Path(f"{prefix}_metrics.json")
    detailed_markdown = Path(f"{prefix}_metrics.md")
    scorecard_json = Path(f"{prefix}_scorecard.json")
    scorecard_markdown = Path(f"{prefix}_scorecard.md")
    scorecard_html = Path(f"{prefix}_scorecard.html")

    compare_command = [
        sys.executable,
        str(ROOT / "scripts" / "compare_buffett_metrics.py"),
        *args.symbols,
        "--start-year",
        str(args.start_year),
        "--end-year",
        str(args.end_year),
        "--database",
        str(args.database.expanduser().resolve()),
        "--price-timeout",
        str(args.price_timeout),
        "--price-retries",
        str(args.price_retries),
        "--format",
        "markdown",
        "--output-json",
        str(detailed_json),
        "--output-markdown",
        str(detailed_markdown),
    ]
    if args.skip_current_price:
        compare_command.append("--skip-current-price")
    for symbol in args.financial_symbol:
        compare_command.extend(("--financial-symbol", symbol))
    subprocess.run(compare_command, check=True, stdout=subprocess.DEVNULL)

    scorecard_command = [
        sys.executable,
        str(ROOT / "scripts" / "build_scorecard_report.py"),
        str(detailed_json),
        "--output-json",
        str(scorecard_json),
        "--output-markdown",
        str(scorecard_markdown),
        "--output-html",
        str(scorecard_html),
    ]
    subprocess.run(scorecard_command, check=True)

    manifest = {
        "detailed_report_markdown": str(detailed_markdown),
        "detailed_data_json": str(detailed_json),
        "colored_scorecard_html": str(scorecard_html),
        "scorecard_markdown": str(scorecard_markdown),
        "scorecard_data_json": str(scorecard_json),
        "weights": str(ROOT / "references" / "scorecard-weights.json"),
        "methodology": str(ROOT / "references" / "scorecard-methodology.md"),
    }
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
