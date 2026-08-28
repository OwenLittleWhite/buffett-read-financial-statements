# Eastmoney financial data schema

The fetcher stores ten fiscal years by default and permits up to twenty fiscal years as a final safety bound. A single command collects all available report periods, Eastmoney's statement display metadata, daily market data aligned to each report date, and effective share-capital records.

## Output files

```text
data/
|-- eastmoney_financials.sqlite3
|-- csv/
|   |-- SH600809_2016_2025_all_balance.csv
|   |-- SH600809_2016_2025_all_income.csv
|   |-- SH600809_2016_2025_all_cashflow.csv
|   |-- SH600809_2016_2025_all_market.csv
|   |-- SH600809_2016_2025_all_capital_changes.csv
|   `-- SH600809_2016_2025_all_repurchases.csv
`-- raw/
    `-- SH600809_2016_2025_all.json
```

CSV and raw JSON filenames are stable for the same symbol, fiscal range, and period filter. Repeating the same command replaces those files.

## Relationships

```text
companies (symbol)
  |-- reports (symbol, statement, report_date)
  |     `-- facts (symbol, statement, report_date, item_code)
  |-- report_market_snapshots (symbol, report_date)
  |-- share_capital_changes (symbol, effective_date)
  |-- share_repurchase_programs (symbol, repurchase_code)
  `-- fetch_runs (run_id)

financial_item_definitions (company_type, statement, item_code)
```

`statement` has exactly three values:

| Value | Statement |
|---|---|
| `balance` | 资产负债表 |
| `income` | 利润表 |
| `cashflow` | 现金流量表 |

## Core tables

### `companies`

One row per normalized A-share symbol.

| Column | Meaning |
|---|---|
| `symbol` | Primary key, for example `SH600809` |
| `security_code` | Six-digit security code |
| `market` | `SH`, `SZ`, or `BJ` |
| `security_name` | Eastmoney short name |
| `company_type` | Eastmoney company-type code used to select the correct statement schema |
| `updated_at` | Latest fetch time |

### `reports`

One raw source record per symbol, statement, and report date.

Primary key: `(symbol, statement, report_date)`.

| Column | Meaning |
|---|---|
| `statement` | `balance`, `income`, or `cashflow` |
| `report_date` | Financial report date |
| `report_type` | Original Eastmoney label such as `年报`, `一季报`, `中报`, or `三季报` |
| `report_date_name` | Original period display name |
| `notice_date` | Disclosure date |
| `currency` | Source currency when provided |
| `raw_json` | Complete Eastmoney source record |
| `run_id` | Fetch command that last refreshed this row |
| `fetched_at` | Last refresh time |

### `facts`

Long-form financial items extracted from each source report. This structure tolerates different field sets for ordinary companies, banks, insurers, and securities companies.

Primary key: `(symbol, statement, report_date, item_code)`.

| Column | Meaning |
|---|---|
| `item_code` | Original Eastmoney field code, for example `TOTAL_ASSETS` |
| `numeric_text` | Decimal representation preserved as text |
| `numeric_value` | SQLite `REAL` convenience value for aggregation |
| `text_value` | Non-numeric source value |
| `run_id` | Fetch command that last refreshed this row |
| `fetched_at` | Last refresh time |

Missing values remain `NULL`; the fetcher never converts them to zero.

### `financial_item_definitions`

Chinese labels and original display order parsed from Eastmoney's own statement templates. This is source display metadata, not a financial calculation.

Primary key: `(company_type, statement, item_code)`.

| Column | Meaning |
|---|---|
| `company_type` | Eastmoney company type whose template supplied the definition |
| `statement` | Statement owning the field |
| `item_code` | Eastmoney field code |
| `display_name` | Chinese statement line name |
| `display_order` | Original row order within the statement |
| `section_name` | Source section heading when present |
| `source_template` | Eastmoney template identifier |
| `fetched_at` | Last refresh time |

Definitions are refreshed as a complete set for the company type on every command, so removed or reordered source fields do not leave stale metadata.

### `report_market_snapshots`

One market and share-capital snapshot per symbol and financial report date.

Primary key: `(symbol, report_date)`.

| Column | Meaning |
|---|---|
| `report_date` | Financial report date used by all three statements |
| `report_type` | Original Eastmoney financial-report label |
| `trade_date` | Actual trading date used for the daily K-line |
| `share_effective_date` | Effective date of the share-capital row used |
| `open_price`, `close_price`, `high_price`, `low_price` | Unadjusted daily prices |
| `volume_lots` | Eastmoney daily volume in lots |
| `amount` | Daily turnover amount in yuan |
| `amplitude` | Daily amplitude percentage |
| `change_percent` | Daily price change percentage |
| `change_amount` | Daily price change amount |
| `turnover_rate` | Daily turnover rate percentage |
| `total_shares` | Source `TOTAL_SHARES` effective on the matched trading date |
| `circulating_shares` | Source `UNLIMITED_SHARES` effective on the matched trading date |
| `listed_a_shares` | Source `LISTED_A_SHARES` |
| `limited_shares` | Source `LIMITED_SHARES` |
| `market_cap` | `close_price * total_shares` |
| `circulating_market_cap` | `close_price * circulating_shares` |
| `quote_raw_json` | Exact parsed K-line source row |
| `capital_raw_json` | Complete capital-change source row |
| `run_id` | Fetch command that last refreshed this row |
| `fetched_at` | Last refresh time |

Only the two explicitly required market-cap fields are calculated and stored. No derived annual flag, fiscal-year field, or normalized report-type field is added.

The matching rules are:

1. `trade_date` is the latest trading date satisfying `trade_date <= report_date`.
2. `share_effective_date` is the latest capital-change date satisfying `share_effective_date <= trade_date`.
3. Prices use Eastmoney's unadjusted daily K-line (`fqt=0`) because adjusted prices cannot be multiplied by historical shares to obtain market capitalization.
4. The command first requests fresh market data. If the market endpoints are temporarily unavailable, it may reuse the existing database snapshots only when every selected report date is already covered; the JSON/console summary then records `market_source=database_cache`.
5. If a qualifying K-line/share-capital record is missing and the database cache cannot cover every selected report date, the command fails before database writes instead of producing a partial row.

### `share_capital_changes`

The complete Eastmoney share-capital history for a symbol, retained separately from report-date snapshots so repurchases, cancellations, placements, bonus shares, and other capital changes between reporting dates remain queryable.

Primary key: `(symbol, effective_date)`.

| Column | Meaning |
|---|---|
| `effective_date` | Effective date of the source capital-change row |
| `total_shares` | Total shares after the change |
| `circulating_shares` | Unrestricted circulating shares after the change |
| `listed_a_shares` | Listed A shares after the change |
| `limited_shares` | Restricted shares after the change |
| `change_reason` | Eastmoney `CHANGE_REASON`, such as `回购` |
| `raw_json` | Complete capital-change source row |
| `run_id` | Fetch command that last refreshed the row |
| `fetched_at` | Last refresh time |

A `回购` or `注销` reason combined with a decrease in `total_shares` is treated by the analysis script as repurchase-related share-reduction evidence. It is not a substitute for the listed company's cancellation announcement when legal confirmation matters. A positive balance-sheet `TREASURY_SHARES` amount instead represents treasury shares still carried as a contra-equity balance; these are different observations.

### `share_repurchase_programs`

The complete current Eastmoney stock-repurchase program list for a symbol. Each fetch refreshes the symbol's program set; this is the latest disclosed execution state, not a historical report-date snapshot.

Primary key: `(symbol, repurchase_code)`.

| Column | Meaning |
|---|---|
| `repurchase_code` | Stable Eastmoney program identifier (`REPURCODE`) |
| `announcement_date` | Initial program announcement date |
| `start_date`, `end_date`, `finish_date` | Source execution-period dates when disclosed |
| `latest_notice_date`, `updated_date` | Latest result notice and source update dates |
| `progress_code` | Original Eastmoney implementation-progress code |
| `share_type` | Repurchased share type |
| `objective` | Disclosed repurchase purpose, retained verbatim |
| `planned_amount_lower_text`, `planned_amount_upper_text` | Planned amount range, exact decimal text |
| `planned_shares_lower`, `planned_shares_upper` | Planned share-count range |
| `planned_price_cap_text` | Planned maximum repurchase price |
| `repurchased_amount_text` | Latest disclosed actual repurchase amount |
| `repurchased_shares` | Latest disclosed actual repurchased shares |
| `repurchased_price_lower_text`, `repurchased_price_upper_text` | Disclosed actual price range |
| `raw_json` | Complete Eastmoney program row |
| `run_id`, `fetched_at` | Refresh provenance |

Do not substitute `PAY_OTHER_FINANCE` for actual repurchase cash: that cash-flow line may contain unrelated financing outflows. Program-level actual amounts also cannot be allocated exactly across calendar years when execution spans more than one year.

### `fetch_runs`

Stores the latest provenance for a command identity. The `run_id` is deterministic for the same symbol, fiscal range, and period filter, so repeating the same command updates the existing run row.

## Idempotent writes

Business tables use stable primary keys and SQLite UPSERT semantics:

- `companies`: update by `symbol`.
- `reports`: update by `(symbol, statement, report_date)`.
- `facts`: replace the item set for `(symbol, statement, report_date)` inside one transaction.
- `financial_item_definitions`: refresh the company-type definition set.
- `report_market_snapshots`: update by `(symbol, report_date)`.
- `share_capital_changes`: refresh the symbol's complete source history and update by `(symbol, effective_date)`.
- `share_repurchase_programs`: refresh the symbol's complete current program list and update by `(symbol, repurchase_code)`.
- `fetch_runs`: update the deterministic `run_id` for the same command.

All database writes occur in one transaction. A failed write rolls back the financial statements, definitions, market snapshots, share-capital history, and repurchase programs together.

## Query a complete balance sheet

Long-form output with source labels and report-period market data:

```sql
SELECT
    f.report_date,
    r.report_type,
    d.section_name,
    d.display_name,
    f.item_code,
    COALESCE(f.numeric_text, f.text_value) AS item_value,
    m.trade_date,
    m.close_price,
    m.total_shares,
    m.market_cap
FROM facts AS f
JOIN reports AS r
  ON r.symbol = f.symbol
 AND r.statement = f.statement
 AND r.report_date = f.report_date
JOIN companies AS c
  ON c.symbol = f.symbol
LEFT JOIN financial_item_definitions AS d
  ON d.company_type = c.company_type
 AND d.statement = f.statement
 AND d.item_code = f.item_code
LEFT JOIN report_market_snapshots AS m
  ON m.symbol = f.symbol
 AND m.report_date = f.report_date
WHERE f.symbol = 'SH600809'
  AND f.statement = 'balance'
ORDER BY
    f.report_date DESC,
    COALESCE(d.display_order, 999999),
    f.item_code;
```

For an Eastmoney-style display, pivot the query result at read time:

- rows: `display_name` / `item_code`
- columns: `report_date`
- values: `item_value`

Do not create a physical statement-wide SQL table: Eastmoney field sets vary by company type and can change over time.
