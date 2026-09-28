# Phase 10-C — Decision Log Evaluation Integration（2026-09-28）

**性质**：真实 Agent 历史的只读评估。8-L～9-E、10-A、10-B **零修改**。
新增 `evaluation/log_evaluation.py`（`DecisionLogEvaluator`）+ CLI
`evaluate/error-analysis --source decision-log`。

## 1. 两种证据，永不混合

| 证据种类 | 来源 | 本阶段入口 |
|---|---|---|
| **backtest evidence** | 冻结 walk-forward 引擎（window-backtest / 10-A per_model_evaluation） | `evaluate --per-model` |
| **real Agent historical evidence** | **10-B append-only Decision Log × Settlement Log**（本阶段 `DecisionLogEvaluator`） | `evaluate --source decision-log` |

两者不得合并为同一统计结果（`evidence_kind: real_agent_history` 显式标记）。

## 2. Join 语义（prediction / settlement 分离）

评估时按 **record_id** 关联 decisions.jsonl × settlements.jsonl（首个事件
生效）——**原始 prediction payload 永不回写**（测试：评估前后 records 逐
值不变）。无 settlement 事件 → skip+计数（`skipped_no_settlement`）；
malformed settlement（缺失 actual_home_score 等）→ skip+计数
（`skipped_malformed`），不崩溃不虚构。join stats 透明输出。

## 3. 指标与切片（全部来自已存概率，零模型重跑）

Accuracy/LogLoss/Brier/ECE + margin/total MAE（数据存在时）+ n。
**family 对比**：`by_model` 输出 primary/elo/strength/simulation 四切片
（全部来自已存概率，sim=elo 时数值可不同——存储的是各自真实概率）。
**事实切片**：league、decision_state、validation_status、dispersion_band、
tossup、directional_disagreement、calibration_source。
**每切片携带 n + sufficient（MIN_SLICE_SAMPLES=30）**；n<30 →
descriptive only；空切片指标 None。

## 4. 过滤与 cutoff

`LogEvaluationFilter(league, season, family, window_start, window_end)`；
window_end 为**排他历史 cutoff**（cutoff 后的记录不读）；六联赛完全隔离
（league 过滤 + 逐联赛切片）；CBA 2024-25=2880s / 2025-26=2400s 逐记录
保留（测试：regs=[2400,2880] 共存）。

## 5. 禁止事项（测试守卫）

不重跑预测模型（per-model 来自已存概率）；不修改 10-B 原始记录（评估后
records 逐值不变）；不自动修改 production family/calibration/参数；无
LLM/网络/随机数/当前时间；n<30 不给规律性结论。

## 6. 测试与状态

新增 `tests/unit/test_log_evaluation.py`（15 项）：join 语义（settled 未
settle 跳过计数、malformed 跳过、**record_id 关联修复了同场多 as_of 错配**）、
prediction payload 零修改、排他 cutoff、league 隔离、CBA 双制、by_model
键与正确性、family 过滤、切片守卫、decision_state/validation/calibration
切片、确定性、10-A 兼容、10-B 记录零改动。

storage 零污染；git clean。
