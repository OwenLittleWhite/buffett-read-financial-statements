---
name: buffett-read-financial-statements
description: Fetch and structure up to twenty fiscal years of Chinese A-share balance sheets, income statements, and cash-flow statements from Eastmoney for Buffett-style financial statement analysis.
---

# Buffett Financial Statement Reading

Use this skill when the user wants to collect or analyze an A-share company's three core financial statements through the lens of 《巴菲特教你读财报》.

## Data collection

Run `scripts/fetch_eastmoney_financials.py` before analysis when local data is missing or the requested range has changed.

```bash
python3 scripts/fetch_eastmoney_financials.py SH600809 --years 10
python3 scripts/fetch_eastmoney_financials.py SH600809 --years 20 --end-year 2026
python3 scripts/fetch_eastmoney_financials.py SH600809 --start-year 2016 --end-year 2025
```

- Keep the requested range at twenty fiscal years or fewer as a final safety bound; ten years remains the default when `--years` is omitted.
- Keep the script's two-second pause after every successful Eastmoney HTTP request to reduce request pressure.
- Collect `all` report periods by default so annual, first-quarter, interim, and third-quarter reports remain available. Filter by the source `REPORT_TYPE` only during analysis unless the user explicitly requests a smaller download.
- The same command must refresh `report_market_snapshots` for every selected report date. Match the latest trading day on or before the report date and the latest effective share-capital row on or before that trading day.
- Preserve the complete live share-capital history in `share_capital_changes`, including `CHANGE_REASON`, so repurchase-related total-share reductions can be distinguished from a positive treasury-share balance. Do not claim legal cancellation without checking the relevant company announcement when confirmation matters.
- Refresh `share_repurchase_programs` in the same command and keep each Eastmoney repurchase program's latest disclosed amount, shares, progress, objective, and dates. Treat a plan, actual execution, treasury shares, and a later share-capital reduction as separate evidence; program results are current disclosures rather than report-date point-in-time snapshots.
- Prefer fresh market data. If Eastmoney's market endpoint is temporarily unavailable, accept `market_source=database_cache` only when the existing database covers every requested report date; never accept a partial market-data set.
- Keep collection idempotent: repeat commands update stable business keys and stable CSV/JSON paths rather than creating duplicate records or timestamped snapshots.
- Use `financial_item_definitions` for Chinese labels and source display order when presenting a complete statement. Keep `facts` as the analytical long table instead of creating a physical statement-wide table.
- Treat Eastmoney as a secondary data service rather than the legal source of record. Preserve the raw snapshot and report dates so important figures can later be checked against exchange filings.
- Do not silently turn missing fields into zero.

Read [references/data-schema.md](references/data-schema.md) when querying the generated SQLite database or extending the storage model.

When the user supplies Buffett-style indicators or judgment rules, record them first in [references/buffett-metrics.md](references/buffett-metrics.md). Preserve the user's wording, map only confirmed source fields, and keep formulas as SQL/Python drafts until the user asks to implement or persist derived results.

## Metric comparison

Use `scripts/compare_buffett_metrics.py` to calculate the registered metrics at analysis time without adding derived columns to the database.

```bash
python3 scripts/compare_buffett_metrics.py SH600519 SZ000858 --start-year 2016 --end-year 2025
python3 scripts/compare_buffett_metrics.py SH600036 --start-year 2016 --end-year 2025 --financial-symbol SH600036
python3 scripts/compare_buffett_metrics.py SH600519 --start-year 2016 --end-year 2025 --skip-current-price
```

For a multi-company delivery, prefer the bundle command so the current quote is refreshed only once and the detailed report is never replaced by the scorecard:

```bash
python3 scripts/build_analysis_bundle.py \
  SH600519 SZ000858 SZ000596 SZ000568 SH600809 \
  --start-year 2016 --end-year 2025 \
  --output-prefix data/analysis/liquor_2016_2025
```

The bundle must retain five separate deliverables with stable suffixes:

- `_metrics.md`: the original complete indicator tables and explanations.
- `_metrics.json`: the detailed machine-readable comparison data.
- `_scorecard.html`: the colored scorecard for primary review.
- `_scorecard.md`: the icon-based scorecard fallback.
- `_scorecard.json`: scores, colors, coverage, contributions, and ranks.

Never overwrite or omit the detailed `_metrics.md` merely because a scorecard was requested. After a successful bundle run, use the printed manifest and include clickable local-file links to all five deliverables in the final response; lead with the colored HTML and the original detailed Markdown. Also link the weights and methodology when the user is reviewing or changing the scoring system.

Use `scripts/build_scorecard_report.py` directly only when rebuilding scorecard files from an already-current detailed JSON without refreshing quotes.

Read [references/scorecard-methodology.md](references/scorecard-methodology.md) before changing score rules or interpreting the composite winner. Keep all trial weights and thresholds in [references/scorecard-weights.json](references/scorecard-weights.json), show coverage and category contributions, and never force an unscored qualitative risk prompt into the total merely to reach a preferred result.

- Pass `--financial-symbol` once for each bank or other financial company. The database does not yet contain a reliable industry classification, so never infer this flag from a company name or symbol. This flag also activates financial-company loan-structure rules and disables non-financial net-margin and debt-to-equity tiers.
- Classify non-financial-company net margins as `<10%` highly competitive, `10%` inclusive to `<20%` gray zone, and `>=20%` possible relative competitive advantage.
- Return financial-company net-margin classification as not applicable. Keep an unusually high net margin as a qualitative risk prompt until the user defines a numeric threshold; do not invent an automatic cutoff.
- For cash-source analysis, use the latest seven annual cash-flow statements as the primary evidence and the balance sheet only as a cross-check. Reconcile operating, investing, financing, and exchange-rate effects to the reported cash-equivalent change; keep disclosed debt, equity, and asset/business-sale inflows as source-specific lower bounds when source rows are missing.
- For long-term investments, keep long-term equity investments, other equity instruments, non-current financial assets, and debt investments separate because their measurement bases differ. Never apply a universal lower-of-cost-or-market assumption or add the categories into a synthetic total. Read official annual-report notes for investee names and accounting methods before assessing hidden value or the investees' competitive advantages.
- For the capital-expenditure burden, require ten complete annual observations and compute `sum(CONSTRUCT_LONG_ASSET) / sum(NETPROFIT)`. Classify strictly below 25% as a strong clue, 25% inclusive to below 50% as a candidate, and 50% or above as not meeting this single criterion; return missing when the ten-year inputs are incomplete or cumulative net profit is not positive.
- For the equity-bond view, refresh Eastmoney's current quote during analysis and compute current buy-in pretax earnings yield as `latest complete annual TOTAL_PROFIT / current total shares / latest completed close`. During an unfinished trading session use the previous close; after close or on a non-trading day use the latest completed closing price. Do not substitute a historical report-date price when the current quote fails. Report the source annual-report date, price date or previous-close status, pretax-profit-per-share CAGR, latest year-over-year change, median year-over-year growth, and the ten-year trend classification together. Label the per-share figure as an estimate and keep actual executed cost basis separate from this current-price approximation. Use `--skip-current-price` only for explicitly offline analysis.
