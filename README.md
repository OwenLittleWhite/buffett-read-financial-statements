# Buffett Read Financial Statements

一个用于中国 A 股财报抓取、结构化存储和巴菲特式财务指标分析的 AI Agent Skill。

主要能力：

- 从东方财富抓取资产负债表、利润表和现金流量表，默认保留年报、一季报、中报和三季报。
- 最多抓取 20 个财年，并将原始数据、CSV 和规范化 SQLite 数据库保存在本地。
- 同步报告期对应的交易日行情、股本、流通股本、市值和流通市值。
- 重复执行时按稳定业务键更新数据，不重复插入。
- 按《巴菲特教你读财报》的思路计算和比较历史指标，生成 Markdown、JSON 和彩色 HTML 评分报告。

当前版本不包含未来五年收益、未来 EPS 或年度业绩预测模型。

## 安装

把这句话发给支持 Skills 的 Agent：

```text
请安装并验证这个 Skill：https://github.com/OwenLittleWhite/buffett-read-financial-statements
```

## 使用示例

```text
请使用这个 Skill 分析贵州茅台（SH600519）最近十年的财报。
```

```text
请使用这个 Skill，对比贵州茅台、五粮液、古井贡酒、泸州老窖和山西汾酒
最近十年的财务指标，并生成完整指标表和彩色评分报告。
```

## 手动运行脚本（可选）

脚本仅依赖 Python 标准库，建议使用 Python 3.10 或更高版本。

```bash
python3 scripts/fetch_eastmoney_financials.py SH600519 --years 10

python3 scripts/build_analysis_bundle.py \
  SH600519 SZ000858 SZ000596 SZ000568 SH600809 \
  --start-year 2016 \
  --end-year 2025 \
  --output-prefix data/analysis/liquor_2016_2025
```

数据会写入本地 `data/` 目录。该目录已被 Git 忽略，不会随代码提交。

## 重要说明

- 东方财富是二级数据服务；重要结论应回查交易所披露的正式公告。
- 缺失值不会被静默替换为零。
- 综合评分只是一种比较辅助，应结合指标覆盖率、原始财报和定性判断阅读。
- 本项目不构成投资建议。

详细使用规则见 [SKILL.md](SKILL.md)，数据库结构见 [references/data-schema.md](references/data-schema.md)，指标定义见 [references/buffett-metrics.md](references/buffett-metrics.md)。
