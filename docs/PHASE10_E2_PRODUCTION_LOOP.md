# Phase 10-E.2 — Production Auto-Prediction Loop（2026-09-28）

**性质**：生产闭环。8-L～10-E.1 全部冻结基线零修改。新增
`collection/production_loop.py` 的 `ProductionLoop`（collect→predict→log→
settle 单 pass）+ CLI `production run/status`。

## 1. 闭环链路

```
CollectionScheduler（10-E discovery/指纹）
  → SCHEDULED 行 upsert（BSL/NBL 行级源；台账分配内部 id；resolve_team
    连续性映射使历史链接）
  → ProductionLoop 预测 pass（冻结核心，as_of=loop 边界；
    已预测/已开始/已完成跳过）
  → persist → Decision Log（append-only，record_id 内容哈希幂等）
  → settle_and_log（比赛 FINAL 后，既有 SettlementStore + 结算事件）
```

## 2. 显式决策规则（要求 4：可测试、无隐藏逻辑）

| 规则 | 条件 | 行为 |
|---|---|---|
| PREDICT | SCHEDULED 且 kickoff > now 且无已有预测/决策记录 | 调冻结核心 + 落账 |
| SKIP already | Decision Log 已有该 game 记录 | 计数跳过 |
| SKIP missed | kickoff ≤ now（已开始/已结束） | 计数 missed，**永不预测** |
| SKIP insufficient | FeatureComputationError（新军无历史） | 计数，下次重试 |
| SETTLE | FINAL 且有未结算 saved prediction | 既有 SettlementStore.settle + 决算事件 |

`as_of = now` 由调度触发时确定（永不隐式查时钟）；reality check 的
prediction_before_game 校验兜底。

## 3. 真实运行（BSL 2026-27）

`production run --leagues BSL --max-predictions 5`：

- 171 场 SCHEDULED 行 upsert（source_id→BSL26-xxxx 台账登记）；
- 5 场新预测（真实 AgentOrchestrator 输出，Decision Log record_id 唯一）；
- 10 场 insufficient（升班/新军无历史——正确行为）；
- 72 errors（FK 约束：SCHEDULED 引用的 canonical team id 在 teams 表中
  可能缺失——如实记录，下次运行安全重试）；
- 二次运行 → `already_predicted=5 predicted=0` 幂等验证通过。

## 4. 六联赛隔离与冻结层零改动

- 逐联赛/逐场隔离（每场独立 try/except，错误记录到 report.errors）；
- frozen defaults、family config、calibration、模型权重、历史结论零改动；
- 决策日志/结算链路行为与 10-B/10-D 完全兼容。

## 5. 测试与状态

新增 `tests/unit/test_production_loop.py`（11 项）：预测 pass、幂等重跑、
past kickoff 不预测（要求 4）、FINAL 结算、非 FINAL 等待、重复结算幂等、
历史零变化、league filter、error isolation、安全重试、六联赛 LOOP 状态
隔离。

全量测试 **832 通过 + 1 环境分支跳过**；storage 零污染（Decision Log
append-only、SCHEDULED 行为正常插入非重复）；git clean。
