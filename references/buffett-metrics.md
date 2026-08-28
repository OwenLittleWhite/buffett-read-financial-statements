# 巴菲特指标登记表

本文件用于逐条记录用户提供的《巴菲特教你读财报》相关判断指标。当前阶段以忠实登记和代码化设计为主，暂不将派生指标写入数据库，也不自行补充用户未提出的阈值或结论。

## 登记原则

每个指标至少记录以下内容：

| 项目 | 说明 |
|---|---|
| 指标名称 | 用户使用的原始名称 |
| 关注原因 | 用户给出的商业含义或判断逻辑 |
| 所需原始字段 | 对应 `facts.item_code`、`reports` 或 `report_market_snapshots` 字段 |
| 计算公式 | 保留量纲、分母和正负号；无法确定时明确标为待确认 |
| 数据口径 | 单期、年初至今、TTM、多年均值或多年趋势 |
| 报告期范围 | 年报、季报、中报或全部报告期 |
| 缺失值处理 | 默认返回 `NULL`，不得自动按零处理 |
| 代码草案 | 可执行或接近可执行的 SQL/Python 函数形式 |
| 判断规则 | 用户明确给出的阈值、方向或比较基准 |
| 登记状态 | `原始描述`、`待确认`、`已映射字段`、`已代码化`、`已验证` |

## 指标清单

### 1. 毛利率及十年持续性

- 原始描述：损益表中的毛利率；查看过去十年的毛利率是否具有持续性，尤其关注毛利率能否达到 40%。
- 关注原因：通过长期毛利率水平观察企业产品或服务的盈利空间，以及这种盈利能力能否持续。
- 所需字段：
  - `facts.item_code = 'OPERATE_INCOME'`：营业收入。
  - `facts.item_code = 'OPERATE_COST'`：营业成本。
  - `reports.report_type = '年报'`：筛选口径一致的年度数据。
  - 不使用 `TOTAL_OPERATE_COST`，因为营业总成本还包含税金及附加、销售费用、管理费用、财务费用等项目，不等于计算毛利所需的营业成本。
- 公式与口径：`毛利率 = (营业收入 - 营业成本) / 营业收入 × 100%`。取最近十个已披露年报；最新年度尚未披露年报时，不用季度数据替代。
- 适用报告期：年报。
- 判断规则：逐年标记 `毛利率 >= 40%`。持续性应根据十年逐年序列判断，不能只看十年平均值。
- 缺失值处理：营业收入或营业成本缺失、营业收入为零时，毛利率返回 `NULL`，该年度标记为数据不足，不按零处理。
- 状态：已映射字段，已形成 SQL/Python 草案；“持续性”需要达到多少个年度或连续多少年才算通过，等待用户进一步定义。
- 适用边界：上述公式适用于以营业收入和营业成本展示主营经营成果的一般企业。银行、保险、证券等金融企业需要根据其利润表结构另行定义，不可强行套用。

SQL 草案：

```sql
WITH yearly AS (
    SELECT
        f.symbol,
        f.report_date,
        MAX(
            CASE WHEN f.item_code = 'OPERATE_INCOME'
                 THEN CAST(f.numeric_text AS REAL) END
        ) AS operating_revenue,
        MAX(
            CASE WHEN f.item_code = 'OPERATE_COST'
                 THEN CAST(f.numeric_text AS REAL) END
        ) AS operating_cost
    FROM facts AS f
    JOIN reports AS r
      ON r.symbol = f.symbol
     AND r.statement = f.statement
     AND r.report_date = f.report_date
    WHERE f.symbol = :symbol
      AND f.statement = 'income'
      AND r.report_type = '年报'
      AND f.item_code IN ('OPERATE_INCOME', 'OPERATE_COST')
    GROUP BY f.symbol, f.report_date
),
last_ten AS (
    SELECT *
    FROM yearly
    ORDER BY report_date DESC
    LIMIT 10
)
SELECT
    report_date,
    operating_revenue,
    operating_cost,
    CASE
        WHEN operating_revenue IS NULL
          OR operating_cost IS NULL
          OR operating_revenue = 0
        THEN NULL
        ELSE (operating_revenue - operating_cost) / operating_revenue
    END AS gross_margin_rate,
    CASE
        WHEN operating_revenue IS NULL
          OR operating_cost IS NULL
          OR operating_revenue = 0
        THEN NULL
        WHEN (operating_revenue - operating_cost) / operating_revenue >= 0.40
        THEN 1
        ELSE 0
    END AS reaches_40_percent
FROM last_ten
ORDER BY report_date;
```

Python 计算函数草案：

```python
from decimal import Decimal


def gross_margin_rate(
    operating_revenue: Decimal | None,
    operating_cost: Decimal | None,
) -> Decimal | None:
    if operating_revenue is None or operating_cost is None:
        return None
    if operating_revenue == 0:
        return None
    return (operating_revenue - operating_cost) / operating_revenue


def reaches_gross_margin_threshold(
    rate: Decimal | None,
    threshold: Decimal = Decimal("0.40"),
) -> bool | None:
    return None if rate is None else rate >= threshold
```

### 2. 销售费用及管理费用占销售毛利比例

- 原始描述：销售费用以及管理费用占销售毛利的比例，保持在 30% 以下最好。
- 关注原因：观察企业为维持销售和日常管理所付出的期间费用，是否过多消耗主营业务创造的销售毛利。
- 所需字段：
  - `facts.item_code = 'SALE_EXPENSE'`：销售费用。
  - `facts.item_code = 'MANAGE_EXPENSE'`：管理费用。
  - `facts.item_code = 'OPERATE_INCOME'`：营业收入。
  - `facts.item_code = 'OPERATE_COST'`：营业成本。
  - `reports.report_type = '年报'`：筛选口径一致的年度数据。
  - `RESEARCH_EXPENSE`（研发费用）不计入分子，因为用户指定的是销售费用与管理费用之和。
- 公式与口径：`费用占销售毛利比例 = (销售费用 + 管理费用) / (营业收入 - 营业成本) × 100%`。沿用最近十个已披露年报、逐年观察的口径。
- 适用报告期：年报。
- 判断规则：逐年标记该比例是否严格 `< 30%`；等于 30% 不属于“30% 以下”。持续性看十年逐年结果，不用十年平均值掩盖个别高费用年度。
- 缺失值处理：任一所需字段缺失时返回 `NULL`。销售毛利小于或等于零时，该比例不具备正常比较意义，返回 `NULL` 并标记为数据不足，不将负数比例误判为达标。
- 状态：已映射字段，已形成 SQL/Python 草案。
- 适用边界：适用于利润表单列营业收入、营业成本、销售费用及管理费用的一般企业；金融企业应按其利润表结构另行定义。

SQL 草案：

```sql
WITH yearly AS (
    SELECT
        f.symbol,
        f.report_date,
        MAX(CASE WHEN f.item_code = 'OPERATE_INCOME'
                 THEN CAST(f.numeric_text AS REAL) END) AS operating_revenue,
        MAX(CASE WHEN f.item_code = 'OPERATE_COST'
                 THEN CAST(f.numeric_text AS REAL) END) AS operating_cost,
        MAX(CASE WHEN f.item_code = 'SALE_EXPENSE'
                 THEN CAST(f.numeric_text AS REAL) END) AS sale_expense,
        MAX(CASE WHEN f.item_code = 'MANAGE_EXPENSE'
                 THEN CAST(f.numeric_text AS REAL) END) AS manage_expense
    FROM facts AS f
    JOIN reports AS r
      ON r.symbol = f.symbol
     AND r.statement = f.statement
     AND r.report_date = f.report_date
    WHERE f.symbol = :symbol
      AND f.statement = 'income'
      AND r.report_type = '年报'
      AND f.item_code IN (
          'OPERATE_INCOME',
          'OPERATE_COST',
          'SALE_EXPENSE',
          'MANAGE_EXPENSE'
      )
    GROUP BY f.symbol, f.report_date
),
last_ten AS (
    SELECT *
    FROM yearly
    ORDER BY report_date DESC
    LIMIT 10
),
calculated AS (
    SELECT
        *,
        operating_revenue - operating_cost AS sales_gross_profit
    FROM last_ten
)
SELECT
    report_date,
    sale_expense,
    manage_expense,
    sales_gross_profit,
    CASE
        WHEN operating_revenue IS NULL
          OR operating_cost IS NULL
          OR sale_expense IS NULL
          OR manage_expense IS NULL
          OR sales_gross_profit <= 0
        THEN NULL
        ELSE (sale_expense + manage_expense) / sales_gross_profit
    END AS expense_to_gross_profit_rate,
    CASE
        WHEN operating_revenue IS NULL
          OR operating_cost IS NULL
          OR sale_expense IS NULL
          OR manage_expense IS NULL
          OR sales_gross_profit <= 0
        THEN NULL
        WHEN (sale_expense + manage_expense) / sales_gross_profit < 0.30
        THEN 1
        ELSE 0
    END AS below_30_percent
FROM calculated
ORDER BY report_date;
```

Python 计算函数草案：

```python
from decimal import Decimal


def expense_to_sales_gross_profit_rate(
    operating_revenue: Decimal | None,
    operating_cost: Decimal | None,
    sale_expense: Decimal | None,
    manage_expense: Decimal | None,
) -> Decimal | None:
    values = (
        operating_revenue,
        operating_cost,
        sale_expense,
        manage_expense,
    )
    if any(value is None for value in values):
        return None
    sales_gross_profit = operating_revenue - operating_cost
    if sales_gross_profit <= 0:
        return None
    return (sale_expense + manage_expense) / sales_gross_profit


def is_below_expense_ratio_threshold(
    rate: Decimal | None,
    threshold: Decimal = Decimal("0.30"),
) -> bool | None:
    return None if rate is None else rate < threshold
```

### 3. 研发开支风险

- 原始描述：研发开支应尽可能回避，尤其是高科技公司。巨额研发一旦失败，会对长期经营前景产生很大影响，未来业务可能不稳定、持续性不强。
- 关注原因：研发投入的结果具有不确定性；当企业持续依赖巨额研发维持竞争力时，研发失败或技术路线变化可能削弱长期盈利的可预测性。
- 所需字段：
  - `facts.item_code = 'RESEARCH_EXPENSE'`：研发费用。
  - `reports.report_type = '年报'`：筛选口径一致的年度数据。
  - 当前数据库没有行业分类或“高科技公司”标志，现阶段只能由用户指定，不能自动猜测。
- 公式与口径：先列出最近十个已披露年报的研发费用原值及变化趋势。用户尚未指定“巨额”的比较基数，因此暂不默认计算研发费用率。
- 适用报告期：年报。
- 判断规则：这是风险规避型指标，研发开支越高、长期越依赖持续研发，经营持续性的风险关注度越高。但在用户明确分母和阈值前，不自动输出达标/不达标结论。
- 缺失值处理：研发费用字段缺失必须保留为 `NULL`，不能按零处理。部分早期报表可能未单列研发费用，相关支出可能包含在管理费用中，跨年比较时应提示口径变化。
- 状态：研发费用字段和十年查询已代码化；“高科技公司”的识别方式、“巨额”的比较基数及阈值待确认。
- 待确认事项：
  - “巨额”是看研发费用绝对值，还是研发费用占营业收入、销售毛利或净利润的比例？
  - 达到多高的比例开始提示风险？
  - 高科技公司由用户指定，还是后续补充行业分类数据后自动识别？

SQL 草案（保留缺失年度）：

```sql
WITH last_ten_annual_reports AS (
    SELECT DISTINCT
        symbol,
        report_date
    FROM reports
    WHERE symbol = :symbol
      AND statement = 'income'
      AND report_type = '年报'
    ORDER BY report_date DESC
    LIMIT 10
)
SELECT
    r.report_date,
    f.numeric_text AS research_expense
FROM last_ten_annual_reports AS r
LEFT JOIN facts AS f
  ON f.symbol = r.symbol
 AND f.statement = 'income'
 AND f.report_date = r.report_date
 AND f.item_code = 'RESEARCH_EXPENSE'
ORDER BY r.report_date;
```

Python 草案（阈值必须由调用方明确传入）：

```python
from decimal import Decimal


def research_expense_ratio(
    research_expense: Decimal | None,
    comparison_base: Decimal | None,
) -> Decimal | None:
    if research_expense is None or comparison_base is None:
        return None
    if comparison_base <= 0:
        return None
    return research_expense / comparison_base


def exceeds_research_expense_limit(
    ratio: Decimal | None,
    maximum_ratio: Decimal,
) -> bool | None:
    return None if ratio is None else ratio > maximum_ratio
```

### 4. 折旧费用占销售毛利比例

- 原始描述：好的公司，折旧费占毛利润的比例较低。
- 关注原因：折旧费用反映既有长期资产在经营中的成本消耗。折旧持续占用较高比例的销售毛利，可能说明企业维持经营需要较重的资产基础；比例较低则更符合轻资产、盈利可持续性较强的特征。
- 所需字段：
  - `facts.statement = 'cashflow'` 且 `item_code = 'FA_IR_DEPR'`：固定资产和投资性房地产折旧。
  - `facts.statement = 'income'` 且 `item_code = 'OPERATE_INCOME'`：营业收入。
  - `facts.statement = 'income'` 且 `item_code = 'OPERATE_COST'`：营业成本。
  - 两张表通过相同的 `symbol` 和 `report_date` 关联，并分别限定 `reports.report_type = '年报'`。
- 公式与口径：`折旧费用占销售毛利比例 = 固定资产和投资性房地产折旧 / (营业收入 - 营业成本) × 100%`。取最近十个已披露年报逐年观察。
- 适用报告期：年报。
- 判断规则：比例越低越好；用户尚未给出“较低”的具体阈值，因此现阶段只输出逐年比例和趋势，不自动判定达标。
- 缺失值处理：折旧、营业收入或营业成本缺失，以及销售毛利小于或等于零时返回 `NULL`，不按零处理。
- 状态：已映射字段，已形成并验证 SQL/Python 草案；具体阈值待确认。
- 口径边界：
  - `FA_IR_DEPR` 来自现金流量表补充资料，东财名称为“固定资产和投资性房地产折旧”，口径比单纯固定资产折旧略宽。
  - `OILGAS_BIOLOGY_DEPR` 和 `IR_DEPR` 是该总项下的拆分项，不再与 `FA_IR_DEPR` 相加，避免重复计算。
  - 该指标衡量会计折旧对毛利的占用，不等同于资本性支出占毛利的比例。

SQL 草案：

```sql
WITH last_ten_annual_reports AS (
    SELECT DISTINCT
        symbol,
        report_date
    FROM reports
    WHERE symbol = :symbol
      AND statement = 'income'
      AND report_type = '年报'
    ORDER BY report_date DESC
    LIMIT 10
),
income_values AS (
    SELECT
        f.symbol,
        f.report_date,
        MAX(CASE WHEN f.item_code = 'OPERATE_INCOME'
                 THEN CAST(f.numeric_text AS REAL) END) AS operating_revenue,
        MAX(CASE WHEN f.item_code = 'OPERATE_COST'
                 THEN CAST(f.numeric_text AS REAL) END) AS operating_cost
    FROM facts AS f
    JOIN reports AS r
      ON r.symbol = f.symbol
     AND r.statement = f.statement
     AND r.report_date = f.report_date
    WHERE f.symbol = :symbol
      AND f.statement = 'income'
      AND r.report_type = '年报'
      AND f.item_code IN ('OPERATE_INCOME', 'OPERATE_COST')
    GROUP BY f.symbol, f.report_date
),
depreciation_values AS (
    SELECT
        f.symbol,
        f.report_date,
        CAST(f.numeric_text AS REAL) AS depreciation_expense
    FROM facts AS f
    JOIN reports AS r
      ON r.symbol = f.symbol
     AND r.statement = f.statement
     AND r.report_date = f.report_date
    WHERE f.symbol = :symbol
      AND f.statement = 'cashflow'
      AND r.report_type = '年报'
      AND f.item_code = 'FA_IR_DEPR'
)
SELECT
    a.report_date,
    d.depreciation_expense,
    i.operating_revenue - i.operating_cost AS sales_gross_profit,
    CASE
        WHEN d.depreciation_expense IS NULL
          OR i.operating_revenue IS NULL
          OR i.operating_cost IS NULL
          OR i.operating_revenue - i.operating_cost <= 0
        THEN NULL
        ELSE d.depreciation_expense
             / (i.operating_revenue - i.operating_cost)
    END AS depreciation_to_gross_profit_rate
FROM last_ten_annual_reports AS a
LEFT JOIN income_values AS i
  ON i.symbol = a.symbol
 AND i.report_date = a.report_date
LEFT JOIN depreciation_values AS d
  ON d.symbol = a.symbol
 AND d.report_date = a.report_date
ORDER BY a.report_date;
```

Python 计算函数草案：

```python
from decimal import Decimal


def depreciation_to_sales_gross_profit_rate(
    depreciation_expense: Decimal | None,
    operating_revenue: Decimal | None,
    operating_cost: Decimal | None,
) -> Decimal | None:
    if any(
        value is None
        for value in (depreciation_expense, operating_revenue, operating_cost)
    ):
        return None
    sales_gross_profit = operating_revenue - operating_cost
    if sales_gross_profit <= 0:
        return None
    return depreciation_expense / sales_gross_profit
```

### 5. 利息支出占营业利润比例

- 原始描述：好公司几乎不支付利息，利息支出均小于其营业利润的 15%。
- 关注原因：较低的利息负担说明企业较少依赖有息债务，经营利润不容易被融资成本侵蚀，财务风险和经营波动风险相对较低。
- 所需字段：
  - `facts.item_code = 'FE_INTEREST_EXPENSE'`：一般企业财务费用明细中的利息费用。
  - `facts.item_code = 'OPERATE_PROFIT'`：营业利润。
  - `reports.report_type = '年报'`：筛选口径一致的年度数据。
- 公式与口径：`利息支出占营业利润比例 = 利息费用 / 营业利润 × 100%`。取最近十个已披露年报逐年观察。
- 适用报告期：年报。
- 判断规则：每个年度都必须严格 `< 15%`。只有十个年度的数据均完整且全部低于 15%，才能得出“过去十年均低于 15%”；任一年缺失时整体结论为数据不足，而不是自动通过。
- 缺失值处理：利息费用或营业利润缺失、营业利润小于或等于零时，该年度比例返回 `NULL`。利息费用为零可以正常计算为 0%。
- 状态：已映射字段，已形成并验证 SQL/Python 草案。
- 口径边界：
  - 一般企业使用 `FE_INTEREST_EXPENSE`，不能用整个 `FINANCE_EXPENSE`（财务费用）替代利息费用。
  - `INTEREST_EXPENSE` 是银行等不同利润表结构中的独立利息支出项目；金融企业需要单独映射和解释，不直接套用一般企业口径。

SQL 草案：

```sql
WITH last_ten_annual_reports AS (
    SELECT DISTINCT
        symbol,
        report_date
    FROM reports
    WHERE symbol = :symbol
      AND statement = 'income'
      AND report_type = '年报'
    ORDER BY report_date DESC
    LIMIT 10
),
income_values AS (
    SELECT
        f.symbol,
        f.report_date,
        MAX(CASE WHEN f.item_code = 'FE_INTEREST_EXPENSE'
                 THEN CAST(f.numeric_text AS REAL) END) AS interest_expense,
        MAX(CASE WHEN f.item_code = 'OPERATE_PROFIT'
                 THEN CAST(f.numeric_text AS REAL) END) AS operating_profit
    FROM facts AS f
    WHERE f.symbol = :symbol
      AND f.statement = 'income'
      AND f.item_code IN ('FE_INTEREST_EXPENSE', 'OPERATE_PROFIT')
    GROUP BY f.symbol, f.report_date
)
SELECT
    a.report_date,
    i.interest_expense,
    i.operating_profit,
    CASE
        WHEN i.interest_expense IS NULL
          OR i.operating_profit IS NULL
          OR i.operating_profit <= 0
        THEN NULL
        ELSE i.interest_expense / i.operating_profit
    END AS interest_to_operating_profit_rate,
    CASE
        WHEN i.interest_expense IS NULL
          OR i.operating_profit IS NULL
          OR i.operating_profit <= 0
        THEN NULL
        WHEN i.interest_expense / i.operating_profit < 0.15
        THEN 1
        ELSE 0
    END AS below_15_percent
FROM last_ten_annual_reports AS a
LEFT JOIN income_values AS i
  ON i.symbol = a.symbol
 AND i.report_date = a.report_date
ORDER BY a.report_date;
```

Python 计算与十年整体判断草案：

```python
from decimal import Decimal
from typing import Sequence


def interest_to_operating_profit_rate(
    interest_expense: Decimal | None,
    operating_profit: Decimal | None,
) -> Decimal | None:
    if interest_expense is None or operating_profit is None:
        return None
    if operating_profit <= 0:
        return None
    return interest_expense / operating_profit


def all_years_below_interest_threshold(
    rates: Sequence[Decimal | None],
    threshold: Decimal = Decimal("0.15"),
    expected_years: int = 10,
) -> bool | None:
    if len(rates) != expected_years or any(rate is None for rate in rates):
        return None
    return all(rate < threshold for rate in rates)
```

### 6. 税前利润及跨公司投资比较

- 原始描述：税前利润用于方便对不同公司的投资进行比较；把股票看作“股权债券”，以买入价格为本金、每股税前利润为会增长的票息。用户明确要求“买入成本税前收益率”使用现在准备买入时的股票价格，而不是历史报告日价格；采用最新已完成交易日收盘价，盘中尚未收盘时采用昨收。
- 关注原因：税前利润排除了不同所得税负担对净利润的影响。当前买入成本税前收益率用于估算按现在价格买入时，公司最新完整年度税前盈利相对于买入成本的静态比例；每股税前利润增长则用于观察这张“票息”过去是否持续提高。
- 所需字段：
  - `facts.item_code = 'TOTAL_PROFIT'`：利润总额，对应税前利润。
  - `reports.report_type = '年报'`：筛选口径一致的年度数据。
  - `report_market_snapshots.close_price`：报告日当日或之前最近交易日的不复权收盘价。
  - `report_market_snapshots.total_shares`：该实际交易日对应的期末总股本。
  - `report_market_snapshots.trade_date`：实际采用的交易日期。
  - `report_market_snapshots.market_cap`：用于对税前盈利收益率进行等价校验。
  - 东财当前行情 `f43`：最新价；只有行情时间表明当日已经收盘，或行情日期早于今天时，才作为最新完整收盘价。
  - 东财当前行情 `f60`：昨收；当日尚未收盘时使用。
  - 东财当前行情 `f84`：当前总股本；缺失时回退至最新年报对应的股本快照，并明确标记来源。
  - 东财当前行情 `f86`：行情更新时间，用于判断当日收盘是否完成。
- 公式与口径：
  - `每股税前利润（估算） = 利润总额 / 报告期实际交易日对应的总股本`。
  - `报告期收盘价税前盈利收益率 = 每股税前利润（估算） / 收盘价`，等价于 `利润总额 / 市值`。
  - `当前股本口径每股税前利润（估算） = 最新完整年报利润总额 / 当前总股本`。
  - `当前买入成本税前收益率 = 当前股本口径每股税前利润（估算） / 最新已完成交易日收盘价`。
  - `每股税前利润 CAGR = (末年每股税前利润 / 首年每股税前利润) ^ (1 / 间隔年数) - 1`。
  - 同时展示最近一年同比增长率，并计算年度同比增长率中位数以减少单个异常年份对“典型增长率”的影响；再沿用每股收益十年趋势规则，展示上涨次数、最长连续下降和强/温和/未满足分档。最近一年同比不能被长期分档或中位数替代。
  - 当前买入成本税前收益率默认每次分析时刷新当前行情，不使用历史报告日收盘价冒充当前价格。真正成交后的个人持仓成本收益率仍应固定使用实际成交价和费用；当前收盘价只是下一个交易日可能买入价格的近似，不保证实际成交价。
- 适用报告期：年报；跨公司比较时应使用同一报告期，不能将不同年度直接横向排列。
- 判断规则：用户尚未给出数值阈值。当前用于展示、排序和后续归一化，不自动判定好坏。
- 缺失值处理：税前利润、总股本或收盘价缺失，或者总股本/收盘价小于等于零时，对应派生结果返回 `NULL`。当前行情请求失败时，保留错误信息并只令当前买入指标缺失，不能用陈旧报告日价格代替。税前亏损保留负数；CAGR 的首尾值任一小于等于零时返回 `NULL`。
- 状态：税前利润、当前买入成本税前收益率、历史报告期税前盈利收益率、每股税前利润 CAGR、最近一年同比、同比增长中位数和十年趋势均已代码化，在读取时计算，不持久化派生字段。
- 口径边界：
  - 东财的 `TOTAL_PROFIT` 中文名称为“利润总额”，即利润表中所得税费用之前的利润。
  - 年度利润总额是期间流量，期末总股本是时点值，因此每股税前利润只是可复核的估算值，不是法定披露的每股指标。发生大额增发、回购或注销时，应结合股本变动记录复核，不能把纯粹由股本减少造成的每股增长全部归因于经营改善。
  - CAGR 只概括首尾变化，不是未来增长承诺；需要与同比中位数、上涨次数、总税前利润增长和股本变化一起阅读。

同一报告期跨公司查询草案：

```sql
SELECT
    f.symbol,
    c.security_name,
    f.report_date,
    f.numeric_text AS pretax_profit,
    r.currency
FROM facts AS f
JOIN reports AS r
  ON r.symbol = f.symbol
 AND r.statement = f.statement
 AND r.report_date = f.report_date
JOIN companies AS c
  ON c.symbol = f.symbol
WHERE f.statement = 'income'
  AND f.item_code = 'TOTAL_PROFIT'
  AND f.report_date = :report_date
  AND r.report_type = '年报'
ORDER BY CAST(f.numeric_text AS REAL) DESC;
```

税前收益率函数：

```python
from decimal import Decimal


def pretax_earnings_yield(
    pretax_profit: Decimal | None,
    report_date_market_cap: Decimal | None,
) -> Decimal | None:
    if pretax_profit is None or report_date_market_cap is None:
        return None
    if report_date_market_cap <= 0:
        return None
    return pretax_profit / report_date_market_cap
```

### 7. 净利润长期增长、净利率分档及竞争对手比较

- 原始描述：净利润能够保持长期增长的态势；净利润占总收入的比例是否高于对手；净利率保持在 20% 以上，企业可能具有长期的相对竞争优势；低于 10% 通常属于高度竞争行业；10%—20% 是可能存在长期投资价值公司的灰色地带。银行和金融类公司不适用这一分档；异常高的净利率也可能对应巨大风险。
- 关注原因：长期增长的净利润体现企业持续创造利润的能力；较高且优于竞争对手的净利率，说明企业把收入转化为最终利润的效率更强。
- 所需字段：
  - `facts.item_code = 'NETPROFIT'`：净利润。
  - `facts.item_code = 'TOTAL_OPERATE_INCOME'`：营业总收入。
  - `reports.report_type = '年报'`：筛选口径一致的年度数据。
- 公式与口径：
  - `净利率 = 净利润 / 营业总收入 × 100%`。
  - 取最近十个已披露年报，逐年展示净利润、相对上年的增长额、同比增长率及净利率。
  - 竞争对手比较使用相同报告期、相同公式计算的净利率，并由调用方显式传入竞争对手股票列表。
- 适用报告期：年报。
- 判断规则：
  - 逐年标记净利润是否高于上一年；长期增长结论结合完整十年序列判断，不能只比较首尾两年。
  - 净利率 `< 10%`：标记为“高度竞争”；这是一项行业竞争风险提示，不把单个年度结果写成绝对结论。
  - 净利率 `>= 10%` 且 `< 20%`：标记为“灰色地带”，可能存在值得继续发掘的长期投资价值，但仍需结合长期趋势和其他指标分析。
  - 净利率 `>= 20%`：标记为“可能具有长期相对竞争优势”。“20% 以上”在这里按包含 20% 处理。
  - 只有十个年度净利率数据完整且每年均 `>= 20%`，才能认定“十年一直保持在 20% 以上”。
  - 银行及其他金融类公司返回“不适用”，不进入上述三个分档。当前数据库没有行业分类字段，必须由调用方明确指定，不能根据公司名称或股票代码自动猜测。
  - “过高的净利率可能承担巨大风险”先作为定性风险提示。用户尚未给出“过高”的数值阈值，因此程序不擅自增加自动风险线或达标字段。
  - 与对手比较时按同一报告期净利率从高到低排列；当前数据库没有行业或竞争关系字段，不能自动把所有同行识别为对手。
- 缺失值处理：净利润或营业总收入缺失、营业总收入小于或等于零时，净利率返回 `NULL`。上一年度净利润小于或等于零时，同比增长率返回 `NULL`，但仍保留净利润金额和增减额。
- 状态：已映射字段，已形成并验证十年趋势、10%/20% 分档、金融类不适用及同期间横向比较代码；竞争对手列表与金融类标志需在实际调用时提供。“过高”风险阈值待确认。
- 口径边界：
  - 本指标按用户原话使用 `NETPROFIT`（净利润），不替换为 `PARENT_NETPROFIT`（归属于母公司股东的净利润）。
  - 不同公司的合并范围、业务模式和一次性损益可能不同，净利率排名应与长期趋势一起看，不能只依据单年排名下结论。

十年趋势与净利率 SQL 草案：

```sql
WITH yearly AS (
    SELECT
        f.symbol,
        f.report_date,
        MAX(CASE WHEN f.item_code = 'NETPROFIT'
                 THEN CAST(f.numeric_text AS REAL) END) AS net_profit,
        MAX(CASE WHEN f.item_code = 'TOTAL_OPERATE_INCOME'
                 THEN CAST(f.numeric_text AS REAL) END) AS total_operating_income
    FROM facts AS f
    JOIN reports AS r
      ON r.symbol = f.symbol
     AND r.statement = f.statement
     AND r.report_date = f.report_date
    WHERE f.symbol = :symbol
      AND f.statement = 'income'
      AND r.report_type = '年报'
      AND f.item_code IN ('NETPROFIT', 'TOTAL_OPERATE_INCOME')
    GROUP BY f.symbol, f.report_date
),
last_ten AS (
    SELECT *
    FROM yearly
    ORDER BY report_date DESC
    LIMIT 10
),
with_previous AS (
    SELECT
        *,
        LAG(net_profit) OVER (ORDER BY report_date) AS previous_net_profit
    FROM last_ten
)
SELECT
    report_date,
    net_profit,
    total_operating_income,
    net_profit - previous_net_profit AS net_profit_growth_amount,
    CASE
        WHEN previous_net_profit IS NULL OR previous_net_profit <= 0
        THEN NULL
        ELSE (net_profit - previous_net_profit) / previous_net_profit
    END AS net_profit_growth_rate,
    CASE
        WHEN previous_net_profit IS NULL OR net_profit IS NULL
        THEN NULL
        WHEN net_profit > previous_net_profit THEN 1
        ELSE 0
    END AS higher_than_previous_year,
    CASE
        WHEN net_profit IS NULL
          OR total_operating_income IS NULL
          OR total_operating_income <= 0
        THEN NULL
        ELSE net_profit / total_operating_income
    END AS net_profit_margin,
    CASE
        WHEN net_profit IS NULL
          OR total_operating_income IS NULL
          OR total_operating_income <= 0
        THEN NULL
        WHEN net_profit / total_operating_income >= 0.20 THEN 1
        ELSE 0
    END AS at_least_20_percent,
    CASE
        WHEN :is_financial = 1 THEN 'not_applicable'
        WHEN net_profit IS NULL
          OR total_operating_income IS NULL
          OR total_operating_income <= 0
        THEN 'insufficient_data'
        WHEN net_profit / total_operating_income < 0.10
        THEN 'highly_competitive'
        WHEN net_profit / total_operating_income < 0.20
        THEN 'gray_zone'
        ELSE 'possible_competitive_advantage'
    END AS net_profit_margin_category
FROM with_previous
ORDER BY report_date;
```

Python 计算与完整性判断草案：

```python
from decimal import Decimal
from typing import Sequence


def net_profit_margin(
    net_profit: Decimal | None,
    total_operating_income: Decimal | None,
) -> Decimal | None:
    if net_profit is None or total_operating_income is None:
        return None
    if total_operating_income <= 0:
        return None
    return net_profit / total_operating_income


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


def all_years_at_least_net_margin_threshold(
    rates: Sequence[Decimal | None],
    threshold: Decimal = Decimal("0.20"),
    expected_years: int = 10,
) -> bool | None:
    if len(rates) != expected_years or any(rate is None for rate in rates):
        return None
    return all(rate >= threshold for rate in rates)
```

### 8. 基本每股收益长期上涨趋势

- 原始描述：寻找每股收益长期呈连续上涨态势的公司；不再要求十年中每一年都上涨，以免标准过于苛刻。
- 关注原因：每股收益持续上升说明企业为每一股普通股创造的利润长期增长，比只观察净利润总额更能减少股本扩张对增长判断的干扰。
- 所需字段：
  - `facts.item_code = 'BASIC_EPS'`：基本每股收益。
  - `reports.report_type = '年报'`：筛选口径一致的年度数据。
- 公式与口径：取最近十个已披露年报的基本每股收益，按报告日期升序逐年比较。十个年度对应九次相邻年度比较。
- 适用报告期：年报。
- 判断规则：
  - 十个年度数据必须完整。
  - `强上涨趋势`：九次相邻年度比较全部严格上涨，保留原来的严格标准作为加分项。
  - `温和上涨趋势`（默认）：九次比较中至少六次严格上涨、期末 EPS 高于期初 EPS、最近三年平均 EPS 高于最初三年平均 EPS，并且不出现连续三个年度下降。
  - 持平不计入上涨次数，但也不算作下降；偶尔一至两个年度回落不会直接淘汰。
  - “至少六次上涨”和“最多连续两次下降”是当前推荐的可配置默认值，不宣称是书中固定阈值。
- 缺失值处理：任一年度基本每股收益缺失时，整体结论返回数据不足，不跳过该年度后继续比较，也不将缺失值按零处理。
- 状态：已映射字段，已形成并验证逐年查询、强趋势及温和趋势判断代码。
- 口径边界：
  - 按用户原话使用 `BASIC_EPS`（基本每股收益），不与 `DILUTED_EPS`（稀释每股收益）混用。
  - 送股、拆股、合股等股本变化可能导致历史每股收益追溯调整。程序使用东财当前返回值进行比较，但出现异常跳变时应结合原始年报核查可比口径。
  - 趋势判断同时返回首尾增长和年复合增长率作为解释信息，但不单独用复合增长率决定是否通过。

SQL 草案：

```sql
WITH last_ten_annual_reports AS (
    SELECT DISTINCT
        symbol,
        report_date
    FROM reports
    WHERE symbol = :symbol
      AND statement = 'income'
      AND report_type = '年报'
    ORDER BY report_date DESC
    LIMIT 10
),
eps_values AS (
    SELECT
        a.report_date,
        CAST(f.numeric_text AS REAL) AS basic_eps
    FROM last_ten_annual_reports AS a
    LEFT JOIN facts AS f
      ON f.symbol = a.symbol
     AND f.statement = 'income'
     AND f.report_date = a.report_date
     AND f.item_code = 'BASIC_EPS'
),
with_previous AS (
    SELECT
        report_date,
        basic_eps,
        LAG(basic_eps) OVER (ORDER BY report_date) AS previous_basic_eps
    FROM eps_values
)
SELECT
    report_date,
    basic_eps,
    previous_basic_eps,
    CASE
        WHEN previous_basic_eps IS NULL THEN NULL
        WHEN basic_eps IS NULL THEN NULL
        WHEN basic_eps > previous_basic_eps THEN 1
        ELSE 0
    END AS higher_than_previous_year
FROM with_previous
ORDER BY report_date;
```

Python 趋势分级草案：

```python
from decimal import Decimal
from typing import Sequence


def assess_basic_eps_trend(
    annual_eps: Sequence[Decimal | None],
    expected_years: int = 10,
    minimum_rising_comparisons: int = 6,
    maximum_consecutive_declines: int = 2,
) -> dict[str, object] | None:
    if len(annual_eps) != expected_years or any(
        eps is None for eps in annual_eps
    ):
        return None

    comparisons = list(zip(annual_eps, annual_eps[1:]))
    rising_count = sum(current > previous for previous, current in comparisons)

    longest_decline_streak = 0
    current_decline_streak = 0
    for previous, current in comparisons:
        if current < previous:
            current_decline_streak += 1
            longest_decline_streak = max(
                longest_decline_streak,
                current_decline_streak,
            )
        else:
            current_decline_streak = 0

    earliest_three_average = sum(annual_eps[:3]) / Decimal("3")
    latest_three_average = sum(annual_eps[-3:]) / Decimal("3")
    strict_uptrend = rising_count == expected_years - 1
    moderate_uptrend = (
        rising_count >= minimum_rising_comparisons
        and annual_eps[-1] > annual_eps[0]
        and latest_three_average > earliest_three_average
        and longest_decline_streak <= maximum_consecutive_declines
    )

    cagr = None
    if annual_eps[0] > 0 and annual_eps[-1] > 0:
        cagr = (
            float(annual_eps[-1] / annual_eps[0])
            ** (1 / (expected_years - 1))
            - 1
        )

    return {
        "trend_level": (
            "strong" if strict_uptrend
            else "moderate" if moderate_uptrend
            else "not_met"
        ),
        "rising_comparisons": rising_count,
        "total_comparisons": expected_years - 1,
        "longest_decline_streak": longest_decline_streak,
        "first_eps": annual_eps[0],
        "last_eps": annual_eps[-1],
        "earliest_three_average": earliest_three_average,
        "latest_three_average": latest_three_average,
        "cagr": cagr,
    }
```

### 9. 有效所得税率（税前与税后差异）

- 原始描述：税前利润与税后净利润相差多少，也就是承担了百分之多少的所得税。
- 关注原因：用于观察企业利润表中的所得税负担，并解释税前利润转化为税后净利润时减少的比例。
- 所需字段：
  - `facts.item_code = 'TOTAL_PROFIT'`：利润总额，即税前利润。
  - `facts.item_code = 'INCOME_TAX'`：所得税费用。
  - `facts.item_code = 'NETPROFIT'`：净利润，用于校验税前减所得税是否等于税后净利润。
- 公式与口径：
  - `税前税后差额 = 税前利润 - 净利润`。
  - `有效所得税率 = 所得税费用 / 税前利润 × 100%`。
  - 正常情况下，`税前利润 - 所得税费用 = 净利润`；程序应把该等式作为数据一致性校验。
- 适用报告期：年报；也可对其他报告期计算，但跨公司比较必须使用相同期间长度。
- 判断规则：用户尚未提供高低阈值，当前只展示逐年有效税率和变化，不自动判定好坏。
- 缺失值处理：税前利润或所得税费用缺失、税前利润小于或等于零时，有效所得税率返回 `NULL`。税前亏损年度不能用该公式解释为正常税率。
- 状态：已映射字段，已形成并验证代码；2025年五家公司均通过“税前利润减所得税费用等于净利润”的一致性校验。
- 口径边界：
  - 这是利润表上的有效所得税率，不等于当年实际用现金缴纳的所得税比例。
  - `INCOME_TAX` 可能同时受当期所得税、递延所得税、税收优惠和以前年度调整影响。
  - 现金流量表的“支付的各项税费”还包含增值税等其他税种，不能直接替代所得税费用。

SQL 草案：

```sql
SELECT
    f.report_date,
    MAX(CASE WHEN f.item_code = 'TOTAL_PROFIT'
             THEN CAST(f.numeric_text AS REAL) END) AS pretax_profit,
    MAX(CASE WHEN f.item_code = 'INCOME_TAX'
             THEN CAST(f.numeric_text AS REAL) END) AS income_tax_expense,
    MAX(CASE WHEN f.item_code = 'NETPROFIT'
             THEN CAST(f.numeric_text AS REAL) END) AS net_profit,
    CASE
        WHEN MAX(CASE WHEN f.item_code = 'TOTAL_PROFIT'
                      THEN CAST(f.numeric_text AS REAL) END) <= 0
        THEN NULL
        ELSE
            MAX(CASE WHEN f.item_code = 'INCOME_TAX'
                     THEN CAST(f.numeric_text AS REAL) END)
            / MAX(CASE WHEN f.item_code = 'TOTAL_PROFIT'
                       THEN CAST(f.numeric_text AS REAL) END)
    END AS effective_income_tax_rate
FROM facts AS f
JOIN reports AS r
  ON r.symbol = f.symbol
 AND r.statement = f.statement
 AND r.report_date = f.report_date
WHERE f.symbol = :symbol
  AND f.statement = 'income'
  AND r.report_type = '年报'
  AND f.item_code IN ('TOTAL_PROFIT', 'INCOME_TAX', 'NETPROFIT')
GROUP BY f.report_date
ORDER BY f.report_date DESC
LIMIT 10;
```

Python 草案：

```python
from decimal import Decimal


def effective_income_tax_rate(
    pretax_profit: Decimal | None,
    income_tax_expense: Decimal | None,
) -> Decimal | None:
    if pretax_profit is None or income_tax_expense is None:
        return None
    if pretax_profit <= 0:
        return None
    return income_tax_expense / pretax_profit
```

### 10. 现金安全垫、低债务与现金来源质量

- 原始描述：分析资产负债表中的现金和现金等价物。企业经营困难时现金越多越好；重点关注拥有大量现金、没什么债务、没有靠出售股份或资产取得现金，并且过去一直保持盈利的公司。
- 关注原因：现金储备可以在经营困难时提供安全垫；如果现金是在持续盈利过程中积累，而不是依靠举债、股权融资或出售资产换来，其质量通常更高。
- 所需字段：
  - `balance.MONETARYFUNDS`：资产负债表“货币资金”，作为期末现金安全垫的主要观察值。
  - `cashflow.END_CCE`：现金流量表“期末现金及现金等价物余额”，用于并列核对，不能与货币资金静默混为同一口径。
  - `balance.TOTAL_ASSETS`：资产总计，用于计算货币资金占总资产比例。
  - `balance.TOTAL_LIABILITIES`：负债合计，仅作为广义偿付压力参考；它包含应付账款等经营性负债，不等于有息债务。
  - 有息融资债务候选明细：`SHORT_LOAN`、`LONG_LOAN`、`NONCURRENT_LIAB_1YEAR`、`BOND_PAYABLE`、`SHORT_BOND_PAYABLE`、`LEASE_LIAB`。这些项目需逐项展示；字段缺失时不能假设为零。
  - `cashflow.ACCEPT_INVEST_CASH`：吸收投资收到的现金。它可能包含子公司吸收少数股东投资，并不天然等同于上市公司发行股份。
  - `cashflow.SUBSIDIARY_ACCEPT_INVEST`：其中子公司吸收少数股东投资收到的现金；只有总额与该子项均完整时，才可将二者之差作为母公司层面股权融资现金的候选值。
  - `cashflow.DISPOSAL_LONG_ASSET`：处置固定资产、无形资产和其他长期资产收回的现金净额。
  - `cashflow.DISPOSAL_SUBSIDIARY_OTHER`：处置子公司及其他营业单位收到的现金。
  - `income.NETPROFIT`：净利润，用于判断过去是否持续盈利。
- 公式与口径：
  - `货币资金占总资产 = MONETARYFUNDS / TOTAL_ASSETS`。
  - `货币资金覆盖总负债 = MONETARYFUNDS / TOTAL_LIABILITIES`，仅作广义参考，不命名为“现金/有息债务”。
  - `已披露融资债务下限 = 上述有息融资债务候选明细中已披露数值之和`，同时必须输出“已披露组件数/候选组件总数”。该下限用于展示已确认的融资债务，不得把它表述成完整有息债务总额。
  - `完整融资债务合计` 只有在所有纳入字段均有明确数值时才计算，否则返回 `NULL`，避免把未披露误当成零债务。
  - `母公司股权融资现金候选值 = ACCEPT_INVEST_CASH - SUBSIDIARY_ACCEPT_INVEST`；任一字段缺失或结果小于零时返回 `NULL`。
  - “出售资产现金”分别展示处置长期资产和处置子公司的现金流入，不与日常营业收入混合。
  - “过去一直盈利”默认观察最近十个已披露年报，要求每年 `NETPROFIT > 0`；盈利不等于净利润每年增长。
- 适用报告期：资产负债表现金看各年年末时点；股权融资、资产处置和净利润看同一年度年报累计值。
- 判断规则：
  - “现金越多越好”先用绝对金额、占总资产比例和多年趋势展示，不设置单一达标线。
  - “大量现金”和“没什么债务”尚无用户确认的阈值，因此不自动输出组合达标结论。
  - 只有十年净利润数据完整且每年均大于零，才标记“十年持续盈利”。
  - 只有相关现金流字段十年完整且每年均等于零，才可以标记“未发现股权融资现金”或“未发现资产处置现金”；缺失年度会使结论变为“数据不足”。
  - 最终组合判断需要同时观察现金水平、债务、股权融资、资产出售和持续盈利，不能仅凭某个期末现金余额下结论。
- 缺失值处理：所有缺失字段保留为 `NULL`。尤其不能因为东财没有返回某个借款或现金流项目，就把它直接解释为零债务、未发股或未出售资产。
- 状态：字段已核对并加入比较脚本；当前已展示现金金额、现金占总资产/总负债比例、已披露融资债务下限及组件覆盖数、持续盈利年数、吸收投资现金与长期资产处置现金的正流入年数。“大量现金”和“低债务”的阈值待确认，不持久化派生字段。
- 口径边界：
  - 货币资金可能包含受限资金；期末现金及现金等价物通常会排除不满足现金等价物条件的项目。二者差异需要结合财报附注核实。
  - 资产出售可能是正常资产更新，不应看到非零流入就直接判断企业经营恶化；应结合金额、频率及其相对经营现金流的比例。
  - 本指标关注现金来源质量，不等于现金越多就必然创造更高股东回报；长期闲置现金也可能反映资本配置效率较低。

Python 计算草案：

```python
from decimal import Decimal
from typing import Sequence


def ratio(
    numerator: Decimal | None,
    denominator: Decimal | None,
) -> Decimal | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def sum_only_when_complete(
    values: Sequence[Decimal | None],
) -> Decimal | None:
    if not values or any(value is None for value in values):
        return None
    return sum((value for value in values if value is not None), Decimal("0"))


def parent_equity_financing_cash(
    accepted_investment_cash: Decimal | None,
    subsidiary_accepted_investment_cash: Decimal | None,
) -> Decimal | None:
    if accepted_investment_cash is None or subsidiary_accepted_investment_cash is None:
        return None
    candidate = accepted_investment_cash - subsidiary_accepted_investment_cash
    return candidate if candidate >= 0 else None


def continuously_profitable(
    annual_net_profits: Sequence[Decimal | None],
    expected_years: int = 10,
) -> bool | None:
    if len(annual_net_profits) != expected_years:
        return None
    if any(value is None for value in annual_net_profits):
        return None
    return all(value > 0 for value in annual_net_profits if value is not None)


def no_positive_cash_inflow(
    annual_inflows: Sequence[Decimal | None],
    expected_years: int = 10,
) -> bool | None:
    if len(annual_inflows) != expected_years or any(
        value is None for value in annual_inflows
    ):
        return None
    return all(value == 0 for value in annual_inflows if value is not None)
```

### 11. 制造类企业的存货与净利润联动

- 原始描述：制造类企业存货增长时，净利润应当对应增长。存货在某些年份迅速增加、随后又迅速减少，可能说明公司处于高度竞争、时而繁荣时而衰退的行业。
- 所需字段：`balance.INVENTORY`（存货）、`income.NETPROFIT`（净利润）及年报报告日期。当前数据库没有可靠的制造业分类，制造类适用性必须由调用方明确确认。
- 公式与口径：分别计算 `存货同比 = (本年存货-上年存货)/上年存货` 与 `净利润同比 = (本年净利润-上年净利润)/上年净利润`；仅在相邻两个年报数据完整且上年值大于零时计算。统计“存货增长且净利润也增长”的次数，并展示存货同比最大增幅和最大降幅。
- 判断规则：存货增长而净利润没有同步增长是需要解释的背离信号；先展示逐年联动，不自动判定好坏。“迅速增加”“迅速减少”尚无数值阈值，因此程序不得擅自给出周期行业结论。
- 缺失值处理：缺失保留 `NULL`；存货或净利润的上年值小于或等于零时，同比返回 `NULL`。不能用期末存货绝对值替代同比变化。
- 状态：字段、同比序列、最大增减幅以及存货增长时的净利润同增次数已代码化；制造业标志和“迅速”阈值待确认。
- 口径边界：存货变化还可能来自并购、会计政策、产品结构、原材料价格和减值准备。出现背离只能触发进一步分析，不能直接证明竞争激烈。

### 12. 应收账款占销售收入比例及竞争对手比较

- 原始描述：如果一家公司持续显示出比竞争对手更低的应收账款占总销售比率，很可能具有某种相对竞争优势。
- 所需字段：`balance.ACCOUNTS_RECE`（应收账款）与 `income.OPERATE_INCOME`（营业收入）。`NOTE_ACCOUNTS_RECE` 是“应收票据及应收账款”合计项，不能在未说明的情况下替代应收账款。
- 公式与口径：`应收账款占销售收入比例 = 期末应收账款 / 当年营业收入 × 100%`。按最近十个年报逐年计算，并在调用方显式传入的竞争对手集合内按从低到高排名。
- 判断规则：比例越低代表赊销占用相对越少；“持续低于竞争对手”必须观察多个相同报告年度，不能只按单年或十年均值下结论。用户尚未定义需要领先多少年、领先多少个百分点，因此当前只输出十年均值、最新值和低位排名。
- 缺失值处理：应收账款或营业收入缺失、营业收入小于或等于零时返回 `NULL`。缺失年度不参与排名，也不能视作零应收账款。
- 状态：字段、十年均值、最新比例和显式对手组低位排名已代码化；持续性阈值待确认。
- 口径边界：期末应收账款是时点值，营业收入是期间值。若以后改用平均应收账款，应明确采用 `(期初+期末)/2`，不能与当前口径混用。

### 13. 流动比率及持续竞争优势公司的特殊解释

- 原始描述：`流动比率 = 流动资产 / 流动负债`。很多具有持续竞争优势的公司流动比率小于 1，这与传统标准不同，因为其盈利和融资能力足以偿还流动负债。
- 所需字段：`balance.TOTAL_CURRENT_ASSETS`（流动资产合计）与 `balance.TOTAL_CURRENT_LIAB`（流动负债合计）。
- 公式与口径：`流动比率 = 流动资产合计 / 流动负债合计`，观察最近十个年报的均值、最新值及 `<1` 的年度数。
- 判断规则：逐年标记是否 `<1`，但绝不能把“小于 1”单独判定为优秀。只有结合长期盈利、经营现金流、债务结构和融资能力后，才可按用户描述解释为持续竞争优势公司的特殊现象。
- 缺失值处理：任一字段缺失或流动负债小于或等于零时返回 `NULL`。
- 状态：字段、十年均值、最新值及 `<1` 年数已代码化；不创建单项达标结论。
- 口径边界：传统流动比率的风险提示仍然有效。竞争优势叙事不能覆盖短期债务集中到期、现金受限或经营现金流恶化等风险。

### 14. 固定资产稳定性与维持竞争力所需更新支出

- 原始描述：优秀公司的厂房、房产、机器设备经常稳定不变，不需要为了保持竞争力耗费巨额资金更新厂房和设备。
- 所需字段：`balance.FIXED_ASSET`（固定资产）、`balance.CIP`（在建工程，可作扩建线索）、`cashflow.CONSTRUCT_LONG_ASSET`（购建固定资产、无形资产和其他长期资产支付的现金）以及 `cashflow.FA_IR_DEPR`（固定资产和投资性房地产折旧）。
- 公式与口径：展示固定资产原值序列的同比变化、平均绝对同比变化、最大增幅和最大降幅；同时计算`最近十年累计资本开支 / 最近十年累计净利润`。资本开支采用 `CONSTRUCT_LONG_ASSET`，不扣除处置长期资产收到的现金；该字段还包含无形资产和其他长期资产，不能全部解释为厂房设备支出。
- 判断规则：十年累计比率严格 `<25%` 标记为“持续竞争优势较强线索”；`25%`（含）至 `<50%` 标记为“候选”；`>=50%` 标记为“未满足”。阈值只用于该项筛选，不单独形成投资结论。
- 缺失值处理：固定资产上年值小于或等于零时同比返回 `NULL`。资本开支比率必须具有最近十个完整年报的资本开支和净利润，且累计净利润大于零；否则返回 `NULL`，不使用不足十年的数据外推。
- 状态：固定资产最新值、平均绝对变动、最大增减幅、十年累计资本开支、累计净利润、累计比率及 25%/50% 分档已代码化，均在读取时计算。
- 口径边界：固定资产账面稳定可能同时受折旧、减值、处置、并购和在建工程转固影响；不能只根据期末净额判断是否没有更新设备。该现金流字段无法区分维护性和扩张性资本开支，也不包含以非现金方式取得的长期资产。

### 15. 账面无形资产与资产负债表外的持续竞争优势

- 原始描述：除资产负债表中的无形资产外，还要发现其他人不容易发现的表外无形资产，即优秀公司的持续竞争优势及由此产生的长期盈利能力。
- 所需字段：`balance.INTANGIBLE_ASSET`（已确认无形资产）、`balance.GOODWILL`（商誉）和 `balance.TOTAL_ASSETS`。表外品牌、渠道、网络效应、客户黏性或经营制度没有可直接抓取的财务报表字段。
- 公式与口径：仅描述性计算 `已确认无形资产 / 总资产`，并列展示商誉金额。未入账的持续竞争优势只能通过其他已登记指标的长期证据综合推断，例如毛利率持续性、净利率、应收账款率、盈利与 EPS 趋势、低利息负担和资本投入需求。
- 判断规则：不因为账面无形资产或商誉较高就认定存在竞争优势，也不因为账面金额较低就认定没有竞争优势。当前不建立“隐形无形资产金额”或单一护城河评分。
- 缺失值处理：账面字段缺失保留 `NULL`；表外竞争优势标记为“需综合分析”，不能用零代替。
- 状态：已确认无形资产金额、占总资产比例和商誉已代码化；表外竞争优势保留为跨指标定性结论，不持久化派生字段。
- 口径边界：商誉主要来自并购溢价，不等于企业自身创造的持续竞争优势；内部形成的品牌等通常不会按估算价值列入资产负债表。

相关 Python 计算草案：

```python
from decimal import Decimal
from typing import Sequence


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


def accounts_receivable_to_sales(
    accounts_receivable: Decimal | None,
    operating_revenue: Decimal | None,
) -> Decimal | None:
    if accounts_receivable is None or operating_revenue is None:
        return None
    if operating_revenue <= 0:
        return None
    return accounts_receivable / operating_revenue


def current_ratio(
    total_current_assets: Decimal | None,
    total_current_liabilities: Decimal | None,
) -> Decimal | None:
    if total_current_assets is None or total_current_liabilities is None:
        return None
    if total_current_liabilities <= 0:
        return None
    return total_current_assets / total_current_liabilities
```

### 16. 资产回报率及过高时的持续性风险

- 原始描述：`资产回报率 = 净利润 / 总资产`。传统分析通常认为资产回报率越高越好，但过高的资产回报率也可能暗示公司的竞争优势在持续性方面比较脆弱。
- 关注原因：资产回报率反映企业利用资产创造净利润的效率；极高数值也可能来自较小或已充分折旧的资产基础、较低的进入资本门槛，因而不能自动等同于难以复制的长期竞争优势。
- 所需字段：`income.NETPROFIT`（净利润）与 `balance.TOTAL_ASSETS`（期末资产总计）。
- 公式与口径：严格按用户口径计算 `资产回报率 = 当年净利润 / 当年期末资产总计 × 100%`。取最近十个年报，展示十年均值、最新值和历史最高值。
- 适用报告期：年报。净利润是期间值，总资产是期末时点值；所有公司必须使用相同口径。
- 判断规则：不采用“越高越好”的单向结论，也不因为数值高就直接认定竞争优势脆弱。“过高”尚无用户确认的数值阈值，程序只输出数值和风险提示，并结合固定资产更新需求、资本进入门槛、盈利持续性及其他竞争优势证据进一步分析。
- 缺失值处理：净利润或总资产缺失、总资产小于或等于零时返回 `NULL`。
- 状态：十年均值、最新值和历史最高值已加入比较脚本；“过高”的数值阈值待确认，不持久化派生字段。
- 口径边界：常见财务分析也会使用平均总资产，即 `(期初总资产+期末总资产)/2`。本指标为忠实保留用户给出的公式，暂不使用平均总资产，两种口径不得混排。

Python 计算草案：

```python
from decimal import Decimal


def return_on_assets(
    net_profit: Decimal | None,
    ending_total_assets: Decimal | None,
) -> Decimal | None:
    if net_profit is None or ending_total_assets is None:
        return None
    if ending_total_assets <= 0:
        return None
    return net_profit / ending_total_assets
```

### 17. 金融机构短期贷款与长期贷款结构

- 原始描述：投资金融机构时，通常回避短期贷款比长期贷款多的公司。
- 所需字段：`balance.SHORT_LOAN`（短期借款）与 `balance.LONG_LOAN`（长期借款），以及调用方显式提供的金融机构标志。
- 公式与口径：比较同一报告日的短期借款与长期借款原值；不把二者先除以总资产，以免改变用户给出的判断方式。
- 判断规则：仅对显式标记为金融机构且两个字段都完整的公司判断。若 `SHORT_LOAN > LONG_LOAN`，输出“回避信号”；相等或短期借款较少时只标记“未触发”，不据此单独判定为好公司。
- 缺失值处理：任一借款字段缺失时返回“数据不足”，不得将未披露解释为零。
- 状态：比较逻辑已代码化，并复用 `--financial-symbol` 标志；当前数据库没有行业分类，不自动识别金融机构。
- 口径边界：银行等金融机构的资产负债表可能不使用一般工商企业的“短期借款/长期借款”项目，而以客户存款、同业负债等形式融资。字段不适配时必须返回数据不足，不能强行套用。

### 18. 长期贷款及三至四年盈余偿债能力

- 原始描述：具有持续竞争优势的公司通常只有很少的长期贷款或完全没有；一般拥有足够盈余，可以在三至四年内偿还全部长期债务。
- 所需字段：
  - `balance.LONG_LOAN`：长期借款，用于计算明确的长期贷款偿还年限。
  - 长期融资债务候选项目：`LONG_LOAN`、`BOND_PAYABLE`、`LEASE_LIAB`、`NONCURRENT_LIAB_1YEAR`。
  - `income.NETPROFIT`：净利润，作为用户所说“盈余”的当前代码口径。
- 公式与口径：
  - `长期贷款偿还年限 = LONG_LOAN / NETPROFIT`。
  - `完整长期融资债务偿还年限 = 四个候选长期融资债务项目合计 / NETPROFIT`。
  - 如果候选项目不完整，只展示“已披露长期融资债务下限”和组件覆盖数，不计算完整偿还年限。
- 判断规则：完整偿还年限 `<=3` 标记为“三年内”，`>3且<=4` 标记为“四年内”，`>4` 标记为超过四年。只有完整长期债务口径才可套用这一结论；单独的长期贷款偿还年限仅作参考。
- 缺失值处理：净利润缺失或小于等于零、债务字段缺失时返回 `NULL`。不得把缺失债务字段按零处理。
- 状态：长期贷款、已披露长期融资债务下限、组件覆盖数以及完整口径偿还年限已代码化；现有东财数据常省略空白债务项目，因此完整偿还年限可能显示数据不足。
- 口径边界：`NONCURRENT_LIAB_1YEAR` 可能包含一年内到期的借款、债券或租赁负债；纳入是为了覆盖由长期债务转入流动负债的部分。该候选合计仍不是经附注逐项核验后的法定“全部长期债务”。

### 19. 债务股权比率及金融机构例外

- 原始描述：`债务股权比率 = 总负债 / 股东权益`。盈利能力强的公司通常股东权益较高、总负债较低；除金融机构外，债务股权比率低于 `0.8` 较好，并且越低越好。
- 所需字段：`balance.TOTAL_LIABILITIES`（负债合计）、`balance.TOTAL_EQUITY`（股东权益合计）及调用方显式提供的金融机构标志。
- 公式与口径：`债务股权比率 = 负债合计 / 股东权益合计`。取最近十个年报，展示十年均值、最新值，并在显式传入的对手组中按从低到高排名。
- 判断规则：非金融企业最新比率严格 `<0.8` 标记为“较好”；等于 `0.8` 不属于低于 `0.8`。数值越低排名越靠前，但单项达标不能替代盈利能力和债务期限结构分析。金融机构返回“不适用”。
- 缺失值处理：总负债或股东权益缺失、股东权益小于或等于零时返回 `NULL`。负股东权益公司不能用普通正数比率排序。
- 状态：十年均值、最新值、低位排名、`0.8` 阈值和金融机构例外已代码化；不持久化派生字段。
- 口径边界：该公式与“资产负债率=总负债/总资产”不同，也与只使用有息债务的净负债权益比不同，三者不可混用。

相关 Python 计算草案：

```python
from decimal import Decimal


def financial_loan_structure(
    short_loan: Decimal | None,
    long_loan: Decimal | None,
    is_financial: bool,
) -> str:
    if not is_financial:
        return "not_applicable"
    if short_loan is None or long_loan is None:
        return "insufficient_data"
    return "avoid" if short_loan > long_loan else "not_triggered"


def debt_payoff_years(
    debt: Decimal | None,
    net_profit: Decimal | None,
) -> Decimal | None:
    if debt is None or net_profit is None or net_profit <= 0:
        return None
    return debt / net_profit


def debt_to_equity_ratio(
    total_liabilities: Decimal | None,
    total_equity: Decimal | None,
) -> Decimal | None:
    if total_liabilities is None or total_equity is None or total_equity <= 0:
        return None
    return total_liabilities / total_equity


def classify_debt_to_equity(
    ratio: Decimal | None,
    is_financial: bool,
) -> str:
    if is_financial:
        return "not_applicable"
    if ratio is None:
        return "insufficient_data"
    return "below_0_8" if ratio < Decimal("0.8") else "at_or_above_0_8"
```

### 20. 留存收益增长及持续竞争优势

- 原始描述：留存收益增长率是判断公司是否得益于某种持续竞争优势的一项好指标，增长率越快越好。
- 所需字段：`balance.SURPLUS_RESERVE`（盈余公积）与 `balance.UNASSIGN_RPOFIT`（未分配利润）。东财普通公司模板没有单独的“留存收益”字段。
- 公式与口径：按中国会计报表口径计算 `留存收益 = 盈余公积 + 未分配利润`；再计算逐年同比、十年 CAGR、上涨次数和最大年度增幅。所有计算使用年报期末余额。
- 判断规则：增长越快排名越靠前，但不设置固定达标阈值。应同时检查是否持续增长，不能只依据某一年最大增幅或首尾 CAGR 判断持续竞争优势。
- 缺失值处理：两个组成字段任一缺失时，该年留存收益返回 `NULL`；上年留存收益小于或等于零时同比返回 `NULL`；CAGR 要求首尾值均大于零。
- 状态：留存收益原值、十年 CAGR、上涨次数、最大增幅及同组排名已代码化，不持久化派生字段。
- 口径边界：留存收益还会受到现金分红、亏损弥补、会计政策调整和以前年度更正影响。增长快是竞争优势线索，不等于留存资金一定创造了高回报。

### 21. 中国A股库存股、股份回购与注销证据

- 原始描述：库存股是公司回购的股票；需要判断这一概念是否适用于中国股市，并观察回购股份是否注销。
- 适用性：适用于中国A股。资产负债表的 `balance.TREASURY_SHARES`（减：库存股）记录公司回购后尚未处置或注销股份的成本金额，属于股东权益抵减项。
- 所需字段：
  - `balance.TREASURY_SHARES`：库存股账面金额，不是库存股数量。
  - `share_capital_changes.total_shares`：每次股本变动后的总股本。
  - `share_capital_changes.change_reason`：东财股本变动原因，例如“回购”。
  - `balance.SHARE_CAPITAL`：报告期实收资本或股本，用于年报时点交叉核对。
  - `share_repurchase_programs`：东财股票回购计划及最新执行结果，包括计划公告日、实际回购金额、实际回购股数、实施进度和回购目的。
- 公式与口径：逐项比较相邻股本变动记录。如果本次 `total_shares < 上次total_shares`，且 `change_reason` 包含“回购”或“注销”，记录为“回购相关股本减少事件”，并计算减少股数。另按计划公告日在所选区间内筛选回购计划，展示计划数、公告年份数、最新披露已回购金额/股数及注销意向计划数。
- 判断规则：计划公告、实际买入、期末库存股和最终注销是四类不同证据。库存股余额大于零只说明期末仍有回购股份列账；回购计划的实际金额/股数大于零是执行证据；回购/注销原因与总股本同步减少才是股本减少证据。涉及法律确认或重大投资结论时，仍应核对公司公告。
- 缺失值处理：库存股、实际回购金额或股数缺失均不能按零。金额累计只覆盖有明确实际金额的计划，并同时输出有效计划数。缺少上一条股本记录、变动原因或总股本时，不产生注销证据。
- 状态：抓取器新增 `share_repurchase_programs` 原始来源表和稳定 CSV，重复抓取按 `(symbol, repurchase_code)` 更新；原有 `share_capital_changes` 按 `(symbol,effective_date)` 更新。分析脚本已输出回购计划、公告年份、最新披露实际金额/股数、注销意向、库存股及已确认股本减少事件。
- 口径边界：回购计划接口保存的是当前最新披露结果，不是每个历史报告日的点时快照；跨年度计划无法据此把现金精确分摊到各年度。A股回购股份还可能用于员工持股计划、股权激励或转换可转债。只有“买了库存股”不能判断回购最终是否增厚每股价值，还需结合回购价格、资金来源以及同期是否发股。

### 22. 股东权益回报率

- 原始描述：`股东权益回报率 = 净利润 / 股东权益`。具有长期持续竞争优势的公司通常能够保持较高的股东权益回报率。
- 所需字段：`income.NETPROFIT`（净利润）与 `balance.TOTAL_EQUITY`（期末股东权益合计）。
- 公式与口径：严格按用户公式计算 `ROE = 当年净利润 / 当年期末股东权益 × 100%`；展示十年均值、最新值、历史最高值和同组均值排名。
- 判断规则：“较高”需要与自身长期序列及显式竞争对手比较，用户尚未指定固定阈值，因此不自动输出达标/不达标。高ROE必须与财务杠杆一起解释。
- 缺失值处理：净利润或股东权益缺失、股东权益小于或等于零时返回 `NULL`。
- 状态：十年均值、最新值、历史最高值及同组排名已代码化，不持久化派生字段。
- 口径边界：常见分析也会使用平均股东权益，或使用归母净利润/归母股东权益。本指标忠实使用用户给出的“净利润/股东权益合计”，不同口径不得混排。

### 23. 财务杠杆及杠杆驱动利润风险

- 原始描述：避开通过大量财务杠杆获取利润的公司。
- 所需字段：`balance.TOTAL_ASSETS`（总资产）、`balance.TOTAL_EQUITY`（股东权益）、`income.NETPROFIT`（净利润），并关联已计算的ROA和ROE。
- 公式与口径：当前将财务杠杆定义为权益乘数：`财务杠杆 = 总资产 / 股东权益`。由于ROA、ROE都使用期末分母，同一口径下满足 `ROE = ROA × 财务杠杆`，可用来识别高ROE是否主要由较高杠杆放大。
- 判断规则：财务杠杆越高，依靠权益支撑的资产越多，风险关注度越高；“大量杠杆”尚无用户确认的数值阈值，因此只展示十年均值、最新值、最高值和低位排名，不自动判定回避。
- 缺失值处理：总资产或股东权益缺失、股东权益小于或等于零时返回 `NULL`。
- 状态：权益乘数十年均值、最新值、最高值和低位排名已代码化；高杠杆阈值待确认。
- 口径边界：金融机构的商业模式天然使用较高杠杆，必须结合资本充足率和金融行业专用指标另行判断，不能与普通制造或消费企业直接比较。

相关 Python 计算草案：

```python
from decimal import Decimal


def retained_earnings(
    surplus_reserve: Decimal | None,
    unassigned_profit: Decimal | None,
) -> Decimal | None:
    if surplus_reserve is None or unassigned_profit is None:
        return None
    return surplus_reserve + unassigned_profit


def return_on_equity(
    net_profit: Decimal | None,
    total_equity: Decimal | None,
) -> Decimal | None:
    if net_profit is None or total_equity is None or total_equity <= 0:
        return None
    return net_profit / total_equity


def equity_multiplier(
    total_assets: Decimal | None,
    total_equity: Decimal | None,
) -> Decimal | None:
    if total_assets is None or total_equity is None or total_equity <= 0:
        return None
    return total_assets / total_equity
```

### 24. 最近七年全部现金来源及持续经营现金质量

- 原始描述：弄清公司全部现金来自哪里。观察过去七年是依靠借款或发行债券、发行股票、出售资产或部分业务，还是经营现金流入持续大于经营现金流出；持续经营活动获得的现金才是高质量来源。
- 口径原则：资产负债表只能显示期末现金、借款、股本等余额，无法单独解释现金来源。现金来源必须以现金流量表为主，再用资产负债表余额变化和股本变动记录交叉核对。
- 所需字段：
  - 经营活动：`TOTAL_OPERATE_INFLOW`、`TOTAL_OPERATE_OUTFLOW`、`NETCASH_OPERATE`。
  - 债务融资：`RECEIVE_LOAN_CASH`（取得借款）、`ISSUE_BOND`（发行债券）。
  - 股权融资：`ACCEPT_INVEST_CASH`；其中可能包含 `SUBSIDIARY_ACCEPT_INVEST`，不能直接等同于上市公司发行新股。
  - 出售资产或业务：`DISPOSAL_LONG_ASSET`、`DISPOSAL_SUBSIDIARY_OTHER`。
  - 三类净现金流：`NETCASH_OPERATE`、`NETCASH_INVEST`、`NETCASH_FINANCE`。
  - 现金勾稽：`RATE_CHANGE_EFFECT`、`CCE_ADD`、`BEGIN_CCE`、`END_CCE`。
  - 资产负债表交叉核对：`MONETARYFUNDS`、借款和债券项目、`SHARE_CAPITAL`；股本变动原因读取 `share_capital_changes`。
- 公式与口径：
  - 固定取最近七个已披露年报，不使用季度累计值代替年度。
  - `经营净现金流 = 经营现金流入小计 - 经营现金流出小计`，并与源字段 `NETCASH_OPERATE` 做一致性检查。
  - `已知债务融资现金流入下限 = 取得借款收到的现金 + 发行债券收到的现金`。
  - `已知股权融资现金流入下限 = 吸收投资收到的现金`；需要结合子公司吸收投资和股本变动判断是否来自上市公司发股。
  - `已知出售资产/业务现金流入下限 = 处置长期资产现金 + 处置子公司及其他营业单位现金`。
  - `现金等价物净增加额 = 经营净现金流 + 投资净现金流 + 融资净现金流 + 汇率变动影响`，逐年做勾稽检查。
- 判断规则：
  - 七个年度数据完整且 `NETCASH_OPERATE > 0` 每年成立，标记“七年经营现金流持续为正”。
  - 借款发债、股权融资、出售资产或业务的现金流入分别展示累计金额，不能互相抵消后隐藏来源。
  - 经营现金持续为正是高质量现金来源证据；是否“主要依靠经营”尚无用户确认的占比阈值，因此当前不输出综合好坏结论。
  - 若现金增加但经营净现金流不足，应优先检查融资净现金流、资产/业务出售和其他投资活动流入。
- 缺失值处理：现金流明细缺失保留 `NULL`。累计“已知流入”只代表已披露项目下限；未披露不能按零，也不能据此断言公司没有借款、发股或出售资产。
- 状态：最近七年经营/投资/融资净现金累计、经营现金为正年数、已知债务/股权/资产出售流入下限及现金勾稽通过年数已代码化，不持久化派生字段。
- 口径边界：收回投资和取得投资收益等投资现金流入不属于出售经营资产，但会影响投资净现金流；其他经营、投资和融资现金流也可能存在，因此“全部现金来源”最终必须保留三类净现金流总额和原始报表，不能只看用户列举的几类明细。

Python 计算草案：

```python
from decimal import Decimal
from typing import Sequence


def operating_cash_is_positive_for_seven_years(
    annual_operating_cash: Sequence[Decimal | None],
) -> bool | None:
    if len(annual_operating_cash) != 7:
        return None
    if any(value is None for value in annual_operating_cash):
        return None
    return all(value > 0 for value in annual_operating_cash if value is not None)


def cash_change_reconciles(
    operating_cash: Decimal | None,
    investing_cash: Decimal | None,
    financing_cash: Decimal | None,
    exchange_rate_effect: Decimal | None,
    cash_increase: Decimal | None,
    tolerance: Decimal = Decimal("1"),
) -> bool | None:
    values = (
        operating_cash,
        investing_cash,
        financing_cash,
        exchange_rate_effect,
        cash_increase,
    )
    if any(value is None for value in values):
        return None
    expected = operating_cash + investing_cash + financing_cash + exchange_rate_effect
    return abs(expected - cash_increase) <= tolerance
```

### 25. 商誉连续增长与持续并购线索

- 原始描述：如果公司商誉连续几年增加，可以据此判断公司不断并购其他企业；如果被并购企业也具有持续竞争优势，则属于锦上添花。
- 所需字段：`balance.GOODWILL`（商誉）与 `cashflow.OBTAIN_SUBSIDIARY_OTHER`（取得子公司及其他营业单位支付的现金净额）。必要时还需查阅企业合并、商誉构成和被收购业务的财报附注。
- 公式与口径：按最近十个年报逐年比较商誉期末余额，输出上涨次数、有效比较次数、当前连续上涨次数和最长连续上涨次数；同时展示取得子公司现金累计金额及正流出年度数。
- 判断规则：
  - 相邻两个年度商誉均有数据且本年严格大于上年，记为一次上涨；持平、下降或中间缺失都会中断连续序列。
  - 商誉连续增加作为持续并购的强线索；若同期取得子公司现金持续为正，证据更强。
  - “连续几年”尚无用户确认的最少年数，因此程序不擅自设置自动判定阈值。
  - 被并购企业是否具有持续竞争优势不能从收购方商誉余额得出，必须结合被收购业务的毛利率、净利率、ROE、现金流、负债及资本投入需求等长期证据。
- 缺失值处理：商誉或相邻年度数据缺失时不进行该次比较，且连续上涨计数归零。取得子公司现金字段缺失不能按零处理，累计金额只表示已披露下限。
- 状态：商誉上涨次数、当前/最长连续上涨次数、取得子公司现金累计及正值年数已代码化，不持久化派生字段。
- 口径边界：商誉增加通常来自非同一控制下企业合并，但也可能受外币折算、购买价分摊调整等影响；商誉减少可能来自减值、处置或核算调整。以股份支付的并购对价也不会完整出现在“取得子公司支付的现金”中，因此最终结论应核对并购公告和财报附注。

Python 计算草案：

```python
from decimal import Decimal
from typing import Sequence


def goodwill_increase_streaks(
    annual_goodwill: Sequence[Decimal | None],
) -> tuple[int, int]:
    current_streak = 0
    longest_streak = 0
    for previous, current in zip(annual_goodwill, annual_goodwill[1:]):
        if previous is not None and current is not None and current > previous:
            current_streak += 1
            longest_streak = max(longest_streak, current_streak)
        else:
            current_streak = 0
    return current_streak, longest_streak
```

### 26. 长期投资账面价值、潜在未实现价值与被投企业质量

- 原始描述：资产负债表中的长期投资可能以较低的历史账面金额列示；如果被投企业大幅增值，投资的实际价值可能显著高于账面价值。除金额外，还要判断公司投资的是具有持续竞争优势的企业，还是处于高度竞争行业的平庸企业。
- 中国 A 股会计口径修正：“成本价或者市场价值取最小”不是现行长期投资项目的统一计量规则。`长期股权投资`可能采用成本法或权益法，并在适用时计提减值；`其他权益工具投资`、`其他非流动金融资产`等可能按公允价值计量；债权类项目可能采用摊余成本或公允价值计量。旧准则下的`可供出售金融资产`、`持有至到期投资`还可能出现在较早年度。
- 会计依据：财政部《企业会计准则第2号——长期股权投资》（财会〔2014〕14号）和《企业会计准则第22号——金融工具确认和计量》（财会〔2017〕7号）。前者区分对子公司的成本法与对联营、合营企业的权益法；后者对金融资产区分摊余成本、公允价值计入其他综合收益、公允价值计入当期损益等后续计量类别。
- 所需字段：
  - 长期股权投资：`balance.LONG_EQUITY_INVEST`。
  - 其他权益工具投资：`balance.OTHER_EQUITY_INVEST`。
  - 其他非流动金融资产：`balance.OTHER_NONCURRENT_FINASSET`。
  - 债权投资、其他债权投资：`balance.CREDITOR_INVEST`、`balance.OTHER_CREDITOR_INVEST`。
  - 旧准则项目：`balance.AVAILABLE_SALE_FINASSET`、`balance.HOLD_MATURITY_INVEST`。
  - 收益交叉观察：`income.INVEST_INCOME`、`income.INVEST_JOINT_INCOME`。
- 公式与口径：按年报分别展示各原始会计项目；计算`长期股权投资 / 总资产`及其所选年度区间 CAGR。各项目计量基础不同，且新旧准则分类可能发生迁移，不将它们相加成一个“长期投资总额”，也不把总额统一解释为“成本与市价孰低”。
- 潜在未实现价值：成本法或权益法核算的个别股权投资，账面价值可能与当前可实现价值存在较大差异；按公允价值计量的项目则可能已反映部分市场变化。三张汇总报表无法量化这种差额，必须读取年报附注中的被投企业名称、持股比例、核算方法、账面价值、减值、上市状态及可取得的公允价值。
- 合并报表边界：母公司对子公司的长期股权投资会在合并资产负债表中与相应子公司权益抵销，因此合并口径的`长期股权投资`并不是集团全部对外股权布局清单。分析完整投资版图时还要读取合并范围、子公司、联营企业和合营企业附注，必要时对照母公司个别报表。
- 被投企业质量：母公司的汇总长期投资余额不能证明被投企业具有持续竞争优势。应先从官方年报附注提取被投企业清单；被投企业为公开公司时，再对其运行本文件中的长期盈利、利润率、现金流、负债和资本投入等指标。非公开企业披露不足时，结论必须标记为证据不足。
- 判断规则：当前只做金额、占比、趋势和投资收益的事实展示，不因账面金额增长自动判定投资成功，不因账面金额较低自动认定存在“隐藏价值”，也不设置未经用户确认的优劣阈值。
- 缺失值处理：源字段缺失保留 `NULL`。旧准则项目在新准则年度消失不能按零处理；跨准则年度的 CAGR 或趋势应结合重分类附注复核。
- 状态：三张表中的长期投资分类、长期股权投资占总资产比例/CAGR、投资收益和联营合营投资收益已代码化，均为读取时计算，不持久化派生字段。被投企业附注提取、逐家公司竞争优势分析和账面价值—可比市场价值差额尚未实现。

Python 计算草案：

```python
from decimal import Decimal


def long_equity_investment_ratio(
    long_equity_investment: Decimal | None,
    total_assets: Decimal | None,
) -> Decimal | None:
    if (
        long_equity_investment is None
        or total_assets is None
        or total_assets <= 0
    ):
        return None
    return long_equity_investment / total_assets
```

### 27. 优先股与资本结构

- 原始描述：具有持续竞争优势的公司，在其资本结构中通常找不到优先股的身影。
- 关注原因：优先股通常具有普通股之前的股息或清算优先权，并可能带来固定或累积支付压力。没有优先股可作为资本结构简单、对普通股股东更友好的观察线索，但不能单独证明企业具有持续竞争优势。
- 中国 A 股会计口径：应根据合同条款和经济实质判断优先股属于权益工具还是金融负债，不能只看证券名称。财政部《企业会计准则第37号——金融工具列报》（财会〔2017〕14号）要求按金融负债和权益工具定义分类。
- 所需字段：
  - `balance.PREFERRED_SHARES`：所有者权益中“其他权益工具—优先股”。
  - `balance.PREFERRED_SHARES_PAYBALE`：非流动负债中“应付债券—优先股”；`PAYBALE` 是东财源字段的实际拼写，代码保持原样。
- 公式与口径：按最近十个年报分别展示权益类和负债类优先股账面余额，统计任一项目大于零的年度数。两类余额不合并成派生字段，避免掩盖其不同资本属性。
- 判断规则：任一年度任一字段明确大于零，标记“发现优先股余额”。只有所选年报中两类字段全部具有明确数值且均为零，才标记“完整数据中未见余额”。否则标记“数据不足，需查附注”。
- 缺失值处理：东财返回 `NULL` 或事实表无对应数值时保持缺失，绝不将空值当作零或直接断言公司没有优先股。
- 口径边界：资产负债表余额不能完整说明发行、赎回、转换、股息递延、强制付息和清算优先等合同条款。重要结论必须结合年报“其他权益工具”“应付债券”附注及优先股发行公告；还应关注永续债等可能具有类似经济负担的其他混合资本工具。
- 状态：权益类/负债类优先股余额、检出年度数、数据完整度和审慎分类已代码化，读取时计算，不持久化派生字段。

## 代码化约定

- 优先从 `facts.numeric_text` 读取原始精度；只有聚合或比较时才转换为数值类型。
- 通过 `financial_item_definitions` 核对中文名称和东财字段代码，不仅依赖中文文本匹配。
- 利润表、现金流量表的季度数据可能是年初至今累计口径。涉及单季度值时，必须明确是否需要用累计值相减，不能默认转换。
- 多年趋势必须明确使用年报还是全部报告期，避免不同期间长度直接比较。
- 比率必须定义分母为零或缺失时的结果，默认返回 `NULL`。
- 市值类指标按 `report_market_snapshots.report_date` 关联，并使用其中已经匹配好的实际交易日。
- 先以查询或独立分析函数实现派生指标；只有用户明确要求后，才设计派生指标持久化表。

## 单项记录格式

后续每个指标使用以下结构：

```text
### 指标名称

- 原始描述：
- 关注原因：
- 所需字段：
- 公式与口径：
- 适用报告期：
- 判断规则：
- 缺失值处理：
- SQL/Python 草案：
- 状态：
- 待确认事项：
```
