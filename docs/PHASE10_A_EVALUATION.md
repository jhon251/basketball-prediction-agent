# Phase 10-A — Evaluation & Error Analysis（2026-09-28）

**性质**：只读历史评估层。8-L～9-E 全部冻结基线零修改。新增
`evaluation/settled_evaluation.py` + 只读 CLI `evaluate` / `error-analysis`。

## 1. 数据输入

- **主输入**：`SettlementStore.list_settled()`（既有 predict→save→settle
  链路产物；当前真实库仅 7 条 settled 记录——评估层如实报小样本）；
- **league/season**：经 games 表 join 解析（只读）；
- **per-model 评估**：薄封装冻结的 `run_window_backtest` 引擎
  （train_seasons=None = frozen defaults，无拟合），需要显式窗口
  （`evaluate --per-model --window-start --window-end`）。

## 2. 指标定义（全部来自冻结核心函数）

Accuracy / LogLoss / Brier / ECE（`evaluation.metrics` + `calibration.
build_reliability_table`）；Margin MAE / Total MAE（settled 记录携带
expected_margin/expected_total 与实际比分——**可用即报**）；sample count
每个切片必报。

## 3. 切片规则与样本门槛

| 切片 | 定义 |
|---|---|
| by league / by season | games 表的 league/season 字段 |
| tossup | primary prob ∈ [0.45, 0.55]（9-A/9-B 同一定义） |
| confidence bands | 固定四带：<0.40 / 0.40-0.60 / 0.60-0.80 / ≥0.80 |
| calibration drift | 按 settlement 时间半年桶（YYYY-H1/H2）的可靠性事实——**不是重校准** |
| model disagreement | **settled 记录只存 primary 概率——不可得**，如实返回 `available=False` + 指向 window-backtest，不虚构 |

**小样本守卫**：`MIN_SLICE_SAMPLES=30`；n<30 的切片 `sufficient=False` +
"descriptive only — do not interpret as a stable pattern"（CLI 显示
[SMALL SAMPLE]）。空切片报 n=0 + 指标 None，不编数。

## 4. 评估窗口与防泄漏

`EvaluationFilter(league, season, window_start, window_end)`；window_end 为
**排他性历史 cutoff**（评估层永不读取 cutoff 之后的结果；"now" 永不被引用
——无当前时间输入）。per-model 评估走冻结引擎的 as-of walk-forward（逐场
只看开球前历史）。六联赛完全隔离（league filter + 逐联赛切片）；CBA 双制
经 season 切片自然分离（2024-25 / 2025-26 各自切片）。

## 5. 事实 / 发现 / 研究候选（本阶段输出三分法）

- **事实统计**：全部 SliceMetrics 数值；
- **描述性发现**：仅 sufficient=True 的切片可作描述性比较；
- **研究候选**：本阶段不自动生成任何生产参数；slice 结果不回写 family
  config / calibration / frozen defaults（no-production-mutation 测试钉住：
  评估前后 DB 文件逐字节一致）。

## 6. 禁止事项（测试守卫）

不新增模型/参数/校准；不按单场结果改生产配置；不引入 LLM/网络/随机数/
当前时间；settled 记录缺失的 disagreement 事实不虚构；per-model 评估不
重新拟合（train_seasons=None）。

## 7. 测试与状态

新增 `tests/unit/test_settled_evaluation.py`（15 项）：指标手算校验
（logloss/accuracy/margin MAE）、league/season 过滤与隔离、CBA 双季分离、
空切片不虚构、小样本旗标、置信带完整划分、漂移桶、disagreement 不可得
如实报告、历史 cutoff 排他、**评估只读**（DB 文件 sha256 前后一致）、
确定性、per-model 委托冻结引擎（四模型键齐全）。

storage 零污染；git clean。
