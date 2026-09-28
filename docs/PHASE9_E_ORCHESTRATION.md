# Phase 9-E — Final Agent Orchestration（2026-09-28）

**性质**：编排收口。8-L Prediction Core 与 9-A/9-B/9-C/9-D 全部冻结基线
**零修改**；新增 `agents/orchestration.py`（组装层）与只读 CLI
`agent-report`。

## 1. 编排契约

```
AgentOrchestrator(prediction_agent, calibration=None)
  .orchestrate(game_id, as_of)   ← as_of 必须由调用方显式提供（永不查时钟）
      1. DecisionAgent.decide()            (9-A：调冻结核心一次)
      2. RiskAnalysisAgent.analyze()       (9-B)
      3. DecisionRecordAgent.build()       (9-C)
      4. ExplanationAgent.explain()        (9-D)
    = AgentBundle（frozen dataclass）
```

**AgentBundle**：`agent_decision(9-A) / risk_result(9-B) /
decision_record(9-C) / explanation(9-D)` 四字段（schema 测试钉住）；便捷
属性 `context / evidence_trace(按引用) / validation_status / decision_state
/ primary_prob_home`。层间对象**按引用链接**（测试断言
`explanation.evidence_trace is record.evidence_references`、
`record.risk_summary is risk`——无重建无复制）。

## 2. 边界

- 编排层**只调用与组装**：不重算概率/风险/decision state/explanation；
- 模块源码级守卫：无 `build_game_params/MonteCarloRunner/FeaturePipeline/
  fit_v1_params/datetime.now/random/urllib/requests/openai`（测试钉住）；
- `as_of=None` → TypeError（"caller owns the information boundary"）；
- 异常传播：核心 `PredictionError`（如 UNKNOWN_GAME 的 reality check 失败）
  原样上抛，编排不吞；
- 六联赛隔离：状态逐联赛独立解析；CBA 2024-25=2880s / 2025-26=2400s 从
  context 原样传递（测试钉住两值不合并）；
- fallback/unresolved/insufficient_evidence 语义逐层保持，永不升级。

## 3. CLI adapter（只读）

`basketball agent-report <game_id> --as-of ... [--family] [--params]`：
打印完整 bundle 报告（状态/决策规则回放/risk facts/provenance/7 项
evidence trace/limitations），**不落库不保存**；`--params` 时构造
`CalibrationProvenance` 显式传递。真实冒烟（CBA25-0453）输出核对：
configured/simulation/prediction_supported/calibration_frozen_defaults/
dispersion_gt_0.15/快照 2365db5e7145 全部符合预期。

## 4. 测试与状态

新增 `tests/unit/test_orchestration.py`（18 项）：四层齐全与按引用链接、
primary 概率与裸 core 输出逐值一致、bundle 不可变、确定性（同输入逐字段
相等）、as_of 必填、fallback/unresolved/insufficient/caution 路由、六联赛
独立、CBA 双制、calibration provenance 贯通、evidence trace 完整、异常
传播、编排层零重算（结构守卫）、CLI 只读 adapter（monkeypatch DB +
真实 settings）、格式化渲染。

storage 零污染；git clean。
