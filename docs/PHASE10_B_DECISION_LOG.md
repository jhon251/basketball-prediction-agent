# Phase 10-B — Append-only Decision Log（2026-09-28）

**性质**：不可变审计日志。Prediction Core 与 9-A～9-E、10-A **零修改**。
新增 `agents/decision_log.py`，存储于 `storage/agent_log/`（gitignored）。

## 1. Schema（decision-log-v1）

每行一个 JSON 对象（canonical 序列化：sort_keys）：
`schema, record_id, sequence, prev_hash, entry_hash, game_id, league,
season, home/away_team_id, as_of, primary_family, validation_status,
decision_state, rule_trace{rule_id, conditions}, prediction{prob_home/
away, winner, margin, total}, model_probabilities{elo(可 null),
strength, sim}, simulation_distribution{…, ot_probability=null},
risk{flags, tossup, pairwise, dispersion, std, core flags}, calibration
(null=冻结默认) / calibration_source, snapshot_id, model_version,
regulation_seconds, known_limitations`。

## 2. Append-only 与 Immutability

- 存储类**没有任何 update/delete/rewrite 方法**（结构测试钉住）；
- 每条记录携带 `prev_hash` + `entry_hash`（sha256(prev + canonical(payload
  minus hashes))）构成哈希链——`verify_chain()` 重走全链，磁盘篡改即失
  败（篡改测试：静默改动 prob 后 verify=False）；
- append 唯一写路径；sequence 单调（1,2,…）。

## 3. Duplicate / Version 语义

- `record_id = sha256(canonical payload)[:16]`：**相同输入 → 相同 id**；
  重复 append 同一 bundle → `("appended","duplicate")`，不写任何行（幂
  等，测试钉住）；
- 同一 game_id 不同 as_of（或任何事实不同）→ 不同 record_id，两条都保
  留，由 sequence 定序——版本语义 = record_id 唯一标识 + sequence 定序。

## 4. Prediction / Settlement 边界（防泄漏）

- 预测 payload **只含 as-of 边界内的事实**（测试：不含
  actual_*/correct/settled 任何字段）；
- settlement 是**独立事件日志**（settlements.jsonl），以 record_id 关联，
  附带 `actual scores / correct / brier / log_loss`；append_settlement 要
  求已知 record_id，重复事件幂等；**原始预测行永不被触碰**（测试：settle
  后 prediction 记录字段不变）。

## 5. 查询 API（10-A 直接消费）

`records() / settlements() / get_record(rid) / settlements_for(rid) /
filter_records(league, season, window_start, window_end)`（window_end 为
排他历史 cutoff，无当前时间）+ `verify_chain()`。

## 6. 隔离与 CBA 双制

逐记录携带 league 与 regulation_seconds；league 过滤互不串扰；CBA
2024-25=2880s 与 2025-26=2400s 两条记录共存、各自值正确（测试）。

## 7. 测试与状态

新增 `tests/unit/test_decision_log.py`（13 项）：roundtrip、无 update/
delete API、哈希链防篡改、sequence 单调、幂等 duplicate、同 game 多
as_of 版本共存、settlement 隔离与幂等、未知 record_id 拒绝、league
过滤、CBA 双制、确定性序列化、无未来结果泄漏。

storage 零污染（agent_log 为新增 gitignored 目录）；git clean。
