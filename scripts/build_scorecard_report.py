#!/usr/bin/env python3
"""Build provisional colored Buffett scorecard reports from comparison JSON."""

from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEIGHTS = ROOT / "references" / "scorecard-weights.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON from compare_buffett_metrics.py")
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    parser.add_argument("--output-html", type=Path)
    return parser.parse_args()


def get_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def format_value(value: Any, kind: str | None = None) -> str:
    number = as_float(value)
    if number is None:
        return "—"
    if kind == "percent":
        return f"{number * 100:.1f}%"
    return f"{number:.2f}"


def percentile_scores(values: list[float | None], direction: str) -> list[float | None]:
    available = sorted({value for value in values if value is not None})
    if not available:
        return [None for _ in values]
    if len(available) == 1:
        return [100.0 if value is not None else None for value in values]
    scores: list[float | None] = []
    for value in values:
        if value is None:
            scores.append(None)
            continue
        index = available.index(value)
        fraction = index / (len(available) - 1)
        if direction == "lower":
            fraction = 1 - fraction
        scores.append(50.0 + 50.0 * fraction)
    return scores


def base_result(display: str = "—") -> dict[str, Any]:
    return {
        "display": display,
        "score": None,
        "status": "insufficient",
        "winner": False,
        "color": "gray",
        "label": "数据不足",
    }


def validate_config(config: dict[str, Any]) -> None:
    categories = config.get("categories")
    metrics = config.get("metrics")
    if not isinstance(categories, list) or not isinstance(metrics, list):
        raise ValueError("scorecard config requires categories and metrics lists")
    category_weights = {item["id"]: float(item["weight"]) for item in categories}
    if abs(sum(category_weights.values()) - 100) > 1e-9:
        raise ValueError("scorecard category weights must total 100")
    metric_ids = [item["id"] for item in metrics]
    if len(metric_ids) != len(set(metric_ids)):
        raise ValueError("scorecard metric ids must be unique")
    actual_by_category: dict[str, float] = defaultdict(float)
    for metric in metrics:
        category = metric["category"]
        if category not in category_weights:
            raise ValueError(f"unknown scorecard category: {category}")
        actual_by_category[category] += float(metric["weight"])
    for category, expected in category_weights.items():
        if abs(actual_by_category[category] - expected) > 1e-9:
            raise ValueError(
                f"metric weights for {category} total {actual_by_category[category]}, expected {expected}"
            )


def evaluate_absolute(company: dict[str, Any], metric: dict[str, Any]) -> dict[str, Any]:
    result = base_result()
    evaluator = metric["evaluator"]

    if evaluator in {"persistence", "comparison_ratio"}:
        container = get_path(company, metric["path"])
        if not isinstance(container, dict):
            return result
        count = as_float(container.get(metric["count"]))
        available = as_float(container.get(metric["available"]))
        if count is None or available is None or available <= 0:
            return result
        required_available = as_float(metric.get("required_available"))
        if required_available is not None and available < required_available:
            result["display"] = (
                f"{int(count)}/{int(available)}（需{int(required_available)}年）"
            )
            return result
        ratio = count / available
        result.update(
            display=f"{int(count)}/{int(available)}",
            score=clamp(ratio * 100),
            status="pass" if ratio + 1e-12 >= metric["pass_ratio"] else "fail",
        )
        return result

    if evaluator == "trend_level":
        level = get_path(company, metric["path"])
        mapping = {
            "strong": (100.0, "pass", "强"),
            "moderate": (80.0, "pass", "温和"),
            "not_met": (30.0, "fail", "未满足"),
        }
        if level not in mapping:
            return result
        score, status, display = mapping[level]
        result.update(display=display, score=score, status=status)
        return result

    if evaluator == "signed_growth":
        value = as_float(get_path(company, metric["path"]))
        if value is None:
            return result
        threshold = float(metric.get("pass_threshold", 0))
        if value >= threshold:
            score = 70 + min(30, max(0, value - threshold) / 0.20 * 30)
            status = "pass"
        else:
            score = max(0, 50 + (value - threshold) * 100)
            status = "fail"
        result.update(
            display=format_value(value, "percent"),
            score=clamp(score),
            status=status,
        )
        return result

    if evaluator == "cash_quality":
        container = get_path(company, metric["path"])
        if not isinstance(container, dict):
            return result
        years = as_float(container.get("years_available"))
        positive = as_float(container.get("positive_operating_cash_years"))
        reconcile_available = as_float(container.get("cash_reconciliation_available"))
        reconcile_passed = as_float(container.get("cash_reconciliation_passed"))
        if years is None or positive is None or years <= 0:
            return result
        positive_ratio = positive / years
        reconcile_ratio = (
            reconcile_passed / reconcile_available
            if reconcile_available and reconcile_passed is not None
            else 0
        )
        score = positive_ratio * 80 + reconcile_ratio * 20
        reconciliation_complete = bool(
            reconcile_available and reconcile_passed is not None
        )
        passed = (
            positive == years
            and reconciliation_complete
            and reconcile_passed == reconcile_available
        )
        result.update(
            display=f"经营现金为正{int(positive)}/{int(years)}；勾稽{int(reconcile_passed or 0)}/{int(reconcile_available or 0)}",
            score=clamp(score),
            status=(
                "pass"
                if passed
                else "partial"
                if not reconciliation_complete
                else "fail"
            ),
        )
        return result

    if evaluator == "debt_to_equity":
        value = as_float(get_path(company, metric["path"]))
        if value is None:
            return result
        threshold = float(metric["pass_threshold"])
        if value < threshold:
            score = 100 if value <= 0.2 else 100 - (value - 0.2) / 0.6 * 30
            status = "pass"
        else:
            score = max(0, 70 - (value - threshold) / 0.7 * 70)
            status = "fail"
        result.update(display=f"{value:.2f}", score=clamp(score), status=status)
        return result

    if evaluator == "debt_payoff":
        value = as_float(get_path(company, metric["path"]))
        if value is None:
            return result
        if value <= 3:
            score = 100
        elif value <= 4:
            score = 80
        else:
            score = max(0, 80 - (value - 4) * 20)
        result.update(
            display=f"{value:.2f}年",
            score=clamp(score),
            status="pass" if value <= float(metric["pass_threshold"]) else "fail",
        )
        return result

    if evaluator == "preferred_stock":
        value = get_path(company, metric["path"])
        mapping = {
            "not_detected_in_complete_data": (100.0, "pass", "完整数据未见"),
            "detected": (0.0, "fail", "发现余额"),
        }
        if value not in mapping:
            return result
        score, status, display = mapping[value]
        result.update(display=display, score=score, status=status)
        return result

    if evaluator == "capex":
        classification = get_path(company, metric["path"])
        value = get_path(company, metric["value_path"])
        mapping = {
            "below_25": (100.0, "pass"),
            "below_50": (75.0, "pass"),
            "at_or_above_50": (25.0, "fail"),
        }
        if classification not in mapping:
            return result
        score, status = mapping[classification]
        result.update(display=format_value(value, "percent"), score=score, status=status)
        return result

    return result


def build_scorecard(companies: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    evaluations: dict[str, list[dict[str, Any]]] = {}
    for metric in config["metrics"]:
        evaluator = metric["evaluator"]
        if evaluator in {"relative", "relative_with_threshold"}:
            values = [as_float(get_path(company, metric["path"])) for company in companies]
            scores = percentile_scores(values, metric["direction"])
            results: list[dict[str, Any]] = []
            for value, score in zip(values, scores):
                result = base_result(format_value(value, metric.get("format")))
                if score is not None:
                    status = "relative"
                    threshold = metric.get("pass_threshold")
                    if threshold is not None:
                        if metric["direction"] == "higher":
                            status = "pass" if value is not None and value > threshold else "fail"
                        else:
                            status = "pass" if value is not None and value < threshold else "fail"
                    result.update(score=score, status=status)
                results.append(result)
            evaluations[metric["id"]] = results
        elif evaluator == "relative_composite":
            component_scores: list[list[float | None]] = []
            component_values: list[list[float | None]] = []
            for component in metric["components"]:
                values = [as_float(get_path(company, component["path"])) for company in companies]
                component_values.append(values)
                component_scores.append(percentile_scores(values, component["direction"]))
            results = []
            for index in range(len(companies)):
                scores = [values[index] for values in component_scores if values[index] is not None]
                raw_values = [values[index] for values in component_values]
                result = base_result()
                if len(scores) == len(component_scores):
                    result.update(
                        display=f"ROE {format_value(raw_values[0], 'percent')} / 杠杆{format_value(raw_values[1])}",
                        score=sum(scores) / len(scores),
                        status="relative",
                    )
                results.append(result)
            evaluations[metric["id"]] = results
        else:
            evaluations[metric["id"]] = [
                evaluate_absolute(company, metric) for company in companies
            ]

    for metric in config["metrics"]:
        results = evaluations[metric["id"]]
        eligible_indices = [
            index
            for index, result in enumerate(results)
            if result["score"] is not None
            and result["status"] not in {"fail", "partial"}
        ]
        winning_score = (
            max(results[index]["score"] for index in eligible_indices)
            if eligible_indices
            else None
        )
        winner_indices = {
            index
            for index in eligible_indices
            if winning_score is not None
            and abs(results[index]["score"] - winning_score) <= 0.5
        }
        tiebreak_path = metric.get("winner_tiebreak_path")
        if len(winner_indices) > 1 and tiebreak_path:
            tiebreak_values = {
                index: as_float(get_path(companies[index], tiebreak_path))
                for index in winner_indices
            }
            available_values = [
                value for value in tiebreak_values.values() if value is not None
            ]
            if available_values:
                best_value = (
                    min(available_values)
                    if metric.get("winner_direction") == "lower"
                    else max(available_values)
                )
                winner_indices = {
                    index
                    for index, value in tiebreak_values.items()
                    if value is not None and abs(value - best_value) <= 1e-12
                }
        for index, result in enumerate(results):
            if result["score"] is None:
                continue
            result["winner"] = index in winner_indices
            if result["winner"]:
                result.update(color="red", label="胜出")
            elif result["status"] == "pass":
                result.update(color="orange", label="符合")
            elif result["status"] == "fail":
                result.update(color="green", label="不符合")
            elif result["status"] == "partial":
                result.update(color="gray", label="部分数据")
            else:
                result.update(color="gray", label="组内比较")

    category_labels = {item["id"]: item["label"] for item in config["categories"]}
    company_results: list[dict[str, Any]] = []
    for index, company in enumerate(companies):
        category_weighted: dict[str, float] = defaultdict(float)
        category_available: dict[str, float] = defaultdict(float)
        total_weighted = 0.0
        total_available = 0.0
        quality_weighted = 0.0
        quality_available = 0.0
        details: list[dict[str, Any]] = []
        for metric in config["metrics"]:
            result = dict(evaluations[metric["id"]][index])
            weight = float(metric["weight"])
            result.update(
                id=metric["id"],
                metric=metric["label"],
                category=metric["category"],
                category_label=category_labels[metric["category"]],
                weight=weight,
            )
            if result["score"] is not None:
                contribution = result["score"] * weight / 100
                result["contribution"] = contribution
                total_weighted += contribution
                total_available += weight
                category_weighted[metric["category"]] += contribution
                category_available[metric["category"]] += weight
                if metric["category"] != "valuation":
                    quality_weighted += contribution
                    quality_available += weight
            else:
                result["contribution"] = None
            details.append(result)
        coverage = total_available / sum(float(metric["weight"]) for metric in config["metrics"])
        total_score = total_weighted / total_available * 100 if total_available else None
        quality_score = quality_weighted / quality_available * 100 if quality_available else None
        category_scores = {
            category["id"]: (
                category_weighted[category["id"]] / category_available[category["id"]] * 100
                if category_available[category["id"]]
                else None
            )
            for category in config["categories"]
        }
        company_results.append(
            {
                "symbol": company["symbol"],
                "name": company["name"],
                "report_dates": company.get("report_dates", []),
                "annual_trends": company.get("annual_trends", {}),
                "coverage": coverage,
                "quality_score": quality_score,
                "valuation_score": category_scores.get("valuation"),
                "overall_score": total_score,
                "category_scores": category_scores,
                "metrics": details,
            }
        )

    minimum_coverage = float(config["minimum_coverage_for_ranking"])
    ranked = [row for row in company_results if row["coverage"] >= minimum_coverage and row["overall_score"] is not None]
    ranked.sort(key=lambda row: row["overall_score"], reverse=True)
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    for row in company_results:
        row.setdefault("rank", None)
    return {
        "scorecard_name": config["name"],
        "version": config["version"],
        "minimum_coverage_for_ranking": minimum_coverage,
        "categories": config["categories"],
        "unscored_risk_prompts": config["unscored_risk_prompts"],
        "companies": sorted(company_results, key=lambda row: row["rank"] or 9999),
    }


def icon(result: dict[str, Any]) -> str:
    return {"red": "🔴", "orange": "🟠", "green": "🟢", "gray": "⚪"}[result["color"]]


def score_text(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def sparkline_value(value: float, kind: str) -> str:
    if kind == "hundred_million":
        return f"{value / 100_000_000:.1f}亿"
    return f"{value:.2f}"


def combined_trend_svg(
    dates: list[str],
    annual_trends: dict[str, list[Any]],
    *,
    company_name: str,
    series_specs: list[dict[str, str]],
) -> str:
    """Plot differently scaled metrics together as first-valid-year index values."""
    normalized_series: list[dict[str, Any]] = []
    for spec in series_specs:
        values = [as_float(value) for value in annual_trends.get(spec["key"], [])]
        values = (values + [None] * len(dates))[: len(dates)]
        baseline = next((value for value in values if value not in {None, 0}), None)
        if baseline is None or sum(value is not None for value in values) < 2:
            continue
        normalized = [
            value / baseline * 100 if value is not None else None for value in values
        ]
        normalized_series.append({**spec, "values": values, "normalized": normalized})

    all_normalized = [
        value
        for series in normalized_series
        for value in series["normalized"]
        if value is not None
    ]
    if not all_normalized or len(dates) < 2:
        return "<div class='trend-missing'>数据不足</div>"

    width = 248.0
    height = 140.0
    left = 28.0
    right = 8.0
    top = 10.0
    bottom = 23.0
    minimum = min(all_normalized)
    maximum = max(all_normalized)
    padding = (maximum - minimum) * 0.08 or 10.0
    minimum -= padding
    maximum += padding

    def x_position(index: int) -> float:
        return left + index / (len(dates) - 1) * (width - left - right)

    def y_position(value: float) -> float:
        return top + (maximum - value) / (maximum - minimum) * (
            height - top - bottom
        )

    escaped_company = html.escape(company_name)
    parts = [
        f"<svg class='combined-trend' viewBox='0 0 {width:g} {height:g}' role='img' aria-label='{escaped_company}四指标长期趋势'>",
        f"<title>{escaped_company}四指标长期趋势，首个有效年度为100</title>",
    ]
    for fraction in (0.0, 0.5, 1.0):
        guide_value = maximum - fraction * (maximum - minimum)
        guide_y = y_position(guide_value)
        parts.append(
            f"<line class='trend-guide' x1='{left:g}' y1='{guide_y:.2f}' x2='{width - right:g}' y2='{guide_y:.2f}'/>"
        )
        parts.append(
            f"<text class='trend-axis-label' x='{left - 4:g}' y='{guide_y + 3:.2f}'>{guide_value:.0f}</text>"
        )

    for series in normalized_series:
        segments: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for index, value in enumerate(series["normalized"]):
            if value is None:
                if len(current) >= 2:
                    segments.append(current)
                current = []
                continue
            current.append((x_position(index), y_position(value)))
        if len(current) >= 2:
            segments.append(current)
        for segment in segments:
            coordinate_text = " ".join(f"{x:.2f},{y:.2f}" for x, y in segment)
            parts.append(
                f"<polyline class='trend-line' style='stroke:{series['color']}' points='{coordinate_text}'/>"
            )
        for index, (actual, normalized) in enumerate(
            zip(series["values"], series["normalized"])
        ):
            if actual is None or normalized is None:
                continue
            tooltip = html.escape(
                f"{series['label']}｜{dates[index][:4]}｜实际值 {sparkline_value(actual, series['value_kind'])}｜指数 {normalized:.1f}"
            )
            parts.append(
                f"<circle class='trend-point' style='fill:{series['color']}' cx='{x_position(index):.2f}' cy='{y_position(normalized):.2f}' r='2.1'><title>{tooltip}</title></circle>"
            )

    parts.extend(
        [
            f"<text class='trend-year' x='{left:g}' y='{height - 4:g}'>{html.escape(dates[0][:4])}</text>",
            f"<text class='trend-year trend-year-end' x='{width - right:g}' y='{height - 4:g}'>{html.escape(dates[-1][:4])}</text>",
            "</svg>",
        ]
    )
    return "".join(parts)


def markdown_report(scorecard: dict[str, Any]) -> str:
    companies = scorecard["companies"]
    lines = [
        f"# {scorecard['scorecard_name']}",
        "",
        "> 颜色：🔴符合且组内胜出；🟠符合但未胜出；🟢不符合；⚪数据不足、仅相对比较或不适用。综合分为试行权重，不构成投资建议。",
        "",
        "## 综合排名",
        "",
        "| 排名 | 公司 | 公司质量分 | 当前估值得分 | 综合分 | 数据覆盖率 |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for company in companies:
        rank = company["rank"] if company["rank"] is not None else "—"
        prefix = "🔴" if rank == 1 else ""
        lines.append(
            f"| {rank} | {prefix}{company['name']} | {score_text(company['quality_score'])} | "
            f"{score_text(company['valuation_score'])} | {score_text(company['overall_score'])} | "
            f"{company['coverage'] * 100:.0f}% |"
        )
    lines.extend(["", "## 分类得分", ""])
    headers = " | ".join(category["label"] for category in scorecard["categories"])
    lines.extend(
        [
            f"| 公司 | {headers} |",
            "|---|" + "---:|" * len(scorecard["categories"]),
        ]
    )
    for company in companies:
        values = " | ".join(
            score_text(company["category_scores"].get(category["id"]))
            for category in scorecard["categories"]
        )
        lines.append(f"| {company['name']} | {values} |")

    for category in scorecard["categories"]:
        lines.extend(["", f"## {category['label']}（{category['weight']}分）", ""])
        lines.append("| 指标（权重） | " + " | ".join(company["name"] for company in companies) + " |")
        lines.append("|---|" + "---|" * len(companies))
        metric_ids = [
            metric["id"]
            for metric in companies[0]["metrics"]
            if metric["category"] == category["id"]
        ]
        for metric_id in metric_ids:
            cells = []
            label = ""
            weight = 0
            for company in companies:
                metric = next(item for item in company["metrics"] if item["id"] == metric_id)
                label = metric["metric"]
                weight = metric["weight"]
                cells.append(f"{icon(metric)}{metric['display']}（{score_text(metric['score'])}分，{metric['label']}）")
            lines.append(f"| {label}（{weight:g}） | " + " | ".join(cells) + " |")

    lines.extend(["", "## 暂不计分的风险提示", ""])
    lines.extend(f"- {item}" for item in scorecard["unscored_risk_prompts"])
    lines.extend(
        [
            "",
            "> 计分边界：相对指标只说明本次显式传入公司中的位置；所有公司都不符合绝对标准时不会产生红色胜出方。缺失项不按零分处理，但覆盖率低于80%时不参加综合排名。",
        ]
    )
    return "\n".join(lines)


def html_report(scorecard: dict[str, Any]) -> str:
    companies = scorecard["companies"]
    trend_series = [
        {"key": "net_profit", "label": "净利润", "value_kind": "hundred_million", "color": "#2563eb"},
        {"key": "basic_eps", "label": "基本EPS", "value_kind": "decimal", "color": "#ea580c"},
        {"key": "pretax_profit_per_share", "label": "每股税前利润", "value_kind": "decimal", "color": "#16a34a"},
        {"key": "retained_earnings", "label": "留存收益", "value_kind": "hundred_million", "color": "#9333ea"},
    ]
    css = """
    :root{font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif;color:#172033;background:#f6f7fb}
    body{margin:0;padding:28px}.wrap{max-width:1480px;margin:auto}.card{background:white;border:1px solid #e5e7eb;border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 4px 18px rgba(15,23,42,.05)}
    h1{margin:0 0 8px}h2{margin:0 0 14px;font-size:20px}.muted{color:#64748b}.legend{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}
    table{width:100%;border-collapse:separate;border-spacing:0;font-size:14px;overflow:hidden}th,td{border-right:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;padding:10px;vertical-align:top}th{background:#f8fafc;text-align:left;position:sticky;top:0}tr:first-child th{border-top:1px solid #e5e7eb}th:first-child,td:first-child{border-left:1px solid #e5e7eb}.num{text-align:right}.rank1{color:#b91c1c;font-weight:800}
    .red{background:#fee2e2;color:#991b1b}.orange{background:#ffedd5;color:#9a3412}.green{background:#dcfce7;color:#166534}.gray{background:#f1f5f9;color:#475569}.badge{display:inline-block;border-radius:999px;padding:2px 7px;font-size:12px;font-weight:700;margin-bottom:4px}.score{font-weight:800}.small{font-size:12px;opacity:.82}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.metric-table{overflow-x:auto}
    .trend-panel-header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}.trend-legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px}.trend-key{display:flex;align-items:center;gap:6px}.trend-swatch{width:18px;height:3px;border-radius:2px}.trend-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:14px}.trend-company{border:1px solid #e5e7eb;border-radius:10px;padding:10px;background:#fff}.trend-company h3{font-size:14px;margin:0 0 5px}.combined-trend{display:block;width:100%;min-width:210px;height:auto;overflow:visible}.trend-guide{stroke:#94a3b8;stroke-width:.7;opacity:.35}.trend-line{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}.trend-point{stroke:white;stroke-width:.7}.trend-axis-label{fill:#64748b;font-size:8px;text-anchor:end}.trend-year{fill:#64748b;font-size:9px}.trend-year-end{text-anchor:end}.trend-missing{color:#94a3b8;padding:45px 0;text-align:center}
    """
    parts = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{html.escape(scorecard['scorecard_name'])}</title><style>{css}</style></head><body><div class='wrap'>",
        f"<h1>{html.escape(scorecard['scorecard_name'])}</h1>",
        "<p class='muted'>试行权重；用于比较公司质量与当前买入税前收益率，不构成投资建议。</p>",
        "<div class='legend'><span class='badge red'>红：符合且胜出</span><span class='badge orange'>橙：符合</span><span class='badge green'>绿：不符合</span><span class='badge gray'>灰：相对比较/数据不足</span></div>",
        "<section class='card'><h2>综合排名</h2><div class='metric-table'><table><tr><th>排名</th><th>公司</th><th class='num'>公司质量分</th><th class='num'>当前估值得分</th><th class='num'>综合分</th><th class='num'>覆盖率</th></tr>",
    ]
    for company in companies:
        rank = company["rank"] or "—"
        rank_class = " class='rank1'" if rank == 1 else ""
        parts.append(
            f"<tr><td{rank_class}>{rank}</td><td{rank_class}>{html.escape(company['name'])}</td>"
            f"<td class='num'>{score_text(company['quality_score'])}</td>"
            f"<td class='num'>{score_text(company['valuation_score'])}</td>"
            f"<td class='num score'>{score_text(company['overall_score'])}</td>"
            f"<td class='num'>{company['coverage'] * 100:.0f}%</td></tr>"
        )
    parts.append("</table></div></section>")
    parts.append("<section class='card'><h2>分类得分</h2><div class='metric-table'><table><tr><th>公司</th>")
    parts.extend(f"<th class='num'>{html.escape(category['label'])}</th>" for category in scorecard["categories"])
    parts.append("</tr>")
    for company in companies:
        parts.append(f"<tr><td>{html.escape(company['name'])}</td>")
        parts.extend(
            f"<td class='num score'>{score_text(company['category_scores'].get(category['id']))}</td>"
            for category in scorecard["categories"]
        )
        parts.append("</tr>")
    parts.append("</table></div></section>")
    for category in scorecard["categories"]:
        if category["id"] == "growth":
            legend = "".join(
                f"<span class='trend-key'><i class='trend-swatch' style='background:{series['color']}'></i>{html.escape(series['label'])}</span>"
                for series in trend_series
            )
            parts.append(
                "<section class='card'><div class='trend-panel-header'><div><h2>四指标长期趋势</h2>"
                "<div class='small'>各指标首个有效年度=100；比较增长轨迹，不比较金额大小。悬停节点查看年份、实际值和指数。</div></div>"
                f"<div class='trend-legend'>{legend}</div></div><div class='trend-grid'>"
            )
            for company in companies:
                parts.append(
                    f"<div class='trend-company'><h3>{html.escape(company['name'])}</h3>"
                    + combined_trend_svg(
                        company.get("report_dates", []),
                        company.get("annual_trends", {}),
                        company_name=company["name"],
                        series_specs=trend_series,
                    )
                    + "</div>"
                )
            parts.append("</div></section>")
        parts.append(f"<section class='card'><h2>{html.escape(category['label'])}（{category['weight']}分）</h2><div class='metric-table'><table><tr><th>指标</th>")
        parts.extend(f"<th>{html.escape(company['name'])}</th>" for company in companies)
        parts.append("</tr>")
        metric_ids = [metric["id"] for metric in companies[0]["metrics"] if metric["category"] == category["id"]]
        for metric_id in metric_ids:
            first = next(item for item in companies[0]["metrics"] if item["id"] == metric_id)
            parts.append(f"<tr><td><strong>{html.escape(first['metric'])}</strong><div class='small'>权重 {first['weight']:g}</div></td>")
            for company in companies:
                metric = next(item for item in company["metrics"] if item["id"] == metric_id)
                parts.append(
                    f"<td class='{metric['color']}'><span class='badge {metric['color']}'>{html.escape(metric['label'])}</span>"
                    f"<div>{html.escape(metric['display'])}</div><div class='small'>指标得分 {score_text(metric['score'])}</div></td>"
                )
            parts.append("</tr>")
        parts.append("</table></div></section>")
    parts.append("<section class='card'><h2>暂不计分的风险提示</h2><ul>")
    parts.extend(f"<li>{html.escape(item)}</li>" for item in scorecard["unscored_risk_prompts"])
    parts.append("</ul><p class='muted'>相对指标只说明本次显式传入公司中的位置；所有公司都不符合绝对标准时不会产生红色胜出方。缺失项不按零分处理，但覆盖率低于80%时不参加综合排名。</p></section>")
    parts.append("</div></body></html>")
    return "".join(parts)


def write(path: Path | None, content: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")


def main() -> int:
    args = parse_args()
    companies = json.loads(args.input.read_text(encoding="utf-8"))
    config = json.loads(args.weights.read_text(encoding="utf-8"))
    if not isinstance(companies, list) or not companies:
        raise SystemExit("input must be a non-empty comparison JSON array")
    validate_config(config)
    scorecard = build_scorecard(companies, config)
    write(args.output_json, json.dumps(scorecard, ensure_ascii=False, indent=2))
    write(args.output_markdown, markdown_report(scorecard))
    write(args.output_html, html_report(scorecard))
    if not any((args.output_json, args.output_markdown, args.output_html)):
        print(markdown_report(scorecard))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
