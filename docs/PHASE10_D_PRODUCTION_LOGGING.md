# Phase 10-D — Production Decision Logging & Settlement Accumulation（2026-09-28）

**性质**：真实数据积累闭环。Prediction Core（8-L）与 9-A～9-E、10-A、10-B
**零修改**。新增 `agents/persistence.py`（显式持久化边界）与 CLI
`agent-report --log [--log-settle]` / `agent-log verify|list|settle`。

## 1. Pure orchestration / Persistence boundary

`orchestrate(game_id, as_of)` 保持纯编排——**永不隐式写库**（测试：编排后
log 目录不存在）。落账是显式的独立步骤：

```
bundle = orchestrator.orchestrate(game_id, as_of)
risk = RiskAnalysisAgent().analyze(bundle.agent_decision)
record = DecisionRecordAgent().build(bundle.agent_decision, risk)
persisted = persist(bundle_decision, risk, record, DecisionLog(dir))
# PersistResult(record_id, status∈{appended,duplicate}, bundle 原样引用)
```

`persist` 不改变 bundle 内容；`record_id` 为内容哈希（10-B 语义），同
bundle 重复 persist → `duplicate` 且零写入。

## 2. Logging modes / CLI

| 命令 | 写入？ | 说明 |
|---|---|---|
| `agent-report <g> --as-of …` | 否（默认只读，输出明确说明） | 纯编排报告 |
| `agent-report <g> --as-of … --log <dir> [--log-settle]` | **是（显式 opt-in）** | 编排 + append 决策 +（可选）结算事件 |
| `agent-log verify --log-dir <dir>` | 否 | 哈希链校验 |
| `agent-log list [--league]` | 否 | 记录列表 |
| `agent-log settle <game_id> --log-dir <dir>` | 是（结算事件） | 经既有 SettlementStore 结算并 append 事件 |

测试/回测路径不调用 persist → 默认零写入。

## 3. Append-only / 幂等 / 异常语义

- 决策 append：内容哈希 record_id；同 bundle → `duplicate` 零写入；
  同 game 不同 as_of → 新 record 共存（sequence 定序）。
- settlement（`settle_and_log`）：经**既有** SettlementStore 结算（链路
  不变），按 record_id append 事件；**重复到达** → `duplicate` 零写入；
  game 无 stored 决策 → 不 append；malformed settlement → 跳过并计数
  （10-C join 语义）；game 非 FINAL / 无未结算预测 → 冻结 store 异常
  原样传播（无部分事实写入）。
- `ot_probability=None`、`frozen_defaults` 等 None 契约原样持久化。

## 4. 端到端 smoke（真实比赛，2026-09-28）

`CBA25-0452`（2025-26 总决赛 G2，浙江方兴渡 101-93 上海久事）：
1. `predict CBA25-0452 --as-of 2026-06-02T10:00Z --save`（id=8）；
2. `agent-report CBA25-0452 --as-of … --log storage/agent_log --log-settle`
   → record_id=`f561a2c1df1f0d82` **appended**；重跑同命令 → 同 id
   **duplicate**（幂等证明）；
3. `agent-log verify` → **VALID (2 records)**（含 G5 决策记录）；
4. `evaluate --source decision-log` → `evidence_kind: real_agent_history`，
   join=1（另一记录无 settlement 如实计入 skipped），四模型指标来自已存
   概率。
   全部切片 `[SMALL SAMPLE — descriptive only]` 守卫生效。

## 5. 审计方法

- 完整性：`agent-log verify`（重走哈希链）；
- 追溯：record → decision_state/rule_trace → evidence_references →
  冻结层；regulation_seconds 逐记录（CBA 双制）；
- 隐私/边界：prediction payload 无任何 actual/result 字段（泄漏测试）。

## 6. 测试与状态

新增 `tests/unit/test_persistence.py`（8 项）：persist 写入且 bundle 原样、
幂等同 id、同 game 双 as_of 双版本、duplicate 零写入、**orchestrate 纯度**
（不写 log）、闭环（orchestrate→persist→settle→10-C join）、settlement
幂等、非 FINAL 传播且无部分事实。

storage 零污染（新增 gitignored `storage/agent_log/` 真实历史 2 条）；git clean。
