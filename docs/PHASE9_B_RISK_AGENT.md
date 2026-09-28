# Phase 9-B — RiskAgent & Evidence Analysis Layer（2026-09-28）

**性质**：风险/证据分析层。Prediction Core（8-L）与 9-A Contract **零修改**；
新增 `agents/risk_analysis.py` 的 `RiskAnalysisAgent` —— 一个**纯 consumer**，
把 9-A `AgentDecision` bundle 中**已存在的事实**组织成结构化 `RiskResult`。

## 1. 职责与边界

```
AgentDecision（9-A：Context + Result + DiagnosticEvidence + DecisionRisk + Status）
        ▼  RiskAnalysisAgent.analyze()   —— 纯函数、无状态、确定性
RiskResult（五类事实 + flags + validation_status + core_risk 透传）
        ▼
Explanation Layer（未来）：基于事实组织自然语言（本阶段不做）
```

**RiskAgent 不**：重算概率、修改 primary_prob/family、选择模型、修改
calibration、使用未来信息、估算 OT 概率、输出综合风险分。
**冻结核心的 `RiskAgent`（risk_agent.py）零修改**——本层名 `RiskAnalysisAgent`，
二者是消费关系不是替代关系。

## 2. RiskResult Contract（五类事实）

| 类别 | 字段 | 语义边界 |
|---|---|---|
| **probability_facts** | primary prob/home/away、predicted_winner、`distance_from_tossup=\|p-0.5\|`、`tossup_indicator`（**9-A 同一定义**，band=(0.45,0.55) 常量透出）、band 本身 | 事实；不定义"错误概率" |
| **model_agreement_facts** | 三模型概率、两两 \|Δp\|、max_pairwise（9-A 透传）、directional flag、`dispersion_band`（**描述性数值桶**：directional_disagreement / ≤0.05 / 0.05-0.15 / >0.15——是范围描述，不是质量等级）、primary-vs-secondary 差 | 分歧≠失败 |
| **distribution_facts** | n_runs、tie、margin/total 的 std、p5/p50/p95、`width=p95-p5`、score std、`ot_probability=None`（契约性不提供） | spread≠预测误差≠校准不确定度 |
| **evidence_facts** | validation_status、calibration_source、has_calibration、`known_limitations`（**按状态固定的 factual 陈述**，如"configured 不等于 universally optimal"、"fallback 不是 validated 选择"） | CONFIGURED≠最优；FALLBACK≠VALIDATED |
| **provenance_facts** | league/season/as_of/snapshot/model_version/**regulation_seconds**/feature_provenance 引用 | 透传，不丢失 |

`flags`（确定性事实旗标）：`tossup_band`、`directional_model_disagreement`、
`validation_fallback/unresolved/insufficient_evidence`、
`calibration_frozen_defaults`、`core:<code>`（冻结核心 RiskFlag 透传）。
**无综合评分**：`RiskResult` 及各 facts 类 schema 中不存在
risk_score/quality/uncertainty 等字段（schema 测试钉住）。

## 3. DecisionRisk 使用方式

9-A 的 `DecisionRisk` 原样透传（`RiskResult.core_risk`，字段名与语义零改动，
测试钉住 `{"max_pairwise_disagreement","margin_std","total_std",
"tossup_indicator","core_risk_flags","validation_warning"}`）；其数值被
**引用**（如 tossup_indicator 复用同一布尔），从不重算。

## 4. Simulation Distribution 消费

分布事实直接取自 9-A `SimulationDistribution`（冻结 core 的
`DistributionStats` 值）；width = p95−p5 为纯算术。`ot_probability` 保持
`None`——不为核心缺失的聚合硬造估算。stats 为 None 的 bundle（非核心
产物）以 Optional 字段安全通过，不崩溃。

## 5. 六联赛隔离与 CBA 双制

分析无状态、无共享阈值；一切来自当前比赛的 contract。CBA
2024-25=2880s / 2025-26=2400s 经 `provenance_facts.regulation_seconds`
逐字透传（测试断言两季值不同且各自正确）。

## 6. 明确不提供的指标（不虚构）

综合风险分、模型质量分、胜率可信度分、校准不确定度、预测误差概率、
OT 概率（核心未提供）、历史深度指标（contract 未携带）。这些是未来
Explanation/Decision 层的扩展点，均需显式定义与预注册。

## 7. 与未来 Explanation Layer 的边界

本层只产出事实与固定定义的指示器；任何自然语言（"分歧较大，注意"）
属于 Explanation 层——它消费 `RiskResult`，且同样不得改动概率或 family。

## 8. 测试与状态

新增 `tests/unit/test_risk_analysis.py`（20 项）：schema 与无综合分、
9-A band 定义一致、确定性（同 bundle 逐字段相等）、无状态、事实回声
（概率/family 逐值不变）、disagreement 精确匹配、dispersion 桶、spread
语义（width=p95−p5）、ot=None、分布对象不被改动、configured/fallback
事实与旗标、provenance 透传、无共享状态、CBA 双制透传、无未来时间字段、
DecisionRisk/core RiskAgent 语义零改动。

storage 零污染；git clean。
