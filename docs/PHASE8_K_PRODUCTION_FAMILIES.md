# Phase 8-K — 生产预测语义与 Per-League Family 配置（2026-09-28）

**性质**：最小、可回滚的架构语义重构。不新增模型、不调参、不删除任何组件、
不动 frozen defaults 与数据。依据：8-J 架构审计（`docs/PHASE8_J_ARCHITECTURE_AUDIT.md`）。

## 1. 新旧生产语义差异

| 项 | 8-K 之前 | 8-K 之后 |
|---|---|---|
| 主概率出口 | `GamePrediction.win_probability_home` = `ensemble.prob_home`，默认权重 (0,0,1) | 同一数值路径，但**语义显式化**：主概率由 league 的配置 family 提供（当前六联赛全部 = simulation，位级不变）；`primary_family` / `primary_family_source` / `primary_prob_home` 三个新字段记录来源 |
| Ensemble 地位 | 名称上像"三模型融合"，实际默认=sim 单成员 | 明确定位为 **experimental/diagnostic layer**（`EnsembleAgent` docstring 与本文件）；不是配置 family；生产使用组合需**显式传 `EnsembleWeights`**（opt-in），否则 `family=ensemble` 直接报错 |
| family 决策 | 隐式（藏在 ensemble 默认权重里） | **显式配置 + provenance**（`agents/family_config.py`）：rationale / train+validation 窗口 / 归档文档与参数文件引用 / 决定阶段 |

## 2. Per-league family 配置（第一版：全部继承 simulation，不改变任何结论）

`FAMILY_CONFIG`（六联赛，全部 `family=simulation`，`status=configured`）：

| League | family | 记录的证据（不据此切换） | 引用 |
|---|---|---|---|
| NBA | simulation | frozen Elo 17/17 季最优；fitted strength+sim 双窗候选（显式 --params） | C2、8-G/H/I/J |
| EuroLeague | simulation | frozen Elo 4/6 季最优；fitted strength 双窗候选 | 同上 |
| ACB | simulation | frozen Elo 4/6 季最优；fitted strength 双窗候选 | 同上 |
| NBL | simulation | sim 两季 frozen 最优；fitted strength 双窗候选 | 8-H、8-I |
| CBA | simulation | frozen Elo 两季最优；fitted sim 双窗候选；8-E λ=0.02 未被双季复现（已记录） | 8-E/H/I |
| BSL | simulation | frozen 逐季排序翻转（sim→elo）；fitted S+Sim 双窗候选；8-F 负结果存档 | 8-F/H/I |

规则（已实现为代码结构）：

- **第一版不按 test 选 family**：`FamilyEvidence` **没有 test 窗口字段**（结构上
  无处记录未来 test 结果）；`resolve_family` 是纯静态查找（不读比赛数据），
  单元测试断言其确定性。
- **evidence 完整性**：每个配置项必须有 rationale / train 窗 / validation 窗 /
  归档引用 / 决定阶段，缺一即测试失败。
- **未配置联赛安全 fallback**：`resolve_family(其它联赛/未知串/None)` →
  `DEFAULT_FAMILY=simulation`，`source="fallback"`、`status="unresolved"`——
  与 8-K 前行为位级一致。
- **ensemble 永不为配置 family**（测试钉住）；跨联赛不继承（每项独立
  evidence 对象，测试钉住）。
- 覆盖方式：`PredictionAgent(..., family="elo|strength|simulation")` 显式
  override；CLI `predict --family`（ensemble 不在 CLI 选项中，仅程序化
  显式权重 opt-in）。

## 3. 架构文档（要求 11）

| 角色 | 组件 | 说明 |
|---|---|---|
| **Primary probability family** | 逐联赛配置（现= simulation） | `resolve_family(league)`；CLI `--family` 显式 override |
| **Secondary / diagnostic families** | Elo 与 Strength 概率 | 始终计算（`features.elo_win_prob_home` / `params.strength_win_prob_home`）并随 `GamePrediction` 可及，用于对照与诊断 |
| **Distribution provider** | Simulation（MonteCarloResult） | 比分/margin/total 分布、比分桶、OT 解析、RiskAgent 输入——**任何联赛 family 都不动它**（RiskAgent 链路保持独立） |
| **Calibration attachment point** | 共享 model-config 层（`win_config` → `build_game_params`） | 同时作用于 Strength 与 Simulation（8-J 确认的正确挂点）；`--params` 显式 opt-in 不变 |
| **Ensemble experimental status** | experimental/diagnostic | 保留代码与显式权重入口；不重新拟合权重；无预注册协议不得升级 |

## 4. 向后兼容

- 默认构造（不传 family/weights）：primary = `mc.home_win_probability` ==
  旧 `ensemble.prob_home`（权重 (0,0,1)）——**位级一致**（测试钉住）。
- 显式传 `ensemble_weights` 的旧调用方：行为完全保留（primary = 加权组合，
  family 标记 simulation）——测试钉住。
- CLI 不加 `--family` 时输出与 8-K 前一致（新增一行 "Primary family: ..."）；
  settle/aggregate/report 链路零改动（settlement 读 `win_probability_home`，
  数值不变）。

## 5. 测试与状态

新增 `tests/unit/test_family_config.py`（14 项）：六联赛配置解析、未知/
未配置 fallback、evidence schema 无 test 窗字段、provenance 完整性、
ensemble 永非默认、league isolation（条目独立/参数不共享）、三 family
路由产物互异且正确、元数据记录、默认位级向后兼容、显式权重旧路径、
ensemble 无显式权重报错。

storage 零污染（无 DB/schema 改动）；git clean。
