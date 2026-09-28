# Phase 9-D — Explanation Layer（2026-09-28）

**性质**：只读解释层。8-L Prediction Core、9-A Contract、9-B RiskResult、
9-C DecisionRecord **零修改**。新增 `agents/explanation.py`。

## 1. 职责与边界

```
DecisionRecord（9-C，含 rule_trace / evidence_references / RiskResult）
        ▼  ExplanationAgent.explain()   —— 纯函数、无状态、确定性
ExplanationResult（六类解释 + limitations + evidence_trace）
```

**只做**：把记录中**已有事实**做确定性引用式陈述（f-string 渲染既有值与
固定定义）。**不做**：重算概率/风险/阈值、新增综合评分/质量分/置信度/
胜率阈值/隐藏规则、LLM、网络、随机数、当前时间（源码级断言测试钉住）、
虚构缺失值。

## 2. ExplanationResult Contract

| 段 | 内容（全部逐值引用） |
|---|---|
| `decision_state_explanation` | state、rule_id、conditions（逐条件 fact=value）、完整陈述（含"为什么得到该 state"的规则回放与状态基础语义） |
| `prediction_explanation` | primary 概率/方向/margin/total/family 逐值 + "verbatim 透传，本层不重算"陈述 |
| `evidence_explanation` | 三模型概率、pairwise Δp、directional、Simulation 分布（原样）+ "事实而非误差估计"陈述 |
| `risk_explanation` | flags、tossup band（9-A/9-B 固定定义 (0.45,0.55)）、dispersion_band、margin/total std + "spread 是描述性事实，非误差/不确定度/失败信号"陈述 |
| `calibration_provenance_explanation` | `frozen_defaults`（默认）或 calibrated 文件/拟合窗/val logloss 的逐值陈述 |
| `limitations` | 状态推导限制（9-B known_limitations 透传）+ 契约性条目（ot_probability 未提供且不在本层估算；本层无博彩/ROI/建议；fitted 参数仅限本联赛本训练窗） |
| `evidence_trace` | 9-C `EvidenceReference` 元组**按引用**透传（7 项，测试断言同一对象） |

## 3. 状态语义（诚实解释，绝不升级）

- `prediction_supported`：configured 且无 caution 触发；
- `prediction_with_caution`：陈述**同时**引用 `tossup_indicator=True` 与
  `directional_disagreement=True` 两个事实（9-C 合取规则的回放）；
- `fallback`：陈述包含 "NOT a validated choice"，limitations 同步；
  validation_status 逐字透传（"fallback"），**不升级**；
- `unresolved` / `insufficient_evidence`：各按固定语义陈述。

## 4. 契约值透传

`ot_probability=None` → 陈述"not provided by the frozen core, therefore
not invented here"（测试钉住）；CBA 2024-25=2880s / 2025-26=2400s 经
record 的 provenance facts 逐字透传（两季不同值各自正确）；
`evidence_trace is record.evidence_references`（按引用，零复制）。

## 5. 明确不提供

综合评分、模型质量分、置信度数值、胜率阈值、预测误差概率、OT 概率估算、
投注/ROI/建议、LLM 文本。schema 测试禁止相关字段；源码检查禁止运行时
输入（datetime.now / import random / random. / requests / urllib / openai /
time.time / http）。

## 6. 测试与状态

新增 `tests/unit/test_explanation.py`（19 项）：schema 与禁字段、概率/
模型/风险事实逐值引用（含 statement 内嵌值校验）、规则回放、五种状态
（supported/fallback 不升级/unresolved/insufficient/caution 合取引用）、
ot=None 保留、CBA 双制、trace 按引用、确定性（同 record 逐字段相等）、
不可变输出、无运行时输入、limitations（契约/伦理/校准窗限制）、frozen
defaults 陈述。

storage 零污染；git clean。
