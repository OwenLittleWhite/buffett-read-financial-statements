#!/usr/bin/env python3
"""Fetch up to twenty fiscal years of Eastmoney A-share financial statements.

Outputs a raw JSON snapshot, one wide CSV per statement, and a normalized SQLite
database. The implementation uses the Python standard library and can fall back
to the local curl executable when Eastmoney closes urllib TLS connections.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html as html_lib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence


BASE_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis"
SOURCE_PAGE = "https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html"
KLINE_URLS = (
    "https://28.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://46.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://50.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://61.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://72.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://7.push2his.eastmoney.com/api/qt/stock/kline/get",
    "http://push2his.eastmoney.com/api/qt/stock/kline/get",
)
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
CAPITAL_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CapitalStockStructure/PageAjax"
CAPITAL_HISTORY_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
REPURCHASE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CAPITAL_HISTORY_COLUMNS = (
    "SECUCODE,SECURITY_CODE,END_DATE,TOTAL_SHARES,LIMITED_SHARES,LIMITED_OTHARS,"
    "LIMITED_DOMESTIC_NATURAL,LIMITED_STATE_LEGAL,LIMITED_OVERSEAS_NOSTATE,"
    "LIMITED_OVERSEAS_NATURAL,UNLIMITED_SHARES,LISTED_A_SHARES,B_FREE_SHARE,"
    "H_FREE_SHARE,FREE_SHARES,LIMITED_A_SHARES,NON_FREE_SHARES,LIMITED_B_SHARES,"
    "OTHER_FREE_SHARES,LIMITED_STATE_SHARES,LIMITED_DOMESTIC_NOSTATE,LOCK_SHARES,"
    "LIMITED_FOREIGN_SHARES,LIMITED_H_SHARES,SPONSOR_SHARES,STATE_SPONSOR_SHARES,"
    "SPONSOR_SOCIAL_SHARES,RAISE_SHARES,RAISE_STATE_SHARES,RAISE_DOMESTIC_SHARES,"
    "RAISE_OVERSEAS_SHARES,CHANGE_REASON"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)
MAX_FISCAL_YEARS = 20
REQUEST_DELAY_SECONDS = 2
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data"
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

STATEMENTS = {
    "balance": ("zcfzbDateAjaxNew", "zcfzbAjaxNew"),
    "income": ("lrbDateAjaxNew", "lrbAjaxNew"),
    "cashflow": ("xjllbDateAjaxNew", "xjllbAjaxNew"),
}

STATEMENT_TEMPLATE_PREFIXES = {
    "balance": "zcfzb",
    "income": "lrb",
    "cashflow": "xjllb",
}

PERIOD_ENDS = {
    "annual": (12, 31),
    "q1": (3, 31),
    "half": (6, 30),
    "q3": (9, 30),
}

REPORT_METADATA_FIELDS = {
    "SECUCODE",
    "SECURITY_CODE",
    "SECURITY_NAME_ABBR",
    "ORG_CODE",
    "ORG_TYPE",
    "REPORT_DATE",
    "REPORT_TYPE",
    "REPORT_DATE_NAME",
    "SECURITY_TYPE_CODE",
    "NOTICE_DATE",
    "UPDATE_DATE",
    "CURRENCY",
}


class EastmoneyError(RuntimeError):
    """Raised when Eastmoney returns an unusable response."""


class DecimalJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


class CompanyTypeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.company_type: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "input":
            return
        values = dict(attrs)
        if values.get("id") == "hidctype" and values.get("value"):
            self.company_type = values["value"]


@dataclass(frozen=True)
class FiscalRange:
    start_year: int
    end_year: int

    def contains(self, report_date: str) -> bool:
        year = int(report_date[:4])
        return self.start_year <= year <= self.end_year


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Eastmoney balance sheet, income statement, and cash-flow "
            "statement data for a range of no more than twenty fiscal years."
        )
    )
    parser.add_argument(
        "symbol",
        help="A-share symbol such as SH600809, 600809.SH, or 600809",
    )
    parser.add_argument(
        "--years",
        type=int,
        help="Number of fiscal years ending at --end-year (default: 10)",
    )
    parser.add_argument("--start-year", type=int, help="First fiscal year, inclusive")
    parser.add_argument("--end-year", type=int, help="Last fiscal year, inclusive")
    parser.add_argument(
        "--period",
        choices=("annual", "all", "q1", "half", "q3"),
        default="all",
        help="Collection filter for report periods (default: all)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Maximum attempts per HTTP request (default: 3)",
    )
    return parser


def normalize_symbol(raw_symbol: str) -> str:
    symbol = raw_symbol.strip().upper()
    match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", symbol)
    if match:
        return f"{match.group(1)}{match.group(2)}"

    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", symbol)
    if match:
        return f"{match.group(2)}{match.group(1)}"

    if re.fullmatch(r"\d{6}", symbol):
        if symbol[0] in {"5", "6", "9"}:
            market = "SH"
        elif symbol[0] in {"4", "8"}:
            market = "BJ"
        else:
            market = "SZ"
        return f"{market}{symbol}"

    raise ValueError("symbol must look like SH600809, 600809.SH, or 600809")


def resolve_fiscal_range(args: argparse.Namespace) -> FiscalRange:
    current_year = datetime.now().year
    if args.start_year is not None:
        if args.end_year is None:
            raise ValueError("--start-year requires --end-year")
        if args.years is not None:
            raise ValueError("use either --years or --start-year/--end-year, not both")
        start_year, end_year = args.start_year, args.end_year
    else:
        years = 10 if args.years is None else args.years
        if not 1 <= years <= MAX_FISCAL_YEARS:
            raise ValueError(f"--years must be between 1 and {MAX_FISCAL_YEARS}")
        end_year = current_year if args.end_year is None else args.end_year
        start_year = end_year - years + 1

    if start_year > end_year:
        raise ValueError("--start-year cannot be later than --end-year")
    if end_year - start_year + 1 > MAX_FISCAL_YEARS:
        raise ValueError(f"fiscal range cannot exceed {MAX_FISCAL_YEARS} years")
    if start_year < 1990 or end_year > current_year:
        raise ValueError(f"fiscal years must be between 1990 and {current_year}")
    return FiscalRange(start_year, end_year)


def request_bytes(
    url: str,
    params: dict[str, str],
    *,
    timeout: float,
    retries: int,
    referer: str | None = None,
) -> bytes:
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}" if query else url
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(full_url, headers=headers)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        open_request = urllib.request.urlopen if attempt % 2 else DIRECT_OPENER.open
        try:
            with open_request(request, timeout=timeout) as response:
                if response.status != 200:
                    raise EastmoneyError(f"HTTP {response.status} for {full_url}")
                body = response.read()
                # Some Eastmoney endpoints occasionally gzip the payload even
                # when the client requests identity encoding.
                if response.headers.get("Content-Encoding", "").lower() == "gzip" or body.startswith(
                    b"\x1f\x8b"
                ):
                    body = gzip.decompress(body)
                time.sleep(REQUEST_DELAY_SECONDS)
                return body
        except (urllib.error.URLError, TimeoutError, OSError, EastmoneyError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.6 * (2 ** (attempt - 1)))

    curl_path = shutil.which("curl")
    if curl_path:
        curl_command = [
            curl_path,
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--retry",
            "3",
            "--retry-all-errors",
            "--retry-delay",
            str(REQUEST_DELAY_SECONDS),
            "--max-time",
            str(timeout),
            "--user-agent",
            USER_AGENT,
            "--header",
            "Accept: application/json,text/plain,text/html;q=0.9,*/*;q=0.8",
            "--header",
            "Accept-Language: zh-CN,zh;q=0.9",
            "--header",
            "Accept-Encoding: identity",
        ]
        if referer:
            curl_command.extend(("--referer", referer))
        curl_command.append(full_url)
        try:
            completed = subprocess.run(
                curl_command,
                check=False,
                capture_output=True,
                timeout=timeout * 4 + 10,
            )
            if completed.returncode == 0 and completed.stdout:
                body = completed.stdout
                if body.startswith(b"\x1f\x8b"):
                    body = gzip.decompress(body)
                time.sleep(REQUEST_DELAY_SECONDS)
                return body
            curl_error = completed.stderr.decode("utf-8", errors="replace").strip()
            last_error = EastmoneyError(
                f"curl exited with {completed.returncode}: {curl_error}"
            )
        except (OSError, subprocess.TimeoutExpired, gzip.BadGzipFile) as exc:
            last_error = exc
    raise EastmoneyError(f"request failed after {retries} attempts: {full_url}: {last_error}")


def fetch_json(
    url: str,
    params: dict[str, str],
    *,
    timeout: float,
    retries: int,
    referer: str,
) -> dict[str, Any]:
    body = request_bytes(
        url,
        params,
        timeout=timeout,
        retries=retries,
        referer=referer,
    )
    try:
        payload = json.loads(body.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EastmoneyError(f"invalid JSON from {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EastmoneyError(f"unexpected JSON shape from {url}")
    return payload


def fetch_company_context(
    symbol: str, *, timeout: float, retries: int
) -> tuple[str, str, str]:
    index_url = f"{BASE_URL}/Index"
    body = request_bytes(
        index_url,
        {"type": "web", "code": symbol.lower()},
        timeout=timeout,
        retries=retries,
    )
    page_html = body.decode("utf-8", errors="replace")
    parser = CompanyTypeParser()
    parser.feed(page_html)
    if not parser.company_type:
        raise EastmoneyError("could not determine Eastmoney companyType")
    return (
        parser.company_type,
        f"{index_url}?type=web&code={symbol.lower()}",
        page_html,
    )


def clean_template_text(fragment: str) -> str:
    text = re.sub(r"\{\{.*?\}\}", " ", fragment, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def extract_item_definitions(page_html: str) -> list[dict[str, Any]]:
    """Extract source labels and order from Eastmoney's statement templates."""
    scripts = list(
        re.finditer(
            r'<script\b[^>]*id=["\']([^"\']+)["\'][^>]*>(.*?)</script>',
            page_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    definitions: list[dict[str, Any]] = []
    for statement, prefix in STATEMENT_TEMPLATE_PREFIXES.items():
        candidates = [
            (match.group(1), match.group(2))
            for match in scripts
            if match.group(1).startswith(f"{prefix}_")
            and not match.group(1).endswith(("_tb", "_hb"))
        ]
        if not candidates:
            raise EastmoneyError(f"could not find Eastmoney template for {statement}")

        template_id, template_body = candidates[0]
        display_order = 0
        section_name: str | None = None
        seen_fields: set[str] = set()
        for row_html in re.findall(
            r"<tr\b[^>]*>(.*?)</tr>", template_body, flags=re.IGNORECASE | re.DOTALL
        ):
            first_cell = re.search(
                r"<td\b[^>]*>(.*?)</td>", row_html, flags=re.IGNORECASE | re.DOTALL
            )
            if not first_cell:
                continue
            display_name = clean_template_text(first_cell.group(1))
            item_codes = list(dict.fromkeys(re.findall(r"value\.([A-Z][A-Z0-9_]*)", row_html)))
            if not item_codes:
                if display_name and display_name != "暂无数据":
                    section_name = display_name
                continue

            item_code = item_codes[0]
            if item_code in seen_fields:
                continue
            seen_fields.add(item_code)
            display_order += 1
            definitions.append(
                {
                    "statement": statement,
                    "item_code": item_code,
                    "display_name": display_name or item_code,
                    "display_order": display_order,
                    "section_name": section_name,
                    "source_template": template_id,
                }
            )
    return definitions


def normalize_report_date(value: Any) -> str:
    text = str(value or "")[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise EastmoneyError(f"invalid REPORT_DATE returned by Eastmoney: {value!r}") from exc


def period_matches(report_date: str, period: str) -> bool:
    if period == "all":
        return True
    parsed = datetime.strptime(report_date, "%Y-%m-%d").date()
    return (parsed.month, parsed.day) == PERIOD_ENDS[period]


def chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def fetch_statement(
    statement: str,
    symbol: str,
    company_type: str,
    fiscal_range: FiscalRange,
    period: str,
    *,
    timeout: float,
    retries: int,
    referer: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    date_endpoint, data_endpoint = STATEMENTS[statement]
    common_params = {
        "companyType": company_type,
        "reportDateType": "0",
        "code": symbol,
    }
    date_payload = fetch_json(
        f"{BASE_URL}/{date_endpoint}",
        common_params,
        timeout=timeout,
        retries=retries,
        referer=referer,
    )
    date_rows = date_payload.get("data")
    if not isinstance(date_rows, list):
        raise EastmoneyError(f"{statement} date endpoint returned no data list")

    selected_dates: list[str] = []
    selected_date_rows: list[dict[str, Any]] = []
    for row in date_rows:
        if not isinstance(row, dict) or not row.get("REPORT_DATE"):
            continue
        report_date = normalize_report_date(row["REPORT_DATE"])
        if fiscal_range.contains(report_date) and period_matches(report_date, period):
            selected_dates.append(report_date)
            selected_date_rows.append(row)

    selected_dates = sorted(set(selected_dates), reverse=True)
    records: list[dict[str, Any]] = []
    request_log: list[dict[str, Any]] = []
    for date_batch in chunks(selected_dates, 5):
        params = {
            **common_params,
            "reportType": "1",
            "dates": ",".join(date_batch),
        }
        payload = fetch_json(
            f"{BASE_URL}/{data_endpoint}",
            params,
            timeout=timeout,
            retries=retries,
            referer=referer,
        )
        batch_records = payload.get("data")
        if not isinstance(batch_records, list):
            raise EastmoneyError(f"{statement} data endpoint returned no data list")
        records.extend(row for row in batch_records if isinstance(row, dict))
        request_log.append({"endpoint": data_endpoint, "params": params, "rows": len(batch_records)})
        time.sleep(0.2)

    records.sort(key=lambda row: normalize_report_date(row.get("REPORT_DATE")), reverse=True)
    return records, request_log


def collect_report_periods(
    statements: dict[str, list[dict[str, Any]]]
) -> dict[str, str | None]:
    periods: dict[str, str | None] = {}
    for records in statements.values():
        for record in records:
            report_date = normalize_report_date(record.get("REPORT_DATE"))
            report_type = record.get("REPORT_TYPE")
            existing = periods.get(report_date)
            if existing and report_type and existing != report_type:
                raise EastmoneyError(
                    f"conflicting REPORT_TYPE values for {report_date}: {existing} / {report_type}"
                )
            periods[report_date] = str(report_type) if report_type is not None else existing
    return dict(sorted(periods.items(), reverse=True))


def eastmoney_secid(symbol: str) -> str:
    market_id = "1" if symbol.startswith("SH") else "0"
    return f"{market_id}.{symbol[2:]}"


def fetch_tencent_daily_klines(
    symbol: str,
    range_start_date: date,
    range_end_date: date,
    *,
    timeout: float,
    retries: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch unadjusted daily closes when all Eastmoney K-line hosts fail.

    Tencent's ``bfq`` rows provide OHLC and volume but omit amount, amplitude,
    change, and turnover fields. Preserve those omissions as ``None`` instead
    of silently inventing zero values.
    """

    quote_symbol = symbol.lower()
    raw_rows: list[list[Any]] = []
    request_log: list[dict[str, Any]] = []
    batch_start_year = range_start_date.year
    while batch_start_year <= range_end_date.year:
        batch_end_year = min(batch_start_year + 1, range_end_date.year)
        batch_start_date = (
            range_start_date
            if batch_start_year == range_start_date.year
            else date(batch_start_year, 1, 1)
        )
        batch_end_date = (
            range_end_date
            if batch_end_year == range_end_date.year
            else date(batch_end_year, 12, 31)
        )
        param = (
            f"{quote_symbol},day,{batch_start_date.isoformat()},"
            f"{batch_end_date.isoformat()},640,bfq"
        )
        payload = fetch_json(
            TENCENT_KLINE_URL,
            {"param": param},
            timeout=timeout,
            retries=retries,
            referer="https://gu.qq.com/",
        )
        data = payload.get("data")
        symbol_data = data.get(quote_symbol) if isinstance(data, dict) else None
        batch_rows = symbol_data.get("day") if isinstance(symbol_data, dict) else None
        if not isinstance(batch_rows, list) or not batch_rows:
            raise EastmoneyError(
                "Tencent unadjusted daily K-line fallback returned no data for "
                f"{batch_start_date.isoformat()}-{batch_end_date.isoformat()}"
            )
        raw_rows.extend(batch_rows)
        request_log.append(
            {
                "endpoint": TENCENT_KLINE_URL,
                "params": {"param": param},
                "rows": len(batch_rows),
                "source": "tencent_unadjusted_fallback",
            }
        )
        batch_start_year = batch_end_year + 1

    klines: list[dict[str, Any]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, list) or len(raw_row) < 6:
            raise EastmoneyError(f"unexpected Tencent K-line row: {raw_row!r}")
        klines.append(
            {
                "trade_date": normalize_report_date(raw_row[0]),
                "open_price": Decimal(str(raw_row[1])),
                "close_price": Decimal(str(raw_row[2])),
                "high_price": Decimal(str(raw_row[3])),
                "low_price": Decimal(str(raw_row[4])),
                "volume_lots": int(Decimal(str(raw_row[5]))),
                "amount": None,
                "amplitude": None,
                "change_percent": None,
                "change_amount": None,
                "turnover_rate": None,
                "raw_line": json.dumps(raw_row, ensure_ascii=False),
                "source": "tencent_unadjusted_fallback",
            }
        )
    deduplicated = {row["trade_date"]: row for row in klines}
    return sorted(deduplicated.values(), key=lambda row: row["trade_date"]), request_log


def fetch_daily_klines(
    symbol: str,
    report_dates: Sequence[str],
    *,
    timeout: float,
    retries: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not report_dates:
        return [], []
    first_report_date = datetime.strptime(min(report_dates), "%Y-%m-%d").date()
    range_start_date = first_report_date - timedelta(days=14)
    last_report_date = max(report_dates)
    start_year = max(1990, range_start_date.year)
    end_year = int(last_report_date[:4])
    raw_klines: list[Any] = []
    request_log: list[dict[str, Any]] = []
    request_span_years = MAX_FISCAL_YEARS + 1
    for batch_start_year in range(start_year, end_year + 1, request_span_years):
        batch_end_year = min(batch_start_year + request_span_years - 1, end_year)
        batch_end_date = min(f"{batch_end_year}1231", last_report_date.replace("-", ""))
        params = {
            "secid": eastmoney_secid(symbol),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "0",
            "beg": (
                range_start_date.strftime("%Y%m%d")
                if batch_start_year == start_year
                else f"{batch_start_year}0101"
            ),
            "end": batch_end_date,
            "lmt": "1000000",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
        }
        payload: dict[str, Any] | None = None
        actual_endpoint: str | None = None
        endpoint_errors: list[str] = []
        for endpoint in KLINE_URLS:
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
                endpoint_errors.append(str(exc))
        if payload is None or actual_endpoint is None:
            print(
                "warning: all Eastmoney daily K-line endpoints failed; "
                "using Tencent unadjusted daily-close fallback",
                file=sys.stderr,
            )
            return fetch_tencent_daily_klines(
                symbol,
                range_start_date,
                datetime.strptime(last_report_date, "%Y-%m-%d").date(),
                timeout=timeout,
                retries=retries,
            )
        data = payload.get("data")
        batch_klines = data.get("klines") if isinstance(data, dict) else None
        if not isinstance(batch_klines, list) or not batch_klines:
            raise EastmoneyError(
                f"Eastmoney daily K-line endpoint returned no data for {batch_start_year}-{batch_end_year}"
            )
        raw_klines.extend(batch_klines)
        request_log.append(
            {"endpoint": actual_endpoint, "params": params, "rows": len(batch_klines)}
        )

    klines: list[dict[str, Any]] = []
    for raw_line in raw_klines:
        values = str(raw_line).split(",")
        if len(values) != 11:
            raise EastmoneyError(f"unexpected K-line field count: {raw_line!r}")
        klines.append(
            {
                "trade_date": normalize_report_date(values[0]),
                "open_price": Decimal(values[1]),
                "close_price": Decimal(values[2]),
                "high_price": Decimal(values[3]),
                "low_price": Decimal(values[4]),
                "volume_lots": int(Decimal(values[5])),
                "amount": Decimal(values[6]),
                "amplitude": Decimal(values[7]),
                "change_percent": Decimal(values[8]),
                "change_amount": Decimal(values[9]),
                "turnover_rate": Decimal(values[10]),
                "raw_line": str(raw_line),
            }
        )
    deduplicated = {row["trade_date"]: row for row in klines}
    klines = sorted(deduplicated.values(), key=lambda row: row["trade_date"])
    return klines, request_log


def fetch_capital_changes(
    symbol: str,
    *,
    timeout: float,
    retries: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    secu_code = f"{symbol[2:]}.{symbol[:2]}"
    params = {
        "reportName": "RPT_F10_EH_EQUITY",
        "columns": CAPITAL_HISTORY_COLUMNS,
        "quoteColumns": "",
        "filter": f'(SECUCODE="{secu_code}")',
        "pageNumber": "1",
        "pageSize": "500",
        "sortTypes": "-1",
        "sortColumns": "END_DATE",
        "source": "HSF10",
        "client": "PC",
    }
    payload = fetch_json(
        CAPITAL_HISTORY_URL,
        params,
        timeout=timeout,
        retries=retries,
        referer="https://emweb.securities.eastmoney.com/",
    )
    result = payload.get("result")
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows:
        raise EastmoneyError("Eastmoney capital-history endpoint returned no history")
    total_pages = int(result.get("pages") or 1)
    all_rows = list(rows)
    for page_number in range(2, total_pages + 1):
        page_params = {**params, "pageNumber": str(page_number)}
        page_payload = fetch_json(
            CAPITAL_HISTORY_URL,
            page_params,
            timeout=timeout,
            retries=retries,
            referer="https://emweb.securities.eastmoney.com/",
        )
        page_result = page_payload.get("result")
        page_rows = page_result.get("data") if isinstance(page_result, dict) else None
        if isinstance(page_rows, list):
            all_rows.extend(page_rows)
    changes = [
        row
        for row in all_rows
        if isinstance(row, dict) and row.get("END_DATE") and row.get("TOTAL_SHARES") is not None
    ]
    changes.sort(key=lambda row: normalize_report_date(row["END_DATE"]))
    if not changes:
        raise EastmoneyError("Eastmoney capital-change history contains no usable rows")
    return changes, {
        "endpoint": CAPITAL_HISTORY_URL,
        "params": params,
        "pages": total_pages,
        "rows": len(changes),
    }


def fetch_repurchase_programs(
    symbol: str,
    *,
    timeout: float,
    retries: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = {
        "reportName": "RPTA_WEB_GETHGLIST_NEW",
        "columns": "ALL",
        "filter": f'(DIM_SCODE="{symbol[2:]}")',
        "pageNumber": "1",
        "pageSize": "500",
        "sortColumns": "UPD,DIM_DATE,DIM_SCODE",
        "sortTypes": "-1,-1,-1",
        "source": "WEB",
        "client": "WEB",
    }
    payload = fetch_json(
        REPURCHASE_URL,
        params,
        timeout=timeout,
        retries=retries,
        referer="https://data.eastmoney.com/gphg/",
    )
    result = payload.get("result")
    if result is None:
        return [], {
            "endpoint": REPURCHASE_URL,
            "params": params,
            "pages": 0,
            "rows": 0,
        }
    if not isinstance(result, dict):
        raise EastmoneyError("Eastmoney repurchase endpoint returned invalid result")
    rows = result.get("data") or []
    if not isinstance(rows, list):
        raise EastmoneyError("Eastmoney repurchase endpoint returned invalid data")
    total_pages = int(result.get("pages") or 1)
    all_rows = list(rows)
    for page_number in range(2, total_pages + 1):
        page_params = {**params, "pageNumber": str(page_number)}
        page_payload = fetch_json(
            REPURCHASE_URL,
            page_params,
            timeout=timeout,
            retries=retries,
            referer="https://data.eastmoney.com/gphg/",
        )
        page_result = page_payload.get("result")
        page_rows = page_result.get("data") if isinstance(page_result, dict) else None
        if not isinstance(page_rows, list):
            raise EastmoneyError(
                f"Eastmoney repurchase endpoint returned invalid page {page_number}"
            )
        all_rows.extend(page_rows)
    programs = [
        row
        for row in all_rows
        if isinstance(row, dict) and row.get("REPURCODE") and row.get("DIM_DATE")
    ]
    programs.sort(
        key=lambda row: (
            normalize_report_date(row["DIM_DATE"]),
            str(row["REPURCODE"]),
        )
    )
    return programs, {
        "endpoint": REPURCHASE_URL,
        "params": params,
        "pages": total_pages,
        "rows": len(programs),
    }


def as_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(Decimal(str(value)))


def build_report_market_snapshots(
    report_periods: dict[str, str | None],
    klines: list[dict[str, Any]],
    capital_changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for report_date, report_type in report_periods.items():
        eligible_quotes = [row for row in klines if row["trade_date"] <= report_date]
        if not eligible_quotes:
            raise EastmoneyError(f"no trading day found on or before {report_date}")
        quote = eligible_quotes[-1]

        eligible_capital = [
            row
            for row in capital_changes
            if normalize_report_date(row["END_DATE"]) <= quote["trade_date"]
        ]
        if not eligible_capital:
            raise EastmoneyError(
                f"no capital record effective on or before {quote['trade_date']}"
            )
        capital = eligible_capital[-1]

        total_shares = as_int_or_none(capital.get("TOTAL_SHARES"))
        circulating_shares = as_int_or_none(capital.get("UNLIMITED_SHARES"))
        if total_shares is None:
            raise EastmoneyError(f"TOTAL_SHARES is missing for {report_date}")
        close_price = quote["close_price"]
        market_cap = close_price * Decimal(total_shares)
        circulating_market_cap = (
            close_price * Decimal(circulating_shares)
            if circulating_shares is not None
            else None
        )
        snapshots.append(
            {
                "report_date": report_date,
                "report_type": report_type,
                "trade_date": quote["trade_date"],
                "share_effective_date": normalize_report_date(capital["END_DATE"]),
                "open_price": quote["open_price"],
                "close_price": close_price,
                "high_price": quote["high_price"],
                "low_price": quote["low_price"],
                "volume_lots": quote["volume_lots"],
                "amount": quote["amount"],
                "amplitude": quote["amplitude"],
                "change_percent": quote["change_percent"],
                "change_amount": quote["change_amount"],
                "turnover_rate": quote["turnover_rate"],
                "total_shares": total_shares,
                "circulating_shares": circulating_shares,
                "listed_a_shares": as_int_or_none(capital.get("LISTED_A_SHARES")),
                "limited_shares": as_int_or_none(capital.get("LIMITED_SHARES")),
                "market_cap": market_cap,
                "circulating_market_cap": circulating_market_cap,
                "quote_raw_json": json.dumps(
                    quote, cls=DecimalJSONEncoder, ensure_ascii=False, sort_keys=True
                ),
                "capital_raw_json": json.dumps(
                    capital, cls=DecimalJSONEncoder, ensure_ascii=False, sort_keys=True
                ),
            }
        )
    return snapshots


def load_cached_market_snapshots(
    db_path: Path,
    symbol: str,
    report_periods: dict[str, str | None],
) -> list[dict[str, Any]]:
    if not db_path.exists() or not report_periods:
        return []
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_market_snapshots'"
        ).fetchone()
        if not table_exists:
            return []
        placeholders = ",".join("?" for _ in report_periods)
        rows = connection.execute(
            f"""
            SELECT *
              FROM report_market_snapshots
             WHERE symbol=?
               AND report_date IN ({placeholders})
            """,
            (symbol, *report_periods.keys()),
        ).fetchall()
    finally:
        connection.close()

    snapshots: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        record.pop("symbol", None)
        record.pop("run_id", None)
        record.pop("fetched_at", None)
        record["report_type"] = report_periods[record["report_date"]]
        for field in (
            "open_price",
            "close_price",
            "high_price",
            "low_price",
            "amount",
            "amplitude",
            "change_percent",
            "change_amount",
            "turnover_rate",
            "market_cap",
            "circulating_market_cap",
        ):
            if record.get(field) is not None:
                record[field] = Decimal(str(record[field]))
        snapshots.append(record)
    snapshots.sort(key=lambda row: row["report_date"], reverse=True)
    return snapshots


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata_order = [field for field in REPORT_METADATA_FIELDS if any(field in row for row in records)]
    metadata_order.sort(key=lambda field: (field != "REPORT_DATE", field))
    other_fields = sorted({key for row in records for key in row} - set(metadata_order))
    fields = metadata_order + other_fields
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in records:
            writer.writerow({key: decimal_to_text(value) for key, value in row.items()})


def decimal_to_text(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS companies (
            symbol TEXT PRIMARY KEY,
            security_code TEXT NOT NULL,
            market TEXT NOT NULL,
            security_name TEXT,
            company_type TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fetch_runs (
            run_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            start_year INTEGER NOT NULL,
            end_year INTEGER NOT NULL,
            period TEXT NOT NULL,
            source_page TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            FOREIGN KEY (symbol) REFERENCES companies(symbol)
        );
        CREATE TABLE IF NOT EXISTS reports (
            symbol TEXT NOT NULL,
            statement TEXT NOT NULL CHECK (statement IN ('balance', 'income', 'cashflow')),
            report_date TEXT NOT NULL,
            report_type TEXT,
            report_date_name TEXT,
            notice_date TEXT,
            currency TEXT,
            raw_json TEXT NOT NULL,
            run_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, statement, report_date),
            FOREIGN KEY (symbol) REFERENCES companies(symbol),
            FOREIGN KEY (run_id) REFERENCES fetch_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS facts (
            symbol TEXT NOT NULL,
            statement TEXT NOT NULL CHECK (statement IN ('balance', 'income', 'cashflow')),
            report_date TEXT NOT NULL,
            item_code TEXT NOT NULL,
            numeric_text TEXT,
            numeric_value REAL,
            text_value TEXT,
            run_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, statement, report_date, item_code),
            FOREIGN KEY (symbol, statement, report_date)
                REFERENCES reports(symbol, statement, report_date) ON DELETE CASCADE,
            FOREIGN KEY (run_id) REFERENCES fetch_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS financial_item_definitions (
            company_type TEXT NOT NULL,
            statement TEXT NOT NULL CHECK (statement IN ('balance', 'income', 'cashflow')),
            item_code TEXT NOT NULL,
            display_name TEXT NOT NULL,
            display_order INTEGER NOT NULL,
            section_name TEXT,
            source_template TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (company_type, statement, item_code)
        );
        CREATE TABLE IF NOT EXISTS report_market_snapshots (
            symbol TEXT NOT NULL,
            report_date TEXT NOT NULL,
            report_type TEXT,
            trade_date TEXT NOT NULL,
            share_effective_date TEXT NOT NULL,
            open_price REAL,
            close_price REAL NOT NULL,
            high_price REAL,
            low_price REAL,
            volume_lots INTEGER,
            amount REAL,
            amplitude REAL,
            change_percent REAL,
            change_amount REAL,
            turnover_rate REAL,
            total_shares INTEGER NOT NULL,
            circulating_shares INTEGER,
            listed_a_shares INTEGER,
            limited_shares INTEGER,
            market_cap REAL NOT NULL,
            circulating_market_cap REAL,
            quote_raw_json TEXT NOT NULL,
            capital_raw_json TEXT NOT NULL,
            run_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, report_date),
            FOREIGN KEY (symbol) REFERENCES companies(symbol),
            FOREIGN KEY (run_id) REFERENCES fetch_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS share_capital_changes (
            symbol TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            total_shares INTEGER NOT NULL,
            circulating_shares INTEGER,
            listed_a_shares INTEGER,
            limited_shares INTEGER,
            change_reason TEXT,
            raw_json TEXT NOT NULL,
            run_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, effective_date),
            FOREIGN KEY (symbol) REFERENCES companies(symbol),
            FOREIGN KEY (run_id) REFERENCES fetch_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS share_repurchase_programs (
            symbol TEXT NOT NULL,
            repurchase_code TEXT NOT NULL,
            announcement_date TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            finish_date TEXT,
            latest_notice_date TEXT,
            updated_date TEXT,
            progress_code TEXT,
            share_type TEXT,
            objective TEXT,
            planned_amount_lower_text TEXT,
            planned_amount_upper_text TEXT,
            planned_shares_lower INTEGER,
            planned_shares_upper INTEGER,
            planned_price_cap_text TEXT,
            repurchased_amount_text TEXT,
            repurchased_shares INTEGER,
            repurchased_price_lower_text TEXT,
            repurchased_price_upper_text TEXT,
            raw_json TEXT NOT NULL,
            run_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, repurchase_code),
            FOREIGN KEY (symbol) REFERENCES companies(symbol),
            FOREIGN KEY (run_id) REFERENCES fetch_runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_facts_lookup
            ON facts(symbol, item_code, report_date);
        CREATE INDEX IF NOT EXISTS idx_market_snapshot_date
            ON report_market_snapshots(symbol, trade_date);
        CREATE INDEX IF NOT EXISTS idx_share_capital_changes_date
            ON share_capital_changes(symbol, effective_date);
        CREATE INDEX IF NOT EXISTS idx_share_repurchase_programs_date
            ON share_repurchase_programs(symbol, announcement_date);
        """
    )


def store_database(
    db_path: Path,
    *,
    symbol: str,
    company_type: str,
    fiscal_range: FiscalRange,
    period: str,
    source_page: str,
    fetched_at: str,
    run_id: str,
    statements: dict[str, list[dict[str, Any]]],
    item_definitions: list[dict[str, Any]],
    market_snapshots: list[dict[str, Any]],
    capital_changes: list[dict[str, Any]],
    repurchase_programs: list[dict[str, Any]],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        initialize_database(connection)
        first_record = next((rows[0] for rows in statements.values() if rows), {})
        security_name = first_record.get("SECURITY_NAME_ABBR")
        with connection:
            connection.execute(
                """
                INSERT INTO companies(symbol, security_code, market, security_name, company_type, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    security_name=excluded.security_name,
                    company_type=excluded.company_type,
                    updated_at=excluded.updated_at
                """,
                (symbol, symbol[2:], symbol[:2], security_name, company_type, fetched_at),
            )
            connection.execute(
                """
                INSERT INTO fetch_runs(run_id, symbol, start_year, end_year, period, source_page, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    source_page=excluded.source_page,
                    fetched_at=excluded.fetched_at
                """,
                (
                    run_id,
                    symbol,
                    fiscal_range.start_year,
                    fiscal_range.end_year,
                    period,
                    source_page,
                    fetched_at,
                ),
            )

            for statement, records in statements.items():
                for record in records:
                    report_date = normalize_report_date(record.get("REPORT_DATE"))
                    connection.execute(
                        "DELETE FROM facts WHERE symbol=? AND statement=? AND report_date=?",
                        (symbol, statement, report_date),
                    )
                    connection.execute(
                        """
                        INSERT INTO reports(
                            symbol, statement, report_date, report_type, report_date_name,
                            notice_date, currency, raw_json, run_id, fetched_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(symbol, statement, report_date) DO UPDATE SET
                            report_type=excluded.report_type,
                            report_date_name=excluded.report_date_name,
                            notice_date=excluded.notice_date,
                            currency=excluded.currency,
                            raw_json=excluded.raw_json,
                            run_id=excluded.run_id,
                            fetched_at=excluded.fetched_at
                        """,
                        (
                            symbol,
                            statement,
                            report_date,
                            record.get("REPORT_TYPE"),
                            record.get("REPORT_DATE_NAME"),
                            record.get("NOTICE_DATE"),
                            record.get("CURRENCY"),
                            json.dumps(record, cls=DecimalJSONEncoder, ensure_ascii=False, sort_keys=True),
                            run_id,
                            fetched_at,
                        ),
                    )
                    fact_rows = []
                    for item_code, value in record.items():
                        if item_code in REPORT_METADATA_FIELDS or value is None:
                            continue
                        numeric_text: str | None = None
                        numeric_value: float | None = None
                        text_value: str | None = None
                        if isinstance(value, bool):
                            text_value = str(value).lower()
                        elif isinstance(value, (int, Decimal)):
                            numeric_text = str(value)
                            numeric_value = float(value)
                        elif isinstance(value, float):
                            numeric_text = repr(value)
                            numeric_value = value
                        else:
                            text_value = str(value)
                        fact_rows.append(
                            (
                                symbol,
                                statement,
                                report_date,
                                item_code,
                                numeric_text,
                                numeric_value,
                                text_value,
                                run_id,
                                fetched_at,
                            )
                        )
                    connection.executemany(
                        """
                        INSERT INTO facts(
                            symbol, statement, report_date, item_code,
                            numeric_text, numeric_value, text_value, run_id, fetched_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                    fact_rows,
                    )

            connection.execute(
                "DELETE FROM financial_item_definitions WHERE company_type=?",
                (company_type,),
            )
            for definition in item_definitions:
                connection.execute(
                    """
                    INSERT INTO financial_item_definitions(
                        company_type, statement, item_code, display_name,
                        display_order, section_name, source_template, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(company_type, statement, item_code) DO UPDATE SET
                        display_name=excluded.display_name,
                        display_order=excluded.display_order,
                        section_name=excluded.section_name,
                        source_template=excluded.source_template,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        company_type,
                        definition["statement"],
                        definition["item_code"],
                        definition["display_name"],
                        definition["display_order"],
                        definition["section_name"],
                        definition["source_template"],
                        fetched_at,
                    ),
                )

            for snapshot in market_snapshots:
                connection.execute(
                    """
                    INSERT INTO report_market_snapshots(
                        symbol, report_date, report_type, trade_date, share_effective_date,
                        open_price, close_price, high_price, low_price, volume_lots,
                        amount, amplitude, change_percent, change_amount, turnover_rate,
                        total_shares, circulating_shares, listed_a_shares, limited_shares,
                        market_cap, circulating_market_cap, quote_raw_json,
                        capital_raw_json, run_id, fetched_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(symbol, report_date) DO UPDATE SET
                        report_type=excluded.report_type,
                        trade_date=excluded.trade_date,
                        share_effective_date=excluded.share_effective_date,
                        open_price=excluded.open_price,
                        close_price=excluded.close_price,
                        high_price=excluded.high_price,
                        low_price=excluded.low_price,
                        volume_lots=excluded.volume_lots,
                        amount=excluded.amount,
                        amplitude=excluded.amplitude,
                        change_percent=excluded.change_percent,
                        change_amount=excluded.change_amount,
                        turnover_rate=excluded.turnover_rate,
                        total_shares=excluded.total_shares,
                        circulating_shares=excluded.circulating_shares,
                        listed_a_shares=excluded.listed_a_shares,
                        limited_shares=excluded.limited_shares,
                        market_cap=excluded.market_cap,
                        circulating_market_cap=excluded.circulating_market_cap,
                        quote_raw_json=excluded.quote_raw_json,
                        capital_raw_json=excluded.capital_raw_json,
                        run_id=excluded.run_id,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        symbol,
                        snapshot["report_date"],
                        snapshot["report_type"],
                        snapshot["trade_date"],
                        snapshot["share_effective_date"],
                        float(snapshot["open_price"]),
                        float(snapshot["close_price"]),
                        float(snapshot["high_price"]),
                        float(snapshot["low_price"]),
                        snapshot["volume_lots"],
                        (
                            float(snapshot["amount"])
                            if snapshot["amount"] is not None
                            else None
                        ),
                        (
                            float(snapshot["amplitude"])
                            if snapshot["amplitude"] is not None
                            else None
                        ),
                        (
                            float(snapshot["change_percent"])
                            if snapshot["change_percent"] is not None
                            else None
                        ),
                        (
                            float(snapshot["change_amount"])
                            if snapshot["change_amount"] is not None
                            else None
                        ),
                        (
                            float(snapshot["turnover_rate"])
                            if snapshot["turnover_rate"] is not None
                            else None
                        ),
                        snapshot["total_shares"],
                        snapshot["circulating_shares"],
                        snapshot["listed_a_shares"],
                        snapshot["limited_shares"],
                        float(snapshot["market_cap"]),
                        (
                            float(snapshot["circulating_market_cap"])
                            if snapshot["circulating_market_cap"] is not None
                            else None
                        ),
                        snapshot["quote_raw_json"],
                        snapshot["capital_raw_json"],
                        run_id,
                        fetched_at,
                    ),
                )

            if capital_changes:
                connection.execute(
                    "DELETE FROM share_capital_changes WHERE symbol=?",
                    (symbol,),
                )
                for change in capital_changes:
                    connection.execute(
                        """
                        INSERT INTO share_capital_changes(
                            symbol, effective_date, total_shares,
                            circulating_shares, listed_a_shares, limited_shares,
                            change_reason, raw_json, run_id, fetched_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(symbol, effective_date) DO UPDATE SET
                            total_shares=excluded.total_shares,
                            circulating_shares=excluded.circulating_shares,
                            listed_a_shares=excluded.listed_a_shares,
                            limited_shares=excluded.limited_shares,
                            change_reason=excluded.change_reason,
                            raw_json=excluded.raw_json,
                            run_id=excluded.run_id,
                            fetched_at=excluded.fetched_at
                        """,
                        (
                            symbol,
                            normalize_report_date(change["END_DATE"]),
                            as_int_or_none(change.get("TOTAL_SHARES")),
                            as_int_or_none(change.get("UNLIMITED_SHARES")),
                            as_int_or_none(change.get("LISTED_A_SHARES")),
                            as_int_or_none(change.get("LIMITED_SHARES")),
                            change.get("CHANGE_REASON"),
                            json.dumps(
                                change,
                                cls=DecimalJSONEncoder,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            run_id,
                            fetched_at,
                        ),
                    )

            connection.execute(
                "DELETE FROM share_repurchase_programs WHERE symbol=?",
                (symbol,),
            )
            for program in repurchase_programs:
                connection.execute(
                    """
                    INSERT INTO share_repurchase_programs(
                        symbol, repurchase_code, announcement_date,
                        start_date, end_date, finish_date, latest_notice_date,
                        updated_date, progress_code, share_type, objective,
                        planned_amount_lower_text, planned_amount_upper_text,
                        planned_shares_lower, planned_shares_upper,
                        planned_price_cap_text, repurchased_amount_text,
                        repurchased_shares, repurchased_price_lower_text,
                        repurchased_price_upper_text, raw_json, run_id, fetched_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(symbol, repurchase_code) DO UPDATE SET
                        announcement_date=excluded.announcement_date,
                        start_date=excluded.start_date,
                        end_date=excluded.end_date,
                        finish_date=excluded.finish_date,
                        latest_notice_date=excluded.latest_notice_date,
                        updated_date=excluded.updated_date,
                        progress_code=excluded.progress_code,
                        share_type=excluded.share_type,
                        objective=excluded.objective,
                        planned_amount_lower_text=excluded.planned_amount_lower_text,
                        planned_amount_upper_text=excluded.planned_amount_upper_text,
                        planned_shares_lower=excluded.planned_shares_lower,
                        planned_shares_upper=excluded.planned_shares_upper,
                        planned_price_cap_text=excluded.planned_price_cap_text,
                        repurchased_amount_text=excluded.repurchased_amount_text,
                        repurchased_shares=excluded.repurchased_shares,
                        repurchased_price_lower_text=excluded.repurchased_price_lower_text,
                        repurchased_price_upper_text=excluded.repurchased_price_upper_text,
                        raw_json=excluded.raw_json,
                        run_id=excluded.run_id,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        symbol,
                        str(program["REPURCODE"]),
                        normalize_report_date(program["DIM_DATE"]),
                        normalize_report_date(program["REPURSTARTDATE"])
                        if program.get("REPURSTARTDATE")
                        else None,
                        normalize_report_date(program["REPURENDDATE"])
                        if program.get("REPURENDDATE")
                        else None,
                        normalize_report_date(program["FINISHDATE"])
                        if program.get("FINISHDATE")
                        else None,
                        normalize_report_date(program["NOTICEDATE"])
                        if program.get("NOTICEDATE")
                        else None,
                        normalize_report_date(program["UPDATEDATE"])
                        if program.get("UPDATEDATE")
                        else None,
                        str(program["REPURPROGRESS"])
                        if program.get("REPURPROGRESS") is not None
                        else None,
                        program.get("SHARETYPE"),
                        program.get("REPUROBJECTIVE"),
                        str(program["REPURAMOUNTLOWER"])
                        if program.get("REPURAMOUNTLOWER") is not None
                        else None,
                        str(program["REPURAMOUNTLIMIT"])
                        if program.get("REPURAMOUNTLIMIT") is not None
                        else None,
                        as_int_or_none(program.get("REPURNUMLOWER")),
                        as_int_or_none(program.get("REPURNUMCAP")),
                        str(program["REPURPRICECAP"])
                        if program.get("REPURPRICECAP") is not None
                        else None,
                        str(program["REPURAMOUNT"])
                        if program.get("REPURAMOUNT") is not None
                        else None,
                        as_int_or_none(program.get("REPURNUM")),
                        str(program["REPURPRICELOWER1"])
                        if program.get("REPURPRICELOWER1") is not None
                        else None,
                        str(program["REPURPRICECAP1"])
                        if program.get("REPURPRICECAP1") is not None
                        else None,
                        json.dumps(
                            program,
                            cls=DecimalJSONEncoder,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        run_id,
                        fetched_at,
                    ),
                )

            connection.execute(
                """
                DELETE FROM fetch_runs
                 WHERE run_id NOT IN (SELECT run_id FROM reports)
                   AND run_id NOT IN (SELECT run_id FROM facts)
                   AND run_id NOT IN (SELECT run_id FROM report_market_snapshots)
                   AND run_id NOT IN (SELECT run_id FROM share_capital_changes)
                   AND run_id NOT IN (SELECT run_id FROM share_repurchase_programs)
                """
            )
    finally:
        connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        if args.retries < 1 or args.retries > 5:
            raise ValueError("--retries must be between 1 and 5")
        symbol = normalize_symbol(args.symbol)
        fiscal_range = resolve_fiscal_range(args)
        output_dir = args.output.expanduser().resolve()
        db_path = output_dir / "eastmoney_financials.sqlite3"
        company_type, referer, company_page_html = fetch_company_context(
            symbol, timeout=args.timeout, retries=args.retries
        )
        item_definitions = extract_item_definitions(company_page_html)

        repurchase_programs, repurchase_request = fetch_repurchase_programs(
            symbol,
            timeout=args.timeout,
            retries=args.retries,
        )

        market_fetch_error: str | None = None
        try:
            klines, kline_request = fetch_daily_klines(
                symbol,
                [
                    f"{fiscal_range.start_year}-03-31",
                    f"{fiscal_range.end_year}-12-31",
                ],
                timeout=args.timeout,
                retries=args.retries,
            )
            capital_changes, capital_request = fetch_capital_changes(
                symbol,
                timeout=args.timeout,
                retries=args.retries,
            )
        except EastmoneyError as exc:
            market_fetch_error = str(exc)
            klines = []
            capital_changes = []
            kline_request = {"error": market_fetch_error}
            capital_request = {"skipped": True}

        statements: dict[str, list[dict[str, Any]]] = {}
        request_log: dict[str, Any] = {
            "daily_kline": kline_request,
            "capital_changes": capital_request,
            "share_repurchase_programs": repurchase_request,
        }
        for statement in STATEMENTS:
            records, requests_made = fetch_statement(
                statement,
                symbol,
                company_type,
                fiscal_range,
                args.period,
                timeout=args.timeout,
                retries=args.retries,
                referer=referer,
            )
            statements[statement] = records
            request_log[statement] = requests_made

        if not any(statements.values()):
            raise EastmoneyError("no financial statement rows matched the requested range")

        report_periods = collect_report_periods(statements)
        if market_fetch_error is None:
            market_snapshots = build_report_market_snapshots(
                report_periods, klines, capital_changes
            )
            market_source = (
                "tencent_unadjusted_fallback"
                if any(
                    request.get("source") == "tencent_unadjusted_fallback"
                    for request in kline_request
                )
                else "live"
            )
        else:
            market_snapshots = load_cached_market_snapshots(
                db_path, symbol, report_periods
            )
            expected_dates = set(report_periods)
            cached_dates = {row["report_date"] for row in market_snapshots}
            missing_dates = sorted(expected_dates - cached_dates)
            if missing_dates:
                preview = ", ".join(missing_dates[:5])
                if len(missing_dates) > 5:
                    preview += ", ..."
                raise EastmoneyError(
                    "market endpoints failed and the database cache is incomplete; "
                    f"missing {len(missing_dates)} report dates ({preview}); "
                    f"upstream error: {market_fetch_error}"
                )
            market_source = "database_cache"
            print(
                "warning: market endpoints failed; reused complete market snapshots "
                f"from the existing database ({market_fetch_error})",
                file=sys.stderr,
            )

        fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        stem = (
            f"{symbol}_{fiscal_range.start_year}_{fiscal_range.end_year}_"
            f"{args.period}"
        )
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"eastmoney-financials:{stem}"))

        csv_paths: dict[str, str] = {}
        for statement, records in statements.items():
            csv_path = output_dir / "csv" / f"{stem}_{statement}.csv"
            write_csv(csv_path, records)
            csv_paths[statement] = str(csv_path)
        market_csv_path = output_dir / "csv" / f"{stem}_market.csv"
        write_csv(market_csv_path, market_snapshots)
        csv_paths["market"] = str(market_csv_path)
        if capital_changes:
            capital_changes_csv_path = (
                output_dir / "csv" / f"{stem}_capital_changes.csv"
            )
            write_csv(capital_changes_csv_path, capital_changes)
            csv_paths["capital_changes"] = str(capital_changes_csv_path)
        repurchase_csv_path = output_dir / "csv" / f"{stem}_repurchases.csv"
        write_csv(repurchase_csv_path, repurchase_programs)
        csv_paths["repurchases"] = str(repurchase_csv_path)

        snapshot = {
            "schema_version": 4,
            "run_id": run_id,
            "source": "Eastmoney F10 NewFinanceAnalysis",
            "source_page": (
                f"{SOURCE_PAGE}?type=web&code={symbol.lower()}&color=b#/cwfx"
            ),
            "fetched_at": fetched_at,
            "symbol": symbol,
            "company_type": company_type,
            "filters": {
                "start_year": fiscal_range.start_year,
                "end_year": fiscal_range.end_year,
                "period": args.period,
            },
            "request_log": request_log,
            "market_source": market_source,
            "statements": statements,
            "financial_item_definitions": item_definitions,
            "report_market_snapshots": market_snapshots,
            "share_capital_changes": capital_changes,
            "share_repurchase_programs": repurchase_programs,
        }
        raw_path = output_dir / "raw" / f"{stem}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            json.dumps(snapshot, cls=DecimalJSONEncoder, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        store_database(
            db_path,
            symbol=symbol,
            company_type=company_type,
            fiscal_range=fiscal_range,
            period=args.period,
            source_page=snapshot["source_page"],
            fetched_at=fetched_at,
            run_id=run_id,
            statements=statements,
            item_definitions=item_definitions,
            market_snapshots=market_snapshots,
            capital_changes=capital_changes,
            repurchase_programs=repurchase_programs,
        )

        summary = {
            "symbol": symbol,
            "company_type": company_type,
            "range": [fiscal_range.start_year, fiscal_range.end_year],
            "period": args.period,
            "market_source": market_source,
            "rows": {
                **{name: len(rows) for name, rows in statements.items()},
                "market": len(market_snapshots),
                "capital_changes": len(capital_changes),
                "repurchase_programs": len(repurchase_programs),
                "item_definitions": len(item_definitions),
            },
            "database": str(db_path),
            "raw_snapshot": str(raw_path),
            "csv": csv_paths,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, EastmoneyError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
