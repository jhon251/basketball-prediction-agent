# Prediction Core Freeze — 冻结决策文档（Phase 8-L，2026-09-28）

**决定**：当前六联赛预测核心（Random / Elo / Strength / Simulation /
primary-family routing / per-league family configuration / calibration /
分布输出 / RiskAgent / Ensemble 诊断层 / save→settle→aggregate 闭环）
**满足冻结条件，予以冻结**。冻结前只读审计未发现任何 correctness bug。

## 1. Freeze Audit 结果（只读，全部实测）

| 审计面 | 结果 |
|---|---|
| 六联赛 league-audit（含泄漏抽样） | **6/6 PASS**（NBA 20,440 / EL 1,867 / ACB 1,989 / NBL 339 / CBA 950 / BSL 527；WARN 均为已记录源怪癖，见 §6） |
| 数据库完整性 | 26,112 场 / 126 队；快照 `2365db5e7145` 与 8-H 末态一致；逐联赛计数核对无污染 |
| CBA 双赛制库内分离 | 2024-25 = 240+25×OT 球队分钟（962 行基线）；2025-26 = 200+25×OT（862 行基线）——两制零混淆 |
| freeze-contract 测试 | 11 项新增全过（frozen defaults 钉值、CBA 双制、family 语义、primary 元数据一致性、分布契约、校准 schema 无 test 字段） |
| 生产语义 | `Primary family: simulation (source: configured)` / `elo (source: override)` CLI 冒烟符合预期 |

## 2. 冻结范围（FROZEN）

- **Primary probability semantics**：主概率由 `resolve_family(league)` 的
  配置 family 提供（当前六联赛 = simulation，`source=configured`）；
  `GamePrediction.primary_family / primary_family_source / primary_prob_home`
  三元组为生产出口的正式语义（测试钉住一致性）。
- **Per-league family semantics**：六联赛显式配置 + provenance（rationale /
  train+validation 窗 / 归档引用）；ensemble 永非配置 family；未知/未配置
  联赛 fallback=simulation 且**必须标记 `fallback/unresolved`**，不得解释为
  "已验证生产模型"（测试钉住）。
- **Calibration contract**：league-scoped（跨联赛拟合结构性禁止）；拟合仅见
  `[train_start, cutoff)`；`FittedParams` schema 无 test 字段（结构性禁止
  未来 test 选择）；fitted 仅经 `--params` 显式生效；全部 candidate /
  rejected / insufficient 历史记录原样保留（含 CBA λ=0.02 失效、BSL 8-F
  负结果、8-H 反转）。
- **Distribution contract**：Simulation 始终产出完整
  `MonteCarloResult`（win probs + tie + score/margin/total 的
  mean/median/std/p5/p25/p50/p75/p95 + 比分桶 + 逐次 runs）——与 primary
  family 无关（elo primary 下同样可用，测试钉住）；RiskAgent 输入链路不变。
- **Ensemble 定位**：experimental/diagnostic；显式 `EnsembleWeights` 才可达；
  无权重时不得称"三模型融合"（默认 (0,0,1) = sim 单成员）。
- **Leakage/provenance contract**：league 硬隔离、as-of 特征纪律、
  `warmup_not_met` 门、reality/temporal-integrity FAIL 即中止、快照链完整。
- **Evaluation protocol**：冻结参数基线 + 双窗校准规则（C2）+ 口径分组
  比较（8-G 纪律）。

## 3. 不冻结范围（OPEN，允许后续演进）

- 各联赛 family 的**未来切换**（需新证据 + 预注册协议 + 显式配置变更，
  不得由未来 test 自动决定）；
- 多赛季扩展（任何联赛补第 3 季）、新联赛接入、新数据源；
- C3 校准层、模型结构改进、k 搜索等研究项（见 §6）；
- Ensemble 权重的任何再拟合（需预注册）。

## 4. 验收记录（模块级）

| 模块 | 验收 |
|---|---|
| Random baseline | 六联赛 walk-forward 基线齐备（8-D/E/F/G/H）；random acc = 真实主胜率参照 |
| Elo | 冻结参数路径稳定（NBA 17/17 季最优记录在案）；无 margin/total 输出（定位如此） |
| Strength | 解析 margin/total 唯一来源；`win_config` 校准挂点正确 |
| Simulation | 分布契约全字段可用（§2）；per-season regulation 正确（CBA 双制） |
| primary_family routing | 三 family 数值/元数据一致（测试）；override 可追溯（`source=override`） |
| family configuration | 六联赛显式 + fallback/unresolved 语义区分（测试） |
| calibration / win_config | league-scoped + 时间顺序（既有单测）+ schema 无 test 字段（新测试） |
| RiskAgent | `confidence_score` / `risk_flags` 输入链路完好（新测试） |
| Ensemble | diagnostic 定位 + 显式权重路径 + 无权重守卫（8-K 测试） |
| save→settle→aggregate | 既有集成测试覆盖（临时库）；真实闭环 8-E/F 各验证一次（CORRECT / WRONG 路径都有） |
| 数据/防泄漏 | 六审计 PASS + provenance.leakage 抽样 0 失败 + 快照一致 |

## 5. 特别保留项（要求 13，全部记录在案）

1. **NBA k=40 上边界**：C2 决议维持——仅记录、停止扩界、开放研究项。
2. **NBL/CBA/BSL 历史深度有限**（各 2 季）：全部结论仅为当前范围观察，
   不得包装成长期规律。
3. **BSL 历史排序翻转**（2024-25 sim / 2025-26 elo）：单联赛内已现翻转，
   是"两季证据不足"的直接证据。
4. **CBA 赛制变化**：2024-25=4×12、2025-26=4×10；per-season regulation
   已实现并测试；混合赛制窗的 total MAE 解读注意事项见 8-H/8-I。
5. **Strength/Simulation 高相关**（r 0.89-0.98、错误 Jaccard 0.73-0.83、
   p_sim−p_str 系统偏移 +0.008..+0.017）：两链路结构差异已文档化（8-J），
   不得把相关解释为相同或因果。
6. **Ensemble 当前无已验证增量**：六联赛等权全负 + EXP-003 权重塌缩；
   保留为诊断层。
7. **family 选择不能由未来 test 自动决定**：`FamilyEvidence` 无 test 字段
   （结构性禁止）。

## 6. 已知开放问题（freeze 后研究项；均非 correctness bug）

| # | 问题 | 状态 |
|---|---|---|
| 1 | NBA k=40 上边界 | C2 决议：记录、不扩界 |
| 2 | EL 10 行 / ACB 2 行 / NBL 5 行分钟聚合怪癖 | 有界容差+计数，原样保留（源怪癖，非缺陷） |
| 3 | NBA/EL 无官方赛程参照（missing.games WARN） | 如实标注；获得参照源后可补审计 |
| 4 | CBA 混合赛制窗的 total MAE 解读 | 已在 8-H/8-I 说明；多季 CBA 分析需按赛季分段 |
| 5 | Simulation 分布级增量未测 | 校准后路径的 score 分布用途属未验证假设 |
| 6 | Ensemble 权重再拟合 | 需预注册协议，当前不做 |
| 7 | family 切换流程 | 需新证据 + 预注册 + 显式配置变更（机制已就绪） |

## 7. 冻结条件判定

**满足**：六审计 PASS、无 correctness bug、646+11 测试通过、生产语义
位级向后兼容、fallback/unresolved 语义正确区分、历史记录零修改、
storage 零污染、git clean。
