# Phase 9-A — Prediction → Agent Decision Contract（2026-09-28）

**性质**：Agent 层建设。冻结的 Prediction Core（8-L，commit `7e9188c`）是
硬基线，零修改；本阶段只新增"消费冻结输出"的稳定契约。

## 1. 边界（Prediction Core / Agent Layer）

```
Prediction Core（frozen, 8-L）
  PredictionAgent / models / simulation / family_config / RiskAgent
        │  唯一入口：GamePrediction（frozen dataclass）
        ▼
Agent Decision Contract（本阶段，agents/decision_contract.py）
  PredictionContext · PredictionResult · SimulationDistribution
  DiagnosticEvidence · ValidationStatus · DecisionRisk
        ▼
DecisionAgent（agents/decision.py，纯 consumer）
        ▼
Risk / Explanation / 未来的 Decision Agents（扩展点）
```

Agent 层**不重算概率、不覆盖 primary family、不选模型、不修改 core**。
`DecisionAgent.decide()` 恰好调用一次 `PredictionAgent.predict()`，然后把
冻结输出转换为契约对象。

## 2. 契约定义

### PredictionContext（预测所处上下文；无未来信息）
`game_id, league, season, home_team_id, away_team_id, as_of(ISO，信息边界),
primary_family, primary_family_source, validation_status, calibration
(CalibrationProvenance | None), calibration_source("frozen_defaults" 或
"calibrated:<file>"), snapshot_id, regulation_seconds, feature_provenance
(冻结核心 FeatureProvenance 的透传引用)`。

### PredictionResult（预测核心输出统一视图）
`primary_prob_home/away, predicted_winner("home"/"away"/"tie"，0.5 阈值的
事实定义), expected_home/away_score, expected_margin, expected_total,
primary_family, primary_family_source, risk_confidence_score(冻结 RiskAgent
阈值规则的透传——**不是**校准不确定度，不得改名), diagnostics,
simulation_distribution`。
**不删除任何底层输出**：secondary（Elo/Strength/Sim 概率）与完整分布都保留。

### SimulationDistribution（分布提供方接口）
`MonteCarloResult` 的纯透传：n_simulations/seed、三方胜率（含 tie）、
home/away score、margin、total 的完整 `DistributionStats`（mean/median/std/
p5..p95）、比分桶、n_runs。**独立于 primary family**：即使 primary=elo，
分布对象照常完整（测试钉住）。
`ot_probability = None` **按契约不提供**：冻结核心的 MonteCarloResult 未聚合
加时频率，本层不得硬造（记录为扩展点）。

### DiagnosticEvidence（模型间事实性证据）
`p_elo, p_strength, p_sim`；两两 `|Δp|`（diff_elo_strength/_elo_sim/
_strength_sim）；primary 与各 secondary 的差；`directional_disagreement`
（事实定义：任一两概率位于 0.5 两侧）；`margin_std/total_std`（分布离散度
事实）；`provenance` 透传。**全部是事实**：不解释为"模型错误/高风险/质量分"。

### ValidationStatus（决策层状态语义，继承 8-L）
`CONFIGURED`（六联赛现状：显式配置+完整 evidence）｜`VALIDATED`（**保留值，
当前无任何联赛持有，永不允许自动赋值，fallback 不得改称 validated**）｜
`FALLBACK`（未知/未配置联赛→默认 family）｜`UNRESOLVED`｜
`INSUFFICIENT_EVIDENCE`（配置机制显式标记；机制已实现并测试）。
fallback→validated 与 configured→universally-optimal 两条污染路径都有
测试守卫。

## 3. PredictionAgent / RiskAgent 状态

- **PredictionAgent（冻结核心）**：零修改，仍是预测编排器。Agent 层的
  `DecisionAgent` 是它的 consumer（不是替代），满足"调用 frozen core、
  获得统一 PredictionResult、不自行计算、不覆盖 family、保留 provenance、
  deterministic"全部要求。
- **RiskAgent（冻结核心）**：零修改。新增的 `DecisionRisk` 是其上的**事实层
  视图**：`max_pairwise_disagreement`（两两 |Δp| 最大值）、`margin_std/
  total_std`、`tossup_indicator`（定义：primary ∈ [0.45, 0.55] 固定带）、
  core risk flags 透传、非 CONFIGURED 联赛的 `validation_warning`。
  不输出胜率/盈利概率/质量分。

## 4. Provenance 传递链

`GamePrediction.features.provenance`（冻结核心逐场 as-of provenance）→
`PredictionContext.feature_provenance` 与 `DiagnosticEvidence.provenance`
（同一对象引用，测试断言不丢失）；校准 provenance 由调用方显式提供
（`CalibrationProvenance`：params 文件/拟合窗/拟合时间/val logloss/boundary
hits），缺省即 `frozen_defaults`——两者都是明确的 provenance 状态。

## 5. 六联赛隔离与 CBA 双制

每个契约实例只描述一个联赛的一场比赛；league/family/calibration/snapshot
全部来自冻结的逐联赛状态。CBA 的 per-season regulation 通过
`context.regulation_seconds` 透传（2024-25=2880s、2025-26=2400s，由冻结
核心按赛季解析）。

## 6. Ensemble 边界

不变：experimental/diagnostic（8-K 语义）。契约层不读取也不修改 ensemble
权重；若未来 Agent 需要模型比较，经 `DiagnosticEvidence` 的事实字段或
显式带 experimental 标记的 ensemble 输出。

## 7. 未定义/不可提供字段（不虚构）

| 字段 | 状态 | 原因 |
|---|---|---|
| `SimulationDistribution.ot_probability` | `None`（契约性不提供） | 冻结核心未聚合 OT 频率 |
| "calibrated uncertainty" | 不存在 | 当前系统无校准不确定度定义；margin/total std 只以事实名暴露 |
| model quality score | 不存在 | ValidationStatus 不得变成质量分 |
| prediction error estimate | 不存在 | disagreement 是事实，不是误差估计 |

## 8. 扩展点（后续阶段）

- Explanation 层：消费 `DiagnosticEvidence` + `DecisionRisk` 生成解释；
- DecisionAgent 策略：消费 `AgentDecision`（本期已产出完整 bundle）；
- OT 频率：若冻结核心未来在 MonteCarloResult 聚合 OT，则
  `SimulationDistribution.ot_probability` 直接接线（契约已留位）；
- VALIDATED 状态：仅可由显式预注册协议赋值。

## 9. 测试与状态

新增 `tests/unit/test_decision_contract.py`（16 项）：六联赛 CONFIGURED、
fallback 永非 validated、VALIDATED 保留、insufficient_evidence 机制、
context schema、无未来信息、primary 一致性、secondary 保留、disagreement
可复现（确定性）、provenance 不丢失、分布完整性与 family 无关、tie/runs、
DecisionRisk 事实字段与 fallback 警告、校准 provenance、向后兼容（core
输出不受 Agent 层影响）。

storage 零污染；git clean。
