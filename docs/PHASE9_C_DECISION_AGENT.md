# Phase 9-C — DecisionAgent & Auditable Decision Record（2026-09-28）

**性质**：可审计决策协议。Prediction Core（8-L）、9-A Contract、9-B
RiskAnalysisAgent **零修改**。新增 `agents/decision_record.py`。

## 1. DecisionAgent 职责与命名

链路：Prediction Core → AgentDecision（9-A）→ RiskResult（9-B）→
**DecisionRecordAgent** → DecisionRecord。
（9-A 已有 `DecisionAgent` = 冻结核心包装器；本阶段决策层因此命名
`DecisionRecordAgent`，避免歧义。）它是**纯 consumer**：不重算概率、不修改
primary family、不重拟合、不使用未来比赛结果或 test 数据、不依赖时间/随机/
网络/LLM（源码级断言测试钉住）。

## 2. DecisionRecord schema

`game_id, league, season, home/away_team_id, as_of, primary_family,
validation_status` + 四个块：
- **prediction_summary**：9-A `PredictionResult` 的逐值透传（prob/winner/
  margin/total/family）——概率**永不重算**；
- **evidence_summary**：三模型概率、pairwise Δp、directional flag、
  Simulation 分布事实（n_runs/tie/std/分位数/ot_probability=None 契约）、
  validation status、calibration provenance、known limitations；
- **risk_summary**：**9-B RiskResult 原对象按引用嵌入**（测试钉住
  `record.risk_summary is risk`），从不重定义；
- **decision_state + rule_trace + evidence_references**（见 §4/§5）。

## 3. Decision States（有限、显式）

| State | 触发（显式规则，顺序固定） |
|---|---|
| `FALLBACK` | validation_status == FALLBACK |
| `UNRESOLVED` | validation_status == UNRESOLVED |
| `INSUFFICIENT_EVIDENCE` | validation_status == INSUFFICIENT_EVIDENCE |
| `PREDICTION_WITH_CAUTION` | CONFIGURED 且 tossup_indicator **且** directional_disagreement（两事实合取） |
| `PREDICTION_SUPPORTED` | 其余 CONFIGURED 情形 |

状态是"当前证据条件下的决策状态"，**不是**准确率/胜率/错误概率/盈利概率/
质量分。`DecisionState` 枚举**没有 validated 成员**（fallback 永无法升级，
结构上禁止；测试钉住）。第一版只使用 9-A/9-B 已有事实与定义，不引入任何
新阈值或权重（caution 用合取规则；单事实不触发——测试覆盖三种组合）。

## 4. Evidence Trace（decision → evidence → source）

每条 `EvidenceReference` 记录 name/location/source 七项固定引用：
primary_probability、model_agreement、distribution、validation_status、
calibration、provenance、risk_facts——每项指向契约中的具体位置与产生它的
冻结层。`rule_trace` 记录命中的 rule_id 与逐条件 (fact, value)。

## 5. Simulation 消费

只读 9-A `SimulationDistribution`（冻结 core 的 `DistributionStats`）；
不重跑 Simulation、不推导 OT；`ot_probability=None` 契约保持（负向测试：
分布缺失时记录 None 而非虚构值，且 9-B facts 镜像该缺失）。

## 6. 六联赛隔离与 CBA 双制

决策无共享状态；规则是 (decision, risk) 的纯函数（测试：相同输入 → 相同
trace）。CBA 2024-25=2880s / 2025-26=2400s 经 context 透传，两季永不共用
一个 regulation 值。

## 7. 负向守卫（测试）

- 冻结 dataclass 不可变：修改 `primary_prob_home` / `primary_family` 抛异常；
- fallback → validated 无路径（枚举无成员）；
- test 数据不能影响规则（规则是纯函数，重复求值恒等）；
- 分布缺失 → None 透传，不虚构；
- DecisionState/DecisionRisk/RiskAssessment 字段集逐个钉住。

## 8. 明确不提供（本阶段禁止）

自动投注/金额/ROI/凯利/买卖建议、综合置信分、模型质量分、LLM 分析、
自动配置修改、自动模型更新。Decision = "对当前预测证据的结构化判断"。

## 9. 测试与状态

新增 `tests/unit/test_decision_record.py`（20 项）：schema、透传、状态路由
（fallback/unresolved/insufficient/caution 合取/supported）、evidence trace、
确定性、无运行时输入（源码断言）、六联赛独立、CBA 双制、负向守卫（冻结
不可变、规则纯函数、缺失分布不虚构）、向后兼容（9-A/9-B 对象零改动）。

storage 零污染；git clean。
