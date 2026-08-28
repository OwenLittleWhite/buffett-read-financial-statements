#!/usr/bin/env python3
"""Compare registered Buffett-style metrics across locally stored companies."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, time as clock_time
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

try:
    from fetch_eastmoney_financials import (
        EastmoneyError,
        eastmoney_secid,
        fetch_json,
    )
except ModuleNotFoundError:
    from scripts.fetch_eastmoney_financials import (
        EastmoneyError,
        eastmoney_secid,
        fetch_json,
    )


DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "data" / "eastmoney_financials.sqlite3"
CURRENT_QUOTE_URLS = (
    "https://push2delay.eastmoney.com/api/qt/stock/get",
    "https://push2.eastmoney.com/api/qt/stock/get",
)
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+", help="Normalized A-share symbols")
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output", type=Path, help="Optional output file")
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Also write detailed JSON without a second analysis run",
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        help="Also write the detailed Markdown report without a second analysis run",
    )
    parser.add_argument(
        "--skip-current-price",
        action="store_true",
        help="Skip refreshing the latest completed closing price",
    )
    parser.add_argument("--price-timeout", type=float, default=10.0)
    parser.add_argument("--price-retries", type=int, default=2)
    parser.add_argument(
        "--financial-symbol",
        action="append",
        default=[],
        help="Financial-company symbol; repeat for multiple symbols",
    )
    return parser.parse_args()


def parse_eastmoney_quote_time(value: Any) -> datetime:
    text = str(value).strip()
    if text.isdigit() and len(text) <= 10:
        return datetime.fromtimestamp(int(text), tz=SHANGHAI_TIMEZONE)
    for pattern in ("%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=SHANGHAI_TIMEZONE)
        except ValueError:
            pass
    raise EastmoneyError(f"unexpected Eastmoney quote time: {value!r}")


def fetch_latest_completed_close(
    symbol: str,
    *,
    timeout: float,
    retries: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    params = {
        "secid": eastmoney_secid(symbol),
        "invt": "2",
        "fltt": "2",
        "fields": "f43,f57,f58,f59,f60,f84,f85,f86",
    }
    payload: dict[str, Any] | None = None
    actual_endpoint: str | None = None
    errors: list[str] = []
    for endpoint in CURRENT_QUOTE_URLS:
        try:
            payload = fetch_json(
                endpoint,
                params,
                timeout=timeout,
                retries=retries,
                referer="https://quote.eastmoney.com/",
            )
            actual_endpoint = endpoint
            break
        except EastmoneyError as exc:
            errors.append(str(exc))
    if payload is None or actual_endpoint is None:
        raise EastmoneyError(
            "all Eastmoney current-quote endpoints failed: " + " | ".join(errors)
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise EastmoneyError("Eastmoney current-quote endpoint returned no data")

    quote_time = parse_eastmoney_quote_time(data.get("f86"))
    shanghai_now = now or datetime.now(SHANGHAI_TIMEZONE)
    if shanghai_now.tzinfo is None:
        shanghai_now = shanghai_now.replace(tzinfo=SHANGHAI_TIMEZONE)
    else:
        shanghai_now = shanghai_now.astimezone(SHANGHAI_TIMEZONE)
    quote_is_completed_close = (
        quote_time.date() < shanghai_now.date()
        or quote_time.time() >= clock_time(15, 0)
    )
    price_field = "f43" if quote_is_completed_close else "f60"
    close_price = decimal_or_none(data.get(price_field))
    if close_price is None or close_price <= 0:
        raise EastmoneyError(
            f"Eastmoney current quote has no usable {price_field} price for {symbol}"
        )
    return {
        "close_price": close_price,
        "trade_date": quote_time.date().isoformat() if quote_is_completed_close else None,
        "price_kind": "latest_completed_close" if quote_is_completed_close else "previous_close",
        "quote_time": quote_time.isoformat(timespec="seconds"),
        "total_shares": decimal_or_none(data.get("f84")),
        "circulating_shares": decimal_or_none(data.get("f85")),
        "source_endpoint": actual_endpoint,
    }


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def safe_ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def classify_preferred_stock(
    equity_values: Sequence[Decimal | None],
    liability_values: Sequence[Decimal | None],
) -> str:
    values = [*equity_values, *liability_values]
    if any(value is not None and value > 0 for value in values):
        return "detected"
    if values and all(value is not None and value == 0 for value in values):
        return "not_detected_in_complete_data"
    return "insufficient_data"


def classify_capex_to_net_profit(
    ratio: Decimal | None,
    complete_ten_years: bool,
) -> str:
    if not complete_ten_years or ratio is None:
        return "insufficient_data"
    if ratio < Decimal("0.25"):
        return "below_25"
    if ratio < Decimal("0.50"):
        return "below_50"
    return "at_or_above_50"


def average(values: Iterable[Decimal | None]) -> Decimal | None:
    available = [value for value in values if value is not None]
    return sum(available, Decimal("0")) / len(available) if available else None


def median_available(values: Iterable[Decimal | None]) -> Decimal | None:
    available = [value for value in values if value is not None]
    return median(available) if available else None


def cagr(values: Sequence[Decimal | None]) -> float | None:
    available = [(index, value) for index, value in enumerate(values) if value is not None]
    if len(available) < 2:
        return None
    first_index, first = available[0]
    last_index, last = available[-1]
    periods = last_index - first_index
    if first is None or last is None or first <= 0 or last <= 0 or periods <= 0:
        return None
    return float(last / first) ** (1 / periods) - 1


def increasing_comparisons(values: Sequence[Decimal | None]) -> tuple[int, int]:
    pairs = [
        (previous, current)
        for previous, current in zip(values, values[1:])
        if previous is not None and current is not None
    ]
    return sum(current > previous for previous, current in pairs), len(pairs)


def consecutive_increase_streaks(
    values: Sequence[Decimal | None],
) -> tuple[int, int]:
    current_streak = 0
    longest_streak = 0
    for previous, current in zip(values, values[1:]):
        if previous is not None and current is not None and current > previous:
            current_streak += 1
            longest_streak = max(longest_streak, current_streak)
        else:
            current_streak = 0
    return current_streak, longest_streak


def year_over_year_rates(
    values: Sequence[Decimal | None],
) -> list[Decimal | None]:
    if not values:
        return []
    rates: list[Decimal | None] = [None]
    for previous, current in zip(values, values[1:]):
        if previous is None or current is None or previous <= 0:
            rates.append(None)
        else:
            rates.append((current - previous) / previous)
    return rates


def average_absolute(values: Iterable[Decimal | None]) -> Decimal | None:
    return average(abs(value) if value is not None else None for value in values)


def sum_available(values: Iterable[Decimal | None]) -> Decimal | None:
    available = [value for value in values if value is not None]
    return sum(available, Decimal("0")) if available else None


def classify_net_profit_margin(
    rate: Decimal | None,
    is_financial: bool = False,
) -> str:
    if rate is None:
        return "insufficient_data"
    if is_financial:
        return "not_applicable"
    if rate < Decimal("0.10"):
        return "highly_competitive"
    if rate < Decimal("0.20"):
        return "gray_zone"
    return "possible_competitive_advantage"


def classify_financial_loan_structure(
    short_loan: Decimal | None,
    long_loan: Decimal | None,
    is_financial: bool,
) -> str:
    if not is_financial:
        return "not_applicable"
    if short_loan is None or long_loan is None:
        return "insufficient_data"
    return "avoid" if short_loan > long_loan else "not_triggered"


def classify_debt_to_equity(
    ratio: Decimal | None,
    is_financial: bool,
) -> str:
    if is_financial:
        return "not_applicable"
    if ratio is None:
        return "insufficient_data"
    return "below_0_8" if ratio < Decimal("0.8") else "at_or_above_0_8"


def eps_trend(values: Sequence[Decimal | None]) -> dict[str, Any]:
    if len(values) != 10 or any(value is None for value in values):
        return {"level": "insufficient", "rising": None, "comparisons": None}
    complete = [value for value in values if value is not None]
    comparisons = list(zip(complete, complete[1:]))
    rising = sum(current > previous for previous, current in comparisons)
    longest_decline = 0
    current_decline = 0
    for previous, current in comparisons:
        if current < previous:
            current_decline += 1
            longest_decline = max(longest_decline, current_decline)
        else:
            current_decline = 0
    first_three = sum(complete[:3], Decimal("0")) / Decimal("3")
    last_three = sum(complete[-3:], Decimal("0")) / Decimal("3")
    strong = rising == 9
    moderate = (
        rising >= 6
        and complete[-1] > complete[0]
        and last_three > first_three
        and longest_decline <= 2
    )
    return {
        "level": "strong" if strong else "moderate" if moderate else "not_met",
        "rising": rising,
        "comparisons": 9,
        "longest_decline": longest_decline,
    }


def load_company(
    connection: sqlite3.Connection,
    symbol: str,
    start_year: int,
    end_year: int,
    is_financial: bool = False,
    current_quote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    company = connection.execute(
        "SELECT security_name FROM companies WHERE symbol=?", (symbol,)
    ).fetchone()
    if not company:
        raise ValueError(f"company not found in database: {symbol}")

    start_date = f"{start_year}-01-01"
    end_date = f"{end_year}-12-31"
    report_dates = [
        row[0]
        for row in connection.execute(
            """
            SELECT report_date
              FROM reports
             WHERE symbol=?
               AND statement='income'
               AND report_type='年报'
               AND report_date BETWEEN ? AND ?
             ORDER BY report_date
            """,
            (symbol, start_date, end_date),
        )
    ]
    facts: dict[str, dict[tuple[str, str], Decimal | None]] = {
        report_date: {} for report_date in report_dates
    }
    for row in connection.execute(
        """
        SELECT f.report_date, f.statement, f.item_code, f.numeric_text
          FROM facts AS f
          JOIN reports AS r
            ON r.symbol=f.symbol
           AND r.statement=f.statement
           AND r.report_date=f.report_date
         WHERE f.symbol=?
           AND r.report_type='年报'
           AND f.report_date BETWEEN ? AND ?
        """,
        (symbol, start_date, end_date),
    ):
        if row[0] in facts:
            facts[row[0]][(row[1], row[2])] = decimal_or_none(row[3])

    market_by_report_date = {
        str(row[0]): {
            "trade_date": str(row[1]),
            "close_price": decimal_or_none(row[2]),
            "total_shares": decimal_or_none(row[3]),
            "market_cap": decimal_or_none(row[4]),
        }
        for row in connection.execute(
            """
            SELECT report_date, trade_date, close_price, total_shares, market_cap
              FROM report_market_snapshots
             WHERE symbol=?
               AND report_date BETWEEN ? AND ?
            """,
            (symbol, start_date, end_date),
        )
    }
    latest_annual_pretax_row = connection.execute(
        """
        SELECT f.report_date, f.numeric_text, m.total_shares
          FROM facts AS f
          JOIN reports AS r
            ON r.symbol=f.symbol
           AND r.statement=f.statement
           AND r.report_date=f.report_date
          LEFT JOIN report_market_snapshots AS m
            ON m.symbol=f.symbol
           AND m.report_date=f.report_date
         WHERE f.symbol=?
           AND f.statement='income'
           AND f.item_code='TOTAL_PROFIT'
           AND r.report_type='年报'
         ORDER BY f.report_date DESC
         LIMIT 1
        """,
        (symbol,),
    ).fetchone()

    series: dict[str, list[Decimal | None]] = {
        name: []
        for name in (
            "gross_margin",
            "expense_to_gross_profit",
            "research_expense",
            "depreciation_to_gross_profit",
            "interest_to_operating_profit",
            "effective_income_tax_rate",
            "pretax_profit",
            "pretax_profit_per_share",
            "pretax_earnings_yield",
            "net_profit",
            "net_margin",
            "basic_eps",
            "monetary_funds",
            "ending_cash_equivalents",
            "total_assets",
            "total_liabilities",
            "short_loan",
            "long_loan",
            "current_maturity_debt",
            "bond_payable",
            "short_bond_payable",
            "lease_liability",
            "equity_financing_inflow",
            "long_asset_disposal_inflow",
            "subsidiary_disposal_inflow",
            "inventory",
            "accounts_receivable",
            "accounts_receivable_to_sales",
            "current_ratio",
            "fixed_assets",
            "long_asset_capex",
            "intangible_assets",
            "goodwill",
            "return_on_assets",
            "total_equity",
            "debt_to_equity",
            "long_loan_payoff_years",
            "retained_earnings",
            "treasury_shares",
            "preferred_shares_equity",
            "preferred_shares_liability",
            "return_on_equity",
            "equity_multiplier",
            "total_operating_cash_inflow",
            "total_operating_cash_outflow",
            "net_operating_cash",
            "loan_cash_inflow",
            "bond_issue_cash_inflow",
            "net_investing_cash",
            "net_financing_cash",
            "exchange_rate_cash_effect",
            "cash_equivalents_increase",
            "beginning_cash_equivalents",
            "subsidiary_acquisition_cash",
            "long_equity_investment",
            "other_equity_investment",
            "other_noncurrent_financial_assets",
            "creditor_investment",
            "other_creditor_investment",
            "available_for_sale_financial_assets",
            "held_to_maturity_investment",
            "investment_income",
            "joint_venture_investment_income",
        )
    }
    for report_date in report_dates:
        row = facts[report_date]
        revenue = row.get(("income", "OPERATE_INCOME"))
        cost = row.get(("income", "OPERATE_COST"))
        gross_profit = (
            revenue - cost if revenue is not None and cost is not None else None
        )
        gross_margin = safe_ratio(gross_profit, revenue)
        sale_expense = row.get(("income", "SALE_EXPENSE"))
        manage_expense = row.get(("income", "MANAGE_EXPENSE"))
        expense_sum = (
            sale_expense + manage_expense
            if sale_expense is not None and manage_expense is not None
            else None
        )
        net_profit = row.get(("income", "NETPROFIT"))
        total_income = row.get(("income", "TOTAL_OPERATE_INCOME"))
        pretax_profit = row.get(("income", "TOTAL_PROFIT"))
        market = market_by_report_date.get(report_date, {})
        pretax_profit_per_share = safe_ratio(
            pretax_profit,
            market.get("total_shares"),
        )
        series["gross_margin"].append(gross_margin)
        series["expense_to_gross_profit"].append(safe_ratio(expense_sum, gross_profit))
        series["research_expense"].append(row.get(("income", "RESEARCH_EXPENSE")))
        series["depreciation_to_gross_profit"].append(
            safe_ratio(row.get(("cashflow", "FA_IR_DEPR")), gross_profit)
        )
        series["interest_to_operating_profit"].append(
            safe_ratio(
                row.get(("income", "FE_INTEREST_EXPENSE")),
                row.get(("income", "OPERATE_PROFIT")),
            )
        )
        series["effective_income_tax_rate"].append(
            safe_ratio(row.get(("income", "INCOME_TAX")), pretax_profit)
        )
        series["pretax_profit"].append(pretax_profit)
        series["pretax_profit_per_share"].append(pretax_profit_per_share)
        series["pretax_earnings_yield"].append(
            safe_ratio(pretax_profit_per_share, market.get("close_price"))
        )
        series["net_profit"].append(net_profit)
        series["net_margin"].append(safe_ratio(net_profit, total_income))
        series["basic_eps"].append(row.get(("income", "BASIC_EPS")))
        series["monetary_funds"].append(row.get(("balance", "MONETARYFUNDS")))
        series["ending_cash_equivalents"].append(row.get(("cashflow", "END_CCE")))
        total_assets_value = row.get(("balance", "TOTAL_ASSETS"))
        series["total_assets"].append(total_assets_value)
        series["return_on_assets"].append(safe_ratio(net_profit, total_assets_value))
        series["total_liabilities"].append(
            row.get(("balance", "TOTAL_LIABILITIES"))
        )
        series["short_loan"].append(row.get(("balance", "SHORT_LOAN")))
        series["long_loan"].append(row.get(("balance", "LONG_LOAN")))
        series["current_maturity_debt"].append(
            row.get(("balance", "NONCURRENT_LIAB_1YEAR"))
        )
        series["bond_payable"].append(row.get(("balance", "BOND_PAYABLE")))
        series["short_bond_payable"].append(
            row.get(("balance", "SHORT_BOND_PAYABLE"))
        )
        series["lease_liability"].append(row.get(("balance", "LEASE_LIAB")))
        total_equity = row.get(("balance", "TOTAL_EQUITY"))
        long_loan = row.get(("balance", "LONG_LOAN"))
        series["total_equity"].append(total_equity)
        series["debt_to_equity"].append(
            safe_ratio(row.get(("balance", "TOTAL_LIABILITIES")), total_equity)
        )
        series["long_loan_payoff_years"].append(safe_ratio(long_loan, net_profit))
        unassigned_profit = row.get(("balance", "UNASSIGN_RPOFIT"))
        surplus_reserve = row.get(("balance", "SURPLUS_RESERVE"))
        retained_earnings = (
            unassigned_profit + surplus_reserve
            if unassigned_profit is not None and surplus_reserve is not None
            else None
        )
        series["retained_earnings"].append(retained_earnings)
        series["treasury_shares"].append(
            row.get(("balance", "TREASURY_SHARES"))
        )
        series["preferred_shares_equity"].append(
            row.get(("balance", "PREFERRED_SHARES"))
        )
        series["preferred_shares_liability"].append(
            row.get(("balance", "PREFERRED_SHARES_PAYBALE"))
        )
        series["return_on_equity"].append(safe_ratio(net_profit, total_equity))
        series["equity_multiplier"].append(
            safe_ratio(total_assets_value, total_equity)
        )
        series["total_operating_cash_inflow"].append(
            row.get(("cashflow", "TOTAL_OPERATE_INFLOW"))
        )
        series["total_operating_cash_outflow"].append(
            row.get(("cashflow", "TOTAL_OPERATE_OUTFLOW"))
        )
        series["net_operating_cash"].append(
            row.get(("cashflow", "NETCASH_OPERATE"))
        )
        series["loan_cash_inflow"].append(
            row.get(("cashflow", "RECEIVE_LOAN_CASH"))
        )
        series["bond_issue_cash_inflow"].append(
            row.get(("cashflow", "ISSUE_BOND"))
        )
        series["net_investing_cash"].append(
            row.get(("cashflow", "NETCASH_INVEST"))
        )
        series["net_financing_cash"].append(
            row.get(("cashflow", "NETCASH_FINANCE"))
        )
        series["exchange_rate_cash_effect"].append(
            row.get(("cashflow", "RATE_CHANGE_EFFECT"))
        )
        series["cash_equivalents_increase"].append(
            row.get(("cashflow", "CCE_ADD"))
        )
        series["beginning_cash_equivalents"].append(
            row.get(("cashflow", "BEGIN_CCE"))
        )
        series["subsidiary_acquisition_cash"].append(
            row.get(("cashflow", "OBTAIN_SUBSIDIARY_OTHER"))
        )
        series["long_equity_investment"].append(
            row.get(("balance", "LONG_EQUITY_INVEST"))
        )
        series["other_equity_investment"].append(
            row.get(("balance", "OTHER_EQUITY_INVEST"))
        )
        series["other_noncurrent_financial_assets"].append(
            row.get(("balance", "OTHER_NONCURRENT_FINASSET"))
        )
        series["creditor_investment"].append(
            row.get(("balance", "CREDITOR_INVEST"))
        )
        series["other_creditor_investment"].append(
            row.get(("balance", "OTHER_CREDITOR_INVEST"))
        )
        series["available_for_sale_financial_assets"].append(
            row.get(("balance", "AVAILABLE_SALE_FINASSET"))
        )
        series["held_to_maturity_investment"].append(
            row.get(("balance", "HOLD_MATURITY_INVEST"))
        )
        series["investment_income"].append(
            row.get(("income", "INVEST_INCOME"))
        )
        series["joint_venture_investment_income"].append(
            row.get(("income", "INVEST_JOINT_INCOME"))
        )
        series["equity_financing_inflow"].append(
            row.get(("cashflow", "ACCEPT_INVEST_CASH"))
        )
        series["long_asset_disposal_inflow"].append(
            row.get(("cashflow", "DISPOSAL_LONG_ASSET"))
        )
        series["subsidiary_disposal_inflow"].append(
            row.get(("cashflow", "DISPOSAL_SUBSIDIARY_OTHER"))
        )
        inventory = row.get(("balance", "INVENTORY"))
        accounts_receivable = row.get(("balance", "ACCOUNTS_RECE"))
        total_current_assets = row.get(("balance", "TOTAL_CURRENT_ASSETS"))
        total_current_liabilities = row.get(("balance", "TOTAL_CURRENT_LIAB"))
        series["inventory"].append(inventory)
        series["accounts_receivable"].append(accounts_receivable)
        series["accounts_receivable_to_sales"].append(
            safe_ratio(accounts_receivable, revenue)
        )
        series["current_ratio"].append(
            safe_ratio(total_current_assets, total_current_liabilities)
        )
        series["fixed_assets"].append(row.get(("balance", "FIXED_ASSET")))
        series["long_asset_capex"].append(
            row.get(("cashflow", "CONSTRUCT_LONG_ASSET"))
        )
        series["intangible_assets"].append(
            row.get(("balance", "INTANGIBLE_ASSET"))
        )
        series["goodwill"].append(row.get(("balance", "GOODWILL")))

    if len(report_dates) != end_year - start_year + 1:
        coverage = f"{len(report_dates)}/{end_year - start_year + 1}"
    else:
        coverage = "complete"

    gross = series["gross_margin"]
    expenses = series["expense_to_gross_profit"]
    research = series["research_expense"]
    depreciation = series["depreciation_to_gross_profit"]
    interest = series["interest_to_operating_profit"]
    tax_rates = series["effective_income_tax_rate"]
    pretax = series["pretax_profit"]
    pretax_per_share = series["pretax_profit_per_share"]
    pretax_earnings_yields = series["pretax_earnings_yield"]
    pretax_per_share_growth = year_over_year_rates(pretax_per_share)
    latest_annual_pretax_report_date = (
        str(latest_annual_pretax_row[0]) if latest_annual_pretax_row else None
    )
    latest_annual_pretax_profit = (
        decimal_or_none(latest_annual_pretax_row[1])
        if latest_annual_pretax_row
        else None
    )
    report_date_share_count = (
        decimal_or_none(latest_annual_pretax_row[2])
        if latest_annual_pretax_row
        else None
    )
    current_quote = current_quote or {}
    current_share_count = current_quote.get("total_shares")
    current_share_count_source = "current_quote"
    if current_share_count is None or current_share_count <= 0:
        current_share_count = report_date_share_count
        current_share_count_source = "latest_annual_report_snapshot"
    current_pretax_profit_per_share = safe_ratio(
        latest_annual_pretax_profit,
        current_share_count,
    )
    current_buyin_pretax_earnings_yield = safe_ratio(
        current_pretax_profit_per_share,
        current_quote.get("close_price"),
    )
    net_profit_values = series["net_profit"]
    net_margin_values = series["net_margin"]
    eps_values = series["basic_eps"]
    monetary_funds = series["monetary_funds"]
    ending_cash_equivalents = series["ending_cash_equivalents"]
    total_assets = series["total_assets"]
    total_liabilities = series["total_liabilities"]
    equity_financing = series["equity_financing_inflow"]
    long_asset_disposal = series["long_asset_disposal_inflow"]
    subsidiary_disposal = series["subsidiary_disposal_inflow"]
    financing_debt_series = [
        series["short_loan"],
        series["long_loan"],
        series["current_maturity_debt"],
        series["bond_payable"],
        series["short_bond_payable"],
        series["lease_liability"],
    ]
    latest_financing_debt_components = [
        values[-1] for values in financing_debt_series if values and values[-1] is not None
    ]
    inventory = series["inventory"]
    inventory_growth = year_over_year_rates(inventory)
    net_profit_growth = year_over_year_rates(net_profit_values)
    inventory_growth_pairs = [
        (inventory_rate, profit_rate)
        for inventory_rate, profit_rate in zip(inventory_growth, net_profit_growth)
        if inventory_rate is not None
        and inventory_rate > 0
        and profit_rate is not None
    ]
    receivable_ratios = series["accounts_receivable_to_sales"]
    current_ratios = series["current_ratio"]
    fixed_assets = series["fixed_assets"]
    fixed_asset_growth = year_over_year_rates(fixed_assets)
    long_asset_capex = series["long_asset_capex"]
    ten_year_capex = long_asset_capex[-10:]
    ten_year_net_profit = net_profit_values[-10:]
    capex_profit_complete_ten_years = (
        len(ten_year_capex) == 10
        and len(ten_year_net_profit) == 10
        and all(value is not None for value in ten_year_capex)
        and all(value is not None for value in ten_year_net_profit)
    )
    cumulative_ten_year_capex = (
        sum((value for value in ten_year_capex if value is not None), Decimal("0"))
        if capex_profit_complete_ten_years
        else None
    )
    cumulative_ten_year_net_profit = (
        sum(
            (value for value in ten_year_net_profit if value is not None),
            Decimal("0"),
        )
        if capex_profit_complete_ten_years
        else None
    )
    capex_to_net_profit = (
        safe_ratio(cumulative_ten_year_capex, cumulative_ten_year_net_profit)
        if capex_profit_complete_ten_years
        else None
    )
    intangible_assets = series["intangible_assets"]
    goodwill = series["goodwill"]
    goodwill_rising, goodwill_comparisons = increasing_comparisons(goodwill)
    goodwill_current_streak, goodwill_longest_streak = (
        consecutive_increase_streaks(goodwill)
    )
    subsidiary_acquisition_cash = series["subsidiary_acquisition_cash"]
    long_equity_investment = series["long_equity_investment"]
    other_equity_investment = series["other_equity_investment"]
    other_noncurrent_financial_assets = series[
        "other_noncurrent_financial_assets"
    ]
    creditor_investment = series["creditor_investment"]
    other_creditor_investment = series["other_creditor_investment"]
    available_for_sale_financial_assets = series[
        "available_for_sale_financial_assets"
    ]
    held_to_maturity_investment = series["held_to_maturity_investment"]
    investment_income = series["investment_income"]
    joint_venture_investment_income = series[
        "joint_venture_investment_income"
    ]
    return_on_assets = series["return_on_assets"]
    debt_to_equity = series["debt_to_equity"]
    long_loan_payoff_years = series["long_loan_payoff_years"]
    latest_short_loan = series["short_loan"][-1] if series["short_loan"] else None
    latest_long_loan = series["long_loan"][-1] if series["long_loan"] else None
    long_term_debt_series = [
        series["long_loan"],
        series["bond_payable"],
        series["lease_liability"],
        series["current_maturity_debt"],
    ]
    latest_long_term_debt_components = [
        values[-1]
        for values in long_term_debt_series
        if values and values[-1] is not None
    ]
    latest_known_long_term_debt = (
        sum(latest_long_term_debt_components, Decimal("0"))
        if latest_long_term_debt_components
        else None
    )
    latest_complete_long_term_debt_payoff_years = (
        safe_ratio(
            latest_known_long_term_debt,
            net_profit_values[-1] if net_profit_values else None,
        )
        if len(latest_long_term_debt_components) == len(long_term_debt_series)
        else None
    )
    retained_earnings = series["retained_earnings"]
    retained_earnings_growth = year_over_year_rates(retained_earnings)
    treasury_shares = series["treasury_shares"]
    preferred_shares_equity = series["preferred_shares_equity"]
    preferred_shares_liability = series["preferred_shares_liability"]
    return_on_equity = series["return_on_equity"]
    equity_multiplier = series["equity_multiplier"]
    seven_year_slice = slice(-7, None)
    seven_year_operating_inflow = series["total_operating_cash_inflow"][
        seven_year_slice
    ]
    seven_year_operating_outflow = series["total_operating_cash_outflow"][
        seven_year_slice
    ]
    seven_year_operating_cash = series["net_operating_cash"][seven_year_slice]
    seven_year_investing_cash = series["net_investing_cash"][seven_year_slice]
    seven_year_financing_cash = series["net_financing_cash"][seven_year_slice]
    seven_year_exchange_effect = series["exchange_rate_cash_effect"][seven_year_slice]
    seven_year_cash_increase = series["cash_equivalents_increase"][seven_year_slice]
    seven_year_loan_cash = series["loan_cash_inflow"][seven_year_slice]
    seven_year_bond_cash = series["bond_issue_cash_inflow"][seven_year_slice]
    seven_year_equity_cash = series["equity_financing_inflow"][seven_year_slice]
    seven_year_asset_sale_cash = series["long_asset_disposal_inflow"][
        seven_year_slice
    ]
    seven_year_business_sale_cash = series["subsidiary_disposal_inflow"][
        seven_year_slice
    ]
    operating_cash_identity_checks: list[bool] = []
    cash_reconciliation_checks: list[bool] = []
    for values in zip(
        seven_year_operating_inflow,
        seven_year_operating_outflow,
        seven_year_operating_cash,
        seven_year_investing_cash,
        seven_year_financing_cash,
        seven_year_exchange_effect,
        seven_year_cash_increase,
    ):
        (
            operating_inflow,
            operating_outflow,
            operating_cash,
            investing_cash,
            financing_cash,
            exchange_effect,
            cash_increase,
        ) = values
        if (
            operating_inflow is not None
            and operating_outflow is not None
            and operating_cash is not None
        ):
            operating_cash_identity_checks.append(
                abs(operating_inflow - operating_outflow - operating_cash)
                <= Decimal("1")
            )
        if all(
            value is not None
            for value in (
                operating_cash,
                investing_cash,
                financing_cash,
                exchange_effect,
                cash_increase,
            )
        ):
            cash_reconciliation_checks.append(
                abs(
                    operating_cash
                    + investing_cash
                    + financing_cash
                    + exchange_effect
                    - cash_increase
                )
                <= Decimal("1")
            )
    share_capital_change_rows: list[tuple[str, int, str | None]] = []
    share_capital_table_exists = connection.execute(
        """
        SELECT 1
          FROM sqlite_master
         WHERE type='table' AND name='share_capital_changes'
        """
    ).fetchone()
    if share_capital_table_exists:
        share_capital_change_rows = [
            (str(row[0]), int(row[1]), str(row[2]) if row[2] is not None else None)
            for row in connection.execute(
                """
                SELECT effective_date, total_shares, change_reason
                  FROM share_capital_changes
                 WHERE symbol=? AND effective_date<=?
                 ORDER BY effective_date
                """,
                (symbol, end_date),
            )
        ]
    repurchase_share_reductions: list[dict[str, Any]] = []
    for previous, current in zip(
        share_capital_change_rows,
        share_capital_change_rows[1:],
    ):
        effective_date, total_shares_value, change_reason = current
        reason = change_reason or ""
        if (
            effective_date >= start_date
            and total_shares_value < previous[1]
            and ("回购" in reason or "注销" in reason)
        ):
            repurchase_share_reductions.append(
                {
                    "effective_date": effective_date,
                    "share_reduction": previous[1] - total_shares_value,
                    "change_reason": change_reason,
                }
            )
    repurchase_program_rows: list[tuple[Any, ...]] = []
    repurchase_table_exists = connection.execute(
        """
        SELECT 1
          FROM sqlite_master
         WHERE type='table' AND name='share_repurchase_programs'
        """
    ).fetchone()
    if repurchase_table_exists:
        repurchase_program_rows = list(
            connection.execute(
                """
                SELECT repurchase_code, announcement_date, finish_date,
                       progress_code, objective, repurchased_amount_text,
                       repurchased_shares
                  FROM share_repurchase_programs
                 WHERE symbol=?
                   AND announcement_date BETWEEN ? AND ?
                 ORDER BY announcement_date, repurchase_code
                """,
                (symbol, start_date, end_date),
            )
        )
    repurchase_amounts = [
        decimal_or_none(row[5]) for row in repurchase_program_rows
    ]
    repurchase_announcement_years = {
        str(row[1])[:4] for row in repurchase_program_rows if row[1]
    }
    net_rising, net_comparisons = increasing_comparisons(net_profit_values)

    return {
        "symbol": symbol,
        "name": company[0],
        "coverage": coverage,
        "report_dates": report_dates,
        "gross_margin": {
            "average": average(gross),
            "minimum": min((x for x in gross if x is not None), default=None),
            "latest": gross[-1] if gross else None,
            "years_ge_40": sum(x >= Decimal("0.40") for x in gross if x is not None),
            "available": sum(x is not None for x in gross),
        },
        "expense_to_gross_profit": {
            "average": average(expenses),
            "latest": expenses[-1] if expenses else None,
            "years_lt_30": sum(x < Decimal("0.30") for x in expenses if x is not None),
            "available": sum(x is not None for x in expenses),
        },
        "research_expense": {
            "latest": research[-1] if research else None,
            "cagr": cagr(research),
            "available": sum(x is not None for x in research),
        },
        "depreciation_to_gross_profit": {
            "average": average(depreciation),
            "latest": depreciation[-1] if depreciation else None,
            "available": sum(x is not None for x in depreciation),
        },
        "interest_to_operating_profit": {
            "maximum": max((x for x in interest if x is not None), default=None),
            "latest": interest[-1] if interest else None,
            "years_lt_15": sum(x < Decimal("0.15") for x in interest if x is not None),
            "available": sum(x is not None for x in interest),
        },
        "effective_income_tax_rate": {
            "average": average(tax_rates),
            "latest": tax_rates[-1] if tax_rates else None,
            "available": sum(x is not None for x in tax_rates),
        },
        "pretax_profit": {
            "latest": pretax[-1] if pretax else None,
            "cagr": cagr(pretax),
        },
        "equity_bond": {
            "current_buyin": {
                "pretax_profit_report_date": latest_annual_pretax_report_date,
                "annual_pretax_profit": latest_annual_pretax_profit,
                "price_trade_date": current_quote.get("trade_date"),
                "price_kind": current_quote.get("price_kind"),
                "quote_time": current_quote.get("quote_time"),
                "close_price": current_quote.get("close_price"),
                "total_shares": current_share_count,
                "share_count_source": current_share_count_source,
                "pretax_profit_per_share": current_pretax_profit_per_share,
                "pretax_earnings_yield": current_buyin_pretax_earnings_yield,
                "quote_source_endpoint": current_quote.get("source_endpoint"),
                "quote_error": current_quote.get("error"),
            },
            "latest_report_date": report_dates[-1] if report_dates else None,
            "latest_trade_date": (
                market_by_report_date.get(report_dates[-1], {}).get("trade_date")
                if report_dates
                else None
            ),
            "latest_close_price": (
                market_by_report_date.get(report_dates[-1], {}).get("close_price")
                if report_dates
                else None
            ),
            "latest_total_shares": (
                market_by_report_date.get(report_dates[-1], {}).get("total_shares")
                if report_dates
                else None
            ),
            "latest_pretax_profit_per_share": (
                pretax_per_share[-1] if pretax_per_share else None
            ),
            "latest_pretax_earnings_yield": (
                pretax_earnings_yields[-1] if pretax_earnings_yields else None
            ),
            "pretax_profit_per_share_cagr": cagr(pretax_per_share),
            "latest_pretax_profit_per_share_yoy_growth": (
                pretax_per_share_growth[-1]
                if pretax_per_share_growth
                else None
            ),
            "pretax_profit_per_share_median_yoy_growth": median_available(
                pretax_per_share_growth
            ),
            **eps_trend(pretax_per_share),
        },
        "net_profit": {
            "cagr": cagr(net_profit_values),
            "rising": net_rising,
            "comparisons": net_comparisons,
            "latest_margin": net_margin_values[-1] if net_margin_values else None,
            "latest_margin_category": classify_net_profit_margin(
                net_margin_values[-1] if net_margin_values else None,
                is_financial=is_financial,
            ),
            "years_ge_20": sum(
                x >= Decimal("0.20") for x in net_margin_values if x is not None
            ),
            "margin_available": sum(x is not None for x in net_margin_values),
        },
        "basic_eps": {
            **eps_trend(eps_values),
            "cagr": cagr(eps_values),
        },
        "cash_safety": {
            "latest_monetary_funds": monetary_funds[-1] if monetary_funds else None,
            "latest_ending_cash_equivalents": (
                ending_cash_equivalents[-1] if ending_cash_equivalents else None
            ),
            "latest_cash_to_assets": safe_ratio(
                monetary_funds[-1] if monetary_funds else None,
                total_assets[-1] if total_assets else None,
            ),
            "latest_cash_to_total_liabilities": safe_ratio(
                monetary_funds[-1] if monetary_funds else None,
                total_liabilities[-1] if total_liabilities else None,
            ),
            "latest_known_financing_debt_lower_bound": (
                sum(latest_financing_debt_components, Decimal("0"))
                if latest_financing_debt_components
                else None
            ),
            "latest_financing_debt_components_available": len(
                latest_financing_debt_components
            ),
            "financing_debt_component_total": len(financing_debt_series),
            "profitable_years": sum(
                value > 0 for value in net_profit_values if value is not None
            ),
            "profit_available": sum(value is not None for value in net_profit_values),
            "equity_financing_positive_years": sum(
                value > 0 for value in equity_financing if value is not None
            ),
            "equity_financing_available": sum(
                value is not None for value in equity_financing
            ),
            "long_asset_disposal_positive_years": sum(
                value > 0 for value in long_asset_disposal if value is not None
            ),
            "long_asset_disposal_available": sum(
                value is not None for value in long_asset_disposal
            ),
            "subsidiary_disposal_positive_years": sum(
                value > 0 for value in subsidiary_disposal if value is not None
            ),
            "subsidiary_disposal_available": sum(
                value is not None for value in subsidiary_disposal
            ),
        },
        "inventory_profit_alignment": {
            "latest_inventory": inventory[-1] if inventory else None,
            "inventory_cagr": cagr(inventory),
            "maximum_inventory_growth": max(
                (
                    value
                    for value in inventory_growth
                    if value is not None and value > 0
                ),
                default=None,
            ),
            "maximum_inventory_decline": min(
                (
                    value
                    for value in inventory_growth
                    if value is not None and value < 0
                ),
                default=None,
            ),
            "inventory_growth_with_profit_comparable": len(inventory_growth_pairs),
            "inventory_and_profit_both_growth": sum(
                profit_rate > 0 for _, profit_rate in inventory_growth_pairs
            ),
        },
        "accounts_receivable_to_sales": {
            "average": average(receivable_ratios),
            "latest": receivable_ratios[-1] if receivable_ratios else None,
            "available": sum(value is not None for value in receivable_ratios),
        },
        "current_ratio": {
            "average": average(current_ratios),
            "latest": current_ratios[-1] if current_ratios else None,
            "years_lt_1": sum(
                value < Decimal("1") for value in current_ratios if value is not None
            ),
            "available": sum(value is not None for value in current_ratios),
        },
        "fixed_asset_stability": {
            "latest_fixed_assets": fixed_assets[-1] if fixed_assets else None,
            "average_absolute_yoy_change": average_absolute(fixed_asset_growth),
            "maximum_growth": max(
                (
                    value
                    for value in fixed_asset_growth
                    if value is not None and value > 0
                ),
                default=None,
            ),
            "maximum_decline": min(
                (
                    value
                    for value in fixed_asset_growth
                    if value is not None and value < 0
                ),
                default=None,
            ),
            "latest_long_asset_capex": (
                long_asset_capex[-1] if long_asset_capex else None
            ),
            "capex_available": sum(value is not None for value in long_asset_capex),
            "cumulative_ten_year_capex": cumulative_ten_year_capex,
            "cumulative_ten_year_net_profit": cumulative_ten_year_net_profit,
            "ten_year_capex_to_net_profit": capex_to_net_profit,
            "ten_year_capex_to_net_profit_classification": (
                classify_capex_to_net_profit(
                    capex_to_net_profit,
                    capex_profit_complete_ten_years,
                )
            ),
            "capex_profit_complete_ten_years": capex_profit_complete_ten_years,
        },
        "recognized_intangible_assets": {
            "latest": intangible_assets[-1] if intangible_assets else None,
            "latest_to_total_assets": safe_ratio(
                intangible_assets[-1] if intangible_assets else None,
                total_assets[-1] if total_assets else None,
            ),
            "latest_goodwill": goodwill[-1] if goodwill else None,
            "goodwill_available": sum(value is not None for value in goodwill),
            "goodwill_rising": goodwill_rising,
            "goodwill_comparisons": goodwill_comparisons,
            "goodwill_current_increase_streak": goodwill_current_streak,
            "goodwill_longest_increase_streak": goodwill_longest_streak,
            "subsidiary_acquisition_cash_total": sum_available(
                subsidiary_acquisition_cash
            ),
            "subsidiary_acquisition_cash_positive_years": sum(
                value > 0
                for value in subsidiary_acquisition_cash
                if value is not None
            ),
            "subsidiary_acquisition_cash_available": sum(
                value is not None for value in subsidiary_acquisition_cash
            ),
        },
        "long_term_investments": {
            "latest_long_equity_investment": (
                long_equity_investment[-1] if long_equity_investment else None
            ),
            "long_equity_investment_to_total_assets": safe_ratio(
                long_equity_investment[-1] if long_equity_investment else None,
                total_assets[-1] if total_assets else None,
            ),
            "long_equity_investment_cagr": cagr(long_equity_investment),
            "latest_other_equity_investment": (
                other_equity_investment[-1] if other_equity_investment else None
            ),
            "latest_other_noncurrent_financial_assets": (
                other_noncurrent_financial_assets[-1]
                if other_noncurrent_financial_assets
                else None
            ),
            "latest_creditor_investment": (
                creditor_investment[-1] if creditor_investment else None
            ),
            "latest_other_creditor_investment": (
                other_creditor_investment[-1]
                if other_creditor_investment
                else None
            ),
            "latest_available_for_sale_financial_assets": (
                available_for_sale_financial_assets[-1]
                if available_for_sale_financial_assets
                else None
            ),
            "latest_held_to_maturity_investment": (
                held_to_maturity_investment[-1]
                if held_to_maturity_investment
                else None
            ),
            "latest_investment_income": (
                investment_income[-1] if investment_income else None
            ),
            "latest_joint_venture_investment_income": (
                joint_venture_investment_income[-1]
                if joint_venture_investment_income
                else None
            ),
        },
        "return_on_assets": {
            "average": average(return_on_assets),
            "latest": return_on_assets[-1] if return_on_assets else None,
            "maximum": max(
                (value for value in return_on_assets if value is not None),
                default=None,
            ),
            "available": sum(value is not None for value in return_on_assets),
        },
        "debt_structure": {
            "latest_short_loan": latest_short_loan,
            "latest_long_loan": latest_long_loan,
            "financial_loan_structure": classify_financial_loan_structure(
                latest_short_loan,
                latest_long_loan,
                is_financial,
            ),
            "latest_long_loan_payoff_years": (
                long_loan_payoff_years[-1] if long_loan_payoff_years else None
            ),
            "latest_known_long_term_debt_lower_bound": (
                latest_known_long_term_debt
            ),
            "latest_long_term_debt_components_available": len(
                latest_long_term_debt_components
            ),
            "long_term_debt_component_total": len(long_term_debt_series),
            "latest_complete_long_term_debt_payoff_years": (
                latest_complete_long_term_debt_payoff_years
            ),
            "latest_debt_to_equity": (
                debt_to_equity[-1] if debt_to_equity else None
            ),
            "debt_to_equity_category": classify_debt_to_equity(
                debt_to_equity[-1] if debt_to_equity else None,
                is_financial,
            ),
            "debt_to_equity_average": average(debt_to_equity),
            "debt_to_equity_available": sum(
                value is not None for value in debt_to_equity
            ),
        },
        "retained_earnings_and_equity_returns": {
            "latest_retained_earnings": (
                retained_earnings[-1] if retained_earnings else None
            ),
            "retained_earnings_cagr": cagr(retained_earnings),
            "retained_earnings_rising": increasing_comparisons(
                retained_earnings
            )[0],
            "retained_earnings_comparisons": increasing_comparisons(
                retained_earnings
            )[1],
            "maximum_retained_earnings_growth": max(
                (
                    value
                    for value in retained_earnings_growth
                    if value is not None and value > 0
                ),
                default=None,
            ),
            "latest_treasury_shares": (
                treasury_shares[-1] if treasury_shares else None
            ),
            "treasury_shares_positive_years": sum(
                value > 0 for value in treasury_shares if value is not None
            ),
            "treasury_shares_available": sum(
                value is not None for value in treasury_shares
            ),
            "repurchase_share_reduction_events": repurchase_share_reductions,
            "repurchase_share_reduction_count": len(
                repurchase_share_reductions
            ),
            "repurchase_share_reduction_total_shares": sum(
                event["share_reduction"] for event in repurchase_share_reductions
            ),
            "repurchase_program_count": len(repurchase_program_rows),
            "repurchase_program_announcement_years": len(
                repurchase_announcement_years
            ),
            "repurchase_actual_amount_total": sum_available(repurchase_amounts),
            "repurchase_actual_amount_available": sum(
                value is not None for value in repurchase_amounts
            ),
            "repurchase_actual_shares_total": sum(
                int(row[6])
                for row in repurchase_program_rows
                if row[6] is not None
            )
            if any(row[6] is not None for row in repurchase_program_rows)
            else None,
            "repurchase_actual_shares_available": sum(
                row[6] is not None for row in repurchase_program_rows
            ),
            "repurchase_cancellation_intent_count": sum(
                "注销" in str(row[4] or "") or "减少注册资本" in str(row[4] or "")
                for row in repurchase_program_rows
            ),
            "return_on_equity_average": average(return_on_equity),
            "return_on_equity_latest": (
                return_on_equity[-1] if return_on_equity else None
            ),
            "return_on_equity_maximum": max(
                (value for value in return_on_equity if value is not None),
                default=None,
            ),
            "equity_multiplier_average": average(equity_multiplier),
            "equity_multiplier_latest": (
                equity_multiplier[-1] if equity_multiplier else None
            ),
            "equity_multiplier_maximum": max(
                (value for value in equity_multiplier if value is not None),
                default=None,
            ),
        },
        "preferred_stock_capital_structure": {
            "latest_equity_classified_preferred_shares": (
                preferred_shares_equity[-1] if preferred_shares_equity else None
            ),
            "latest_liability_classified_preferred_shares": (
                preferred_shares_liability[-1]
                if preferred_shares_liability
                else None
            ),
            "years_with_detected_preferred_shares": sum(
                (equity is not None and equity > 0)
                or (liability is not None and liability > 0)
                for equity, liability in zip(
                    preferred_shares_equity, preferred_shares_liability
                )
            ),
            "available_cells": sum(
                value is not None
                for value in [
                    *preferred_shares_equity,
                    *preferred_shares_liability,
                ]
            ),
            "expected_cells": len(preferred_shares_equity)
            + len(preferred_shares_liability),
            "classification": classify_preferred_stock(
                preferred_shares_equity, preferred_shares_liability
            ),
        },
        "seven_year_cash_sources": {
            "years_requested": 7,
            "years_available": len(seven_year_operating_cash),
            "cumulative_net_operating_cash": sum_available(
                seven_year_operating_cash
            ),
            "positive_operating_cash_years": sum(
                value > 0 for value in seven_year_operating_cash if value is not None
            ),
            "operating_cash_available": sum(
                value is not None for value in seven_year_operating_cash
            ),
            "operating_cash_positive_all_seven": (
                None
                if len(seven_year_operating_cash) != 7
                or any(value is None for value in seven_year_operating_cash)
                else all(
                    value > 0
                    for value in seven_year_operating_cash
                    if value is not None
                )
            ),
            "cumulative_net_investing_cash": sum_available(
                seven_year_investing_cash
            ),
            "cumulative_net_financing_cash": sum_available(
                seven_year_financing_cash
            ),
            "known_debt_financing_cash_inflow_lower_bound": sum_available(
                [*seven_year_loan_cash, *seven_year_bond_cash]
            ),
            "known_equity_financing_cash_inflow_lower_bound": sum_available(
                seven_year_equity_cash
            ),
            "known_asset_business_sale_cash_inflow_lower_bound": sum_available(
                [*seven_year_asset_sale_cash, *seven_year_business_sale_cash]
            ),
            "cumulative_cash_equivalents_increase": sum_available(
                seven_year_cash_increase
            ),
            "operating_cash_identity_passed": sum(operating_cash_identity_checks),
            "operating_cash_identity_available": len(
                operating_cash_identity_checks
            ),
            "cash_reconciliation_passed": sum(cash_reconciliation_checks),
            "cash_reconciliation_available": len(cash_reconciliation_checks),
        },
    }


def rank(
    companies: list[dict[str, Any]],
    path: tuple[str, str],
    key_name: str,
    reverse: bool = True,
) -> None:
    available = [
        company
        for company in companies
        if company[path[0]][path[1]] is not None
    ]
    available.sort(key=lambda company: company[path[0]][path[1]], reverse=reverse)
    for position, company in enumerate(available, start=1):
        company[path[0]][key_name] = position


def json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


def percent(value: Decimal | float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.{digits}f}%"


def count_text(count: int, available: int, condition: str) -> str:
    return f"{count}/{available}{condition}" if available else "—"


def hundred_million(value: Decimal | None) -> str:
    if value is None:
        return "—"
    scaled = value / Decimal("100000000")
    digits = 2 if scaled != 0 and abs(scaled) < Decimal("0.1") else 1
    return f"{scaled:.{digits}f}亿"


def number(value: Decimal | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def ten_thousand_shares(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value / 10000:.1f}万股"


def markdown(companies: list[dict[str, Any]], start_year: int, end_year: int) -> str:
    lines = [
        f"## 盈利能力与费用（{start_year}—{end_year}年报）",
        "",
        "| 公司 | 毛利率10年均值/最低 | ≥40%年数 | 销管费/毛利均值/2025 | <30%年数 | 折旧/毛利均值/2025 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for c in companies:
        g = c["gross_margin"]
        e = c["expense_to_gross_profit"]
        d = c["depreciation_to_gross_profit"]
        lines.append(
            f"| {c['name']} | {percent(g['average'])} / {percent(g['minimum'])} | "
            f"{count_text(g['years_ge_40'], g['available'], '')} | "
            f"{percent(e['average'])} / {percent(e['latest'])} | "
            f"{count_text(e['years_lt_30'], e['available'], '')} | "
            f"{percent(d['average'])} / {percent(d['latest'])} |"
        )

    lines.extend(
        [
            "",
            "## 财务负担与利润规模",
            "",
            f"| 公司 | 研发费{end_year}/披露年数 | 利息/营业利润最大值 | <15%年数/有数据年数 | {end_year}有效所得税率 | 税前利润{end_year}（排名） | 税前利润CAGR |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for c in companies:
        r = c["research_expense"]
        i = c["interest_to_operating_profit"]
        t = c["effective_income_tax_rate"]
        p = c["pretax_profit"]
        lines.append(
            f"| {c['name']} | {hundred_million(r['latest'])} / {r['available']}年 | "
            f"{percent(i['maximum'], 2)} | {count_text(i['years_lt_15'], i['available'], '')} | "
            f"{percent(t['latest'], 2)} | "
            f"{hundred_million(p['latest'])}（{p.get('rank', '—')}/{len(companies)}） | {percent(p['cagr'])} |"
        )

    equity_bond_level_names = {
        "strong": "强",
        "moderate": "温和",
        "not_met": "未满足",
        "insufficient": "数据不足",
    }
    lines.extend(
        [
            "",
            "## 股权债券：当前买入成本税前收益率",
            "",
            "| 公司 | 税前利润年报/价格日 | 最新已完成收盘价 | 当前股本口径每股税前利润（估算） | 当前买入成本税前收益率 | 每股税前利润CAGR | 最近一年同比 | 同比增长中位数 | 上涨次数 | 趋势 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for c in companies:
        equity_bond = c["equity_bond"]
        current_buyin = equity_bond["current_buyin"]
        price_date = current_buyin["price_trade_date"]
        if price_date is None and current_buyin["price_kind"] == "previous_close":
            price_date = "上一交易日"
        lines.append(
            f"| {c['name']} | {current_buyin['pretax_profit_report_date'] or '—'} / "
            f"{price_date or '—'} | "
            f"{number(current_buyin['close_price'])}元 | "
            f"{number(current_buyin['pretax_profit_per_share'], 3)}元 | "
            f"{percent(current_buyin['pretax_earnings_yield'], 2)} | "
            f"{percent(equity_bond['pretax_profit_per_share_cagr'])} | "
            f"{percent(equity_bond['latest_pretax_profit_per_share_yoy_growth'])} | "
            f"{percent(equity_bond['pretax_profit_per_share_median_yoy_growth'])} | "
            f"{equity_bond['rising'] if equity_bond['rising'] is not None else '—'}/"
            f"{equity_bond['comparisons'] if equity_bond['comparisons'] is not None else '—'} | "
            f"{equity_bond_level_names[equity_bond['level']]} |"
        )
    lines.extend(
        [
            "",
            "> 注：当前买入成本税前收益率按“最新完整年报利润总额 ÷ 当前总股本 ÷ 最新已完成交易日收盘价”计算。盘中运行使用昨收，收盘后或休市日使用行情接口返回的最近完整收盘价；若实时行情未提供当前总股本，则回退到最新年报快照股本并在JSON中标记口径。该指标是静态近似值，不代表现金分红率或未来回报。CAGR反映历史首尾年化增长，同比中位数降低单个异常年份的影响；以上均为读取时派生，不写入数据库。",
        ]
    )

    level_names = {
        "strong": "强",
        "moderate": "温和",
        "not_met": "未满足",
        "insufficient": "数据不足",
    }
    margin_category_names = {
        "possible_competitive_advantage": "可能具相对优势",
        "gray_zone": "灰色地带",
        "highly_competitive": "高度竞争",
        "not_applicable": "金融类不适用",
        "insufficient_data": "数据不足",
    }
    financial_loan_structure_names = {
        "avoid": "短贷较多：回避信号",
        "not_triggered": "未触发",
        "not_applicable": "非金融类不适用",
        "insufficient_data": "数据不足",
    }
    debt_to_equity_category_names = {
        "below_0_8": "<0.8：较好",
        "at_or_above_0_8": "≥0.8",
        "not_applicable": "金融类不适用",
        "insufficient_data": "数据不足",
    }
    lines.extend(
        [
            "",
            "## 净利润与每股收益趋势",
            "",
            f"| 公司 | 净利润上涨次数 | 净利润CAGR | {end_year}净利率（排名） | {end_year}净利率分档 | 净利率≥20%年数 | EPS趋势 | EPS上涨次数 | EPS CAGR |",
            "|---|---:|---:|---:|---|---:|---:|---:|---:|",
        ]
    )
    for c in companies:
        n = c["net_profit"]
        eps = c["basic_eps"]
        lines.append(
            f"| {c['name']} | {n['rising']}/{n['comparisons']} | {percent(n['cagr'])} | "
            f"{percent(n['latest_margin'])}（{n.get('margin_rank', '—')}/{len(companies)}） | "
            f"{margin_category_names[n['latest_margin_category']]} | "
            f"{count_text(n['years_ge_20'], n['margin_available'], '')} | "
            f"{level_names[eps['level']]} | "
            f"{eps['rising'] if eps['rising'] is not None else '—'}/{eps['comparisons'] if eps['comparisons'] is not None else '—'} | "
            f"{percent(eps['cagr'])} |"
        )

    lines.extend(
        [
            "",
            "## 现金安全垫与外部输血线索",
            "",
            f"| 公司 | {end_year}货币资金/期末现金等价物 | 货币资金/总资产 | 货币资金/总负债 | 已披露融资债务下限（组件） | 持续盈利年数 | 吸收投资现金>0 | 处置长期资产现金>0 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for c in companies:
        cash = c["cash_safety"]
        lines.append(
            f"| {c['name']} | {hundred_million(cash['latest_monetary_funds'])} / "
            f"{hundred_million(cash['latest_ending_cash_equivalents'])} | "
            f"{percent(cash['latest_cash_to_assets'])} | "
            f"{percent(cash['latest_cash_to_total_liabilities'])} | "
            f"{hundred_million(cash['latest_known_financing_debt_lower_bound'])} "
            f"（{cash['latest_financing_debt_components_available']}/{cash['financing_debt_component_total']}） | "
            f"{count_text(cash['profitable_years'], cash['profit_available'], '')} | "
            f"{count_text(cash['equity_financing_positive_years'], cash['equity_financing_available'], '')} | "
            f"{count_text(cash['long_asset_disposal_positive_years'], cash['long_asset_disposal_available'], '')} |"
        )
    lines.extend(
        [
            "",
            "> 注：现金流项目未披露时保留为缺失，不能按零处理。因此“0/有数据年数”只说明已披露年度未出现正流入，不能单独证明十年从未出售股份或资产。吸收投资现金还可能包含子公司吸收少数股东投资，并不等同于上市公司发行股份。",
        ]
    )

    lines.extend(
        [
            "",
            "## 最近七年现金来源质量",
            "",
            "| 公司 | 经营净现金累计/为正年数 | 投资净现金累计 | 融资净现金累计 | 已知借款发债流入下限 | 已知股权融资流入下限 | 已知出售资产/业务流入下限 | 现金等价物净增加累计 | 现金勾稽通过 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for c in companies:
        cash_sources = c["seven_year_cash_sources"]
        lines.append(
            f"| {c['name']} | {hundred_million(cash_sources['cumulative_net_operating_cash'])} / "
            f"{count_text(cash_sources['positive_operating_cash_years'], cash_sources['operating_cash_available'], '')} | "
            f"{hundred_million(cash_sources['cumulative_net_investing_cash'])} | "
            f"{hundred_million(cash_sources['cumulative_net_financing_cash'])} | "
            f"{hundred_million(cash_sources['known_debt_financing_cash_inflow_lower_bound'])} | "
            f"{hundred_million(cash_sources['known_equity_financing_cash_inflow_lower_bound'])} | "
            f"{hundred_million(cash_sources['known_asset_business_sale_cash_inflow_lower_bound'])} | "
            f"{hundred_million(cash_sources['cumulative_cash_equivalents_increase'])} | "
            f"{count_text(cash_sources['cash_reconciliation_passed'], cash_sources['cash_reconciliation_available'], '')} |"
        )
    lines.extend(
        [
            "",
            "> 注：经营、投资、融资三类净现金流用于解释现金总变动；借款发债、股权融资及出售资产/业务是已披露具体流入的下限。东财未返回的现金流明细仍按缺失处理，不能把下限当成完整总额。“持续经营获得现金”要求七个年度经营现金流数据完整且每年均为正，但是否构成主要来源尚未设置比例阈值。",
        ]
    )

    lines.extend(
        [
            "",
            "## 债务结构与股东权益",
            "",
            f"| 公司 | {end_year}短期/长期贷款 | 金融机构短贷判断 | 已披露长期融资债务下限（组件） | 长期贷款/净利润（年） | 完整长期债务偿还年限 | 总负债/股东权益（低位排名） |",
            "|---|---:|---|---:|---:|---:|---:|",
        ]
    )
    for c in companies:
        debt = c["debt_structure"]
        lines.append(
            f"| {c['name']} | {hundred_million(debt['latest_short_loan'])} / "
            f"{hundred_million(debt['latest_long_loan'])} | "
            f"{financial_loan_structure_names[debt['financial_loan_structure']]} | "
            f"{hundred_million(debt['latest_known_long_term_debt_lower_bound'])} "
            f"（{debt['latest_long_term_debt_components_available']}/{debt['long_term_debt_component_total']}） | "
            f"{number(debt['latest_long_loan_payoff_years'])} | "
            f"{number(debt['latest_complete_long_term_debt_payoff_years'])} | "
            f"{number(debt['latest_debt_to_equity'])}（{debt.get('debt_to_equity_low_rank', '—')}/{len(companies)}；"
            f"{debt_to_equity_category_names[debt['debt_to_equity_category']]}） |"
        )
    lines.extend(
        [
            "",
            "> 注：长期融资债务暂按长期借款、应付债券、租赁负债和一年内到期的非流动负债四个候选项目核对。组件不完整时只展示已披露下限，不能据此断言完整长期债务可在3—4年偿清。金融机构短贷判断与债务股权比率例外需要通过 `--financial-symbol` 显式指定。",
        ]
    )

    lines.extend(
        [
            "",
            "## 留存收益、回购与权益回报",
            "",
            f"| 公司 | {end_year}留存收益/CAGR（排名） | 留存收益上涨次数/最大增幅 | {end_year}库存股/正值年数 | 回购相关股本减少事件/减少股数 | ROE均值/最新/最高（排名） | 财务杠杆均值/最新/最高（低位排名） |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for c in companies:
        equity = c["retained_earnings_and_equity_returns"]
        lines.append(
            f"| {c['name']} | {hundred_million(equity['latest_retained_earnings'])} / "
            f"{percent(equity['retained_earnings_cagr'])}（{equity.get('retained_earnings_cagr_rank', '—')}/{len(companies)}） | "
            f"{equity['retained_earnings_rising']}/{equity['retained_earnings_comparisons']} / "
            f"{percent(equity['maximum_retained_earnings_growth'])} | "
            f"{hundred_million(equity['latest_treasury_shares'])} / "
            f"{count_text(equity['treasury_shares_positive_years'], equity['treasury_shares_available'], '')} | "
            f"{equity['repurchase_share_reduction_count']}次 / "
            f"{ten_thousand_shares(equity['repurchase_share_reduction_total_shares'])} | "
            f"{percent(equity['return_on_equity_average'])} / {percent(equity['return_on_equity_latest'])} / "
            f"{percent(equity['return_on_equity_maximum'])}（{equity.get('return_on_equity_average_rank', '—')}/{len(companies)}） | "
            f"{number(equity['equity_multiplier_average'])} / {number(equity['equity_multiplier_latest'])} / "
            f"{number(equity['equity_multiplier_maximum'])}（{equity.get('equity_multiplier_low_rank', '—')}/{len(companies)}） |"
        )
    lines.extend(
        [
            "",
            "> 注：中国会计口径的留存收益按“盈余公积+未分配利润”计算。库存股是回购后尚在公司账面的抵减权益金额，不是股数；“回购相关股本减少”仅在东财股本变动原因为回购/注销且总股本同步下降时记录，作为注销证据，重要结论仍应核对公司公告。财务杠杆按总资产/股东权益计算，未设置“过高”阈值。",
        ]
    )

    lines.extend(
        [
            "",
            "## 股票回购计划与实际执行",
            "",
            "| 公司 | 区间内回购计划/公告年份 | 最新披露已回购金额/有效计划 | 最新披露已回购股数/有效计划 | 注销或减少注册资本意向 | 已确认股本减少事件/减少股数 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for c in companies:
        repurchase = c["retained_earnings_and_equity_returns"]
        lines.append(
            f"| {c['name']} | {repurchase['repurchase_program_count']} / "
            f"{repurchase['repurchase_program_announcement_years']} | "
            f"{hundred_million(repurchase['repurchase_actual_amount_total'])} / "
            f"{repurchase['repurchase_actual_amount_available']} | "
            f"{ten_thousand_shares(repurchase['repurchase_actual_shares_total'])} / "
            f"{repurchase['repurchase_actual_shares_available']} | "
            f"{repurchase['repurchase_cancellation_intent_count']} | "
            f"{repurchase['repurchase_share_reduction_count']} / "
            f"{ten_thousand_shares(repurchase['repurchase_share_reduction_total_shares'])} |"
        )
    lines.extend(
        [
            "",
            "> 注：回购金额和股数来自东财回购计划的最新披露执行结果，按计划公告日在所选区间内筛选；跨年度计划不能据此精确拆分到每个自然年的现金流，也不是历史报告日的点时快照。计划、实际执行、库存股和最终注销是四类不同证据；金额缺失只累计已披露部分，不能按零处理。",
        ]
    )

    preferred_stock_names = {
        "detected": "发现优先股余额",
        "not_detected_in_complete_data": "完整数据中未见余额",
        "insufficient_data": "数据不足，需查附注",
    }
    lines.extend(
        [
            "",
            "## 优先股与资本结构",
            "",
            f"| 公司 | {end_year}权益类优先股 | {end_year}负债类优先股 | 检出年度数 | 有效字段/应有字段 | 结论 |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for c in companies:
        preferred = c["preferred_stock_capital_structure"]
        lines.append(
            f"| {c['name']} | "
            f"{hundred_million(preferred['latest_equity_classified_preferred_shares'])} | "
            f"{hundred_million(preferred['latest_liability_classified_preferred_shares'])} | "
            f"{preferred['years_with_detected_preferred_shares']} | "
            f"{preferred['available_cells']}/{preferred['expected_cells']} | "
            f"{preferred_stock_names[preferred['classification']]} |"
        )
    lines.extend(
        [
            "",
            "> 注：优先股可能依合同经济实质被列为权益工具或金融负债，因此同时检查“其他权益工具—优先股”和“应付债券—优先股”。只有两类字段在全部所选年报中均有明确数值且均为零，才输出“完整数据中未见余额”；空值不按零处理。三张表仍不能替代优先股发行、赎回及转换条款的年报附注和公告。",
        ]
    )

    lines.extend(
        [
            "",
            "## 资产负债表经营资产指标",
            "",
            f"| 公司 | {end_year}存货 | 存货最大增幅/最大降幅 | 存货增长时净利润同增 | 应收账款/销售均值/最新（低位排名） | ROA均值/最新/最高 | 流动比率均值/最新 | <1年数 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for c in companies:
        inv = c["inventory_profit_alignment"]
        ar = c["accounts_receivable_to_sales"]
        roa = c["return_on_assets"]
        current = c["current_ratio"]
        lines.append(
            f"| {c['name']} | {hundred_million(inv['latest_inventory'])} | "
            f"{percent(inv['maximum_inventory_growth'])} / {percent(inv['maximum_inventory_decline'])} | "
            f"{inv['inventory_and_profit_both_growth']}/{inv['inventory_growth_with_profit_comparable']} | "
            f"{percent(ar['average'], 2)} / {percent(ar['latest'], 2)}（{ar.get('average_low_rank', '—')}/{len(companies)}） | "
            f"{percent(roa['average'])} / {percent(roa['latest'])} / {percent(roa['maximum'])} | "
            f"{number(current['average'])} / {number(current['latest'])} | "
            f"{count_text(current['years_lt_1'], current['available'], '')} |"
        )

    lines.extend(
        [
            "",
            "## 固定资产与已确认无形资产",
            "",
            f"| 公司 | {end_year}固定资产 | 固定资产平均绝对变动/最大增幅/最大降幅 | {end_year}长期资产购建现金 | {end_year}无形资产/总资产 | {end_year}商誉 | 商誉上涨次数/当前连续/最长连续 | 取得子公司现金累计/正值年数 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for c in companies:
        fixed = c["fixed_asset_stability"]
        intangible = c["recognized_intangible_assets"]
        lines.append(
            f"| {c['name']} | {hundred_million(fixed['latest_fixed_assets'])} | "
            f"{percent(fixed['average_absolute_yoy_change'])} / {percent(fixed['maximum_growth'])} / {percent(fixed['maximum_decline'])} | "
            f"{hundred_million(fixed['latest_long_asset_capex'])} | "
            f"{hundred_million(intangible['latest'])} / {percent(intangible['latest_to_total_assets'])} | "
            f"{hundred_million(intangible['latest_goodwill'])} | "
            f"{intangible['goodwill_rising']}/{intangible['goodwill_comparisons']} / "
            f"{intangible['goodwill_current_increase_streak']} / {intangible['goodwill_longest_increase_streak']} | "
            f"{hundred_million(intangible['subsidiary_acquisition_cash_total'])} / "
            f"{count_text(intangible['subsidiary_acquisition_cash_positive_years'], intangible['subsidiary_acquisition_cash_available'], '')} |"
        )
    lines.extend(
        [
            "",
            "> 注：存货“迅速增减”、固定资产“稳定”和更新支出“巨额”尚无阈值，表中只展示可复核的变化率。应收账款低位排名仅在本次显式传入的公司之间比较。商誉连续增加是并购线索，并用取得子公司现金交叉验证；仍不能据此判断被收购企业具有持续竞争优势。账面无形资产和商誉不代表未入账的品牌、网络效应或持续竞争优势。",
        ]
    )

    capex_classification_names = {
        "below_25": "<25%，较强线索",
        "below_50": "25%—<50%，候选",
        "at_or_above_50": "≥50%，未满足",
        "insufficient_data": "数据不足",
    }
    lines.extend(
        [
            "",
            "## 最近十年累计资本开支负担",
            "",
            "| 公司 | 十年累计资本开支 | 十年累计净利润 | 资本开支/净利润 | 分档 | 十年数据完整 |",
            "|---|---:|---:|---:|---|---|",
        ]
    )
    for c in companies:
        capex = c["fixed_asset_stability"]
        lines.append(
            f"| {c['name']} | {hundred_million(capex['cumulative_ten_year_capex'])} | "
            f"{hundred_million(capex['cumulative_ten_year_net_profit'])} | "
            f"{percent(capex['ten_year_capex_to_net_profit'])} | "
            f"{capex_classification_names[capex['ten_year_capex_to_net_profit_classification']]} | "
            f"{'是' if capex['capex_profit_complete_ten_years'] else '否'} |"
        )
    lines.extend(
        [
            "",
            "> 注：资本开支采用现金流量表“购建固定资产、无形资产和其他长期资产支付的现金”，不扣除资产处置收入；净利润采用同年度 `NETPROFIT`。只有最近十个年报两项数据全部完整且累计净利润大于零才计算。严格 `<25%` 标记为较强线索，`25%—<50%` 标记为候选，`≥50%` 为未满足；该单项不能替代对增长性、并购支出和维护性/扩张性资本开支的分析。",
        ]
    )

    lines.extend(
        [
            "",
            "## 长期投资账面项目",
            "",
            f"| 公司 | {end_year}长期股权投资/总资产/CAGR | 其他权益工具投资 | 其他非流动金融资产 | 债权投资 | 其他债权投资 | 旧准则：可供出售/持有至到期 | 投资收益/联营合营投资收益 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for c in companies:
        investments = c["long_term_investments"]
        lines.append(
            f"| {c['name']} | {hundred_million(investments['latest_long_equity_investment'])} / "
            f"{percent(investments['long_equity_investment_to_total_assets'])} / "
            f"{percent(investments['long_equity_investment_cagr'])} | "
            f"{hundred_million(investments['latest_other_equity_investment'])} | "
            f"{hundred_million(investments['latest_other_noncurrent_financial_assets'])} | "
            f"{hundred_million(investments['latest_creditor_investment'])} | "
            f"{hundred_million(investments['latest_other_creditor_investment'])} | "
            f"{hundred_million(investments['latest_available_for_sale_financial_assets'])} / "
            f"{hundred_million(investments['latest_held_to_maturity_investment'])} | "
            f"{hundred_million(investments['latest_investment_income'])} / "
            f"{hundred_million(investments['latest_joint_venture_investment_income'])} |"
        )
    lines.extend(
        [
            "",
            "> 注：这些项目可能分别采用成本法、权益法、摊余成本或公允价值计量，不能相加后统一解释为“成本与市价孰低”。三张表不包含完整被投企业名单和持股明细，账面金额也不能直接揭示潜在市场价值；判断被投企业是否具有持续竞争优势必须读取年报附注并对被投企业单独分析。",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.start_year > args.end_year:
        raise SystemExit("--start-year cannot be later than --end-year")
    if args.price_timeout <= 0:
        raise SystemExit("--price-timeout must be positive")
    if not 1 <= args.price_retries <= 5:
        raise SystemExit("--price-retries must be between 1 and 5")
    normalized_symbols = [symbol.upper() for symbol in args.symbols]
    current_quotes: dict[str, dict[str, Any]] = {}
    for symbol in dict.fromkeys(normalized_symbols):
        if args.skip_current_price:
            current_quotes[symbol] = {"error": "current price refresh skipped"}
            continue
        try:
            current_quotes[symbol] = fetch_latest_completed_close(
                symbol,
                timeout=args.price_timeout,
                retries=args.price_retries,
            )
        except EastmoneyError as exc:
            current_quotes[symbol] = {"error": str(exc)}
            print(
                f"warning: current price unavailable for {symbol}: {exc}",
                file=sys.stderr,
            )
    connection = sqlite3.connect(args.database)
    financial_symbols = {symbol.upper() for symbol in args.financial_symbol}
    try:
        companies = [
            load_company(
                connection,
                symbol,
                args.start_year,
                args.end_year,
                is_financial=symbol in financial_symbols,
                current_quote=current_quotes[symbol],
            )
            for symbol in normalized_symbols
        ]
    finally:
        connection.close()
    rank(companies, ("pretax_profit", "latest"), "rank")
    rank(companies, ("net_profit", "latest_margin"), "margin_rank")
    rank(
        companies,
        ("accounts_receivable_to_sales", "average"),
        "average_low_rank",
        reverse=False,
    )
    rank(
        companies,
        ("retained_earnings_and_equity_returns", "retained_earnings_cagr"),
        "retained_earnings_cagr_rank",
    )
    rank(
        companies,
        ("retained_earnings_and_equity_returns", "return_on_equity_average"),
        "return_on_equity_average_rank",
    )
    rank(
        companies,
        ("retained_earnings_and_equity_returns", "equity_multiplier_average"),
        "equity_multiplier_low_rank",
        reverse=False,
    )
    rank(
        companies,
        ("debt_structure", "latest_debt_to_equity"),
        "debt_to_equity_low_rank",
        reverse=False,
    )
    json_output = json.dumps(json_ready(companies), ensure_ascii=False, indent=2)
    markdown_output = markdown(companies, args.start_year, args.end_year)
    output = json_output if args.format == "json" else markdown_output
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json_output + "\n", encoding="utf-8")
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(markdown_output + "\n", encoding="utf-8")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
