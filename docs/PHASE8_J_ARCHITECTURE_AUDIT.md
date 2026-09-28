# Phase 8-J — 模型架构决策与最终预测核心审计（2026-09-28）

**性质**：架构定位审计。不新增模型、不调参、不删除任何组件、不修改
frozen defaults 与历史结论。证据来源：六联赛预测路径的**源码级信息链追踪**、
Phase 8-I 的逐场概率存档（25,286 场，`.tmp/8i_*.json`）与既有实验结果
（B2/B4/B7/C1/C2/8-D/E/F/H/I）。本报告新增的唯一定量工作是对 8-I 存档的
只读再统计（sim-strength 差异分布）。

---

## 1. 四个预测节点的架构审计（源码级信息链）

### 1.1 Elo（`EloPredictor`）

- **信息来源**：仅历史比赛**结果序列**（每联赛独立的 Elo 链，K/HCA/init 由
  elo config 定；不读任何 box score）。
- **输入依赖**：`features.elo_win_prob_home`（单值）；`league=` 硬隔离的
  对局历史。
- **输出依赖**：无（不产出 margin/total）。
- **实际增量**（8-I）：深历史上六联赛最强单点概率源（NBA 17/17 季最优、
  NBA disagreement 子集 0.6538/0.625 大幅领先）；但在浅历史（单季起点、
  NBL 全部、EL/ACB 最早一季）被 score-based 反超；无 margin/total 输出，
  不能独立支撑 score 类预测。

### 1.2 Strength（`StrengthPredictor` → `WinProbabilityModel`）

- **信息来源**：聚合成效率特征（双方经 shrinkage k 收缩的 ORtg/DRtg = 净
  评级差）× PaceModel 期望回合数 + HCA，经 logistic(scale) 映射为胜率。
- **输入依赖**：`TeamFeatureSet` 聚合特征、league 基线、`WinProbabilityConfig`
  （HCA/scale/k）——**校准参数直接作用于该节点**。
- **输出依赖**：同时产出 expected_margin/expected_total（score 层唯一解析源）。
- **实际增量**：校准后（C2）在 NBA 双测试窗大幅超越 frozen（0.6731→0.6111）；
  EL/ACB fitted_strength 双窗成立；六联赛 margin/total MAE 的唯一定义点。

### 1.3 Simulation（`SimulationPredictor` → `build_game_params` → `GameSimulator`）

- **信息来源**：**逐项回合级比率向量**（tov_rate、def_ratio、p2%/p3%/ft%、
  p3a_share、orb%——每项独立做 matchup 调整 + shrinkage）→ 回合级随机
  演化 N 次 → 胜率/比分分布。
- **输入依赖**：与 Strength 共享 PaceModel、league 基线、shrinkage 机制、
  HCA（以 ppp 奖励注入）与 `win_config`（校准可作用）；**但评分路径不同**：
  Strength 用净评级差→logistic；Simulation 用比率组合逐回合随机解析。
- **输出依赖**：唯一产出完整分布（比分桶、margin/total 分布、OT 解析）的
  节点；生产链路（`PredictionAgent`）的主概率来自 ensemble，而 ensemble
  **默认权重 = (0, 0, 1)——当前生产主预测就是 Simulation**。
- **实际增量**（见 §2）：冻结参数下相对 Strength 无 logloss 增量
  （8-I 六联赛一致）；校准后（C2）NBA fitted_sim 成为双窗候选。

### 1.4 Ensemble（`EnsembleAgent`）

- **信息来源**：三个概率的凸组合；权重可训练（EXP 机制）或显式给定。
- **输入依赖**：三节点概率 + `EnsembleWeights`；**不引入任何新信息**。
- **生产角色**：`PredictionAgent` 的主概率出口（默认权重 sim-only）。
- **实际增量**：等权组合在六联赛全部无增量（8-I Δlogloss -0.0007..-0.0083，
  全负）；EXP-NBA-003 在 NBA 训练窗拟合出 (0, 0, 1)（Elo=0, strength=0）。

## 2. Strength → Simulation 信息重合分析（要求 4）

**源码级重合**（同一次 `build_game_params` 内共享）：
PaceModel 期望回合数、league 基线、shrinkage k 机制、HCA 注入方式、
opponent-adjustment 框架、`win_config` 校准挂点——两条链的全部"原料"层。

**实际分歧点**（两者并非同一模型）：
1. 胜率映射：logistic(nrtg-margin / scale) vs 比率组合的回合级随机解析
   （p2/p3/ft/orb/tov 路径的方差与整数得分、OT 解析隐含其自身分布）。
2. 8-I 逐场量化（只读再统计）：`p_sim − p_strength` 均值 **+0.008..+0.017**
   （六联赛全部为正），标准差 ~0.05，方向翻转率 5-12%；且 NBA 的偏移随
   |p_strength−0.5| 增大（0.05 桶内 +0.004 → 0.15-0.20 桶 +0.033）——
   **Simulation 的隐含 margin 分布比 frozen logistic(scale=11.5) 更陡**，
   即两者对同一特征的"分布假设"不同，差异是结构性的，不是纯 MC 噪声。
3. 错误重合 85-93%（S-Sim 对）+ Jaccard 0.73-0.83——高但**不等于 1**：
   8-I 中两节点在六联赛各有独有错误（NBA strength 672 / sim 923 场独有错）。

**理论上可能但当前未测出的独立信息**（未验证假设，仅列举）：
回合级时序效应（追分/垃圾时间对真实胜率的影响）、OT 内生解析（当前 V1
不模拟犯规/暂停，§14 已记录）、整数得分的 tie→OT 处理、以及比分分布
（桶/bucket 输出）对 score-line 型下游用途的价值。8-I 只测了 logloss 维度；
**"冻结参数下无 logloss 增量"不能外推为"这些路径无价值"**——尤其 C2 已证
明校准后 fitted_sim 在 NBA 成为双窗候选。

## 3. Ensemble 增量分析（要求 5）

- **等权为何无增量**：三个概率高度相关（S-Sim r 0.89-0.98；E-S 0.58-0.97），
  凸组合只是在这条低维流形上移动；当其中一个成员（多数联赛的 Elo）单独
  更强时，混入两个相关且更弱的成员必然拉高 logloss（EL：ens 0.6223 vs
  elo 0.6183）。等权是"无信息"的混合，其负增量是成员相关性的直接结果。
- **EXP-NBA-003 的 (0,0,1) 解释**：该实验在 NBA 训练窗（含 2024-25 前历史）
  拟合权重，Elo 与 strength 的训练窗 logloss 均差于 sim（训练窗结论与测试
  窗相反——B2 已记录 frozen 口径下训练/测试的差异），故权重全部塌缩到
  sim；这证明**权重拟合对训练窗过敏感**，作为预测层不可靠。
- **定位判断**：Ensemble 当前应定位为**实验/诊断层**——它是有价值的
  "成员相关性"度量仪表（r、disagreement、Δlogloss），并保留生产链路的
  显式权重入口（`--params` 同级的显式 opt-in 语义），但**不应作为默认
  预测层**（现默认权重 (0,0,1) 实际等价于"sim 主预测"，名不副实）。
  建议（不实施）：生产文档与输出中明确"默认权重=sim 单成员"，或把默认
  权重改为显式标注的诊断组合。不删除代码。

## 4. Elo 稳定性审计（要求 6）

- NBA：17/17 季冻结最优（0.5883..0.6834），四时期聚合稳定领先
  （Δ vs strength 0.028-0.037）；disagreement 子集（n=6,525）0.6538/0.625
  大幅优于两 score-based——**Elo 独有信息在分歧样本上最值钱**。
- 跨联赛：多季全窗上 EL/ACB/CBA/BSL 的 Elo 也最优（B4/B7/8-H），但
  NBL 两季 sim 均最优、EL/ACB 历史最浅的一季 score-based 反超、BSL 两季
  翻转——**"Elo 最优"的条件是足够深的联赛内历史**，不得外推为全称命题
  （要求 6 明示）。Elo 无 margin/total 输出，也不适合 score 类用途。

## 5. Calibration 挂载位置分析（要求 7）

源码级事实：`win_config`（HCA/scale/k）经 `build_game_params` 同时注入
Strength 的 logistic 与 Simulation 的 ppp/回合路径——**校准天然挂在两个
score-based 节点共享的模型参数层**，这正是 C2 fitted strength/sim 同窗同参
的原因；λ（decay）挂在特征层，同时影响 Elo 输入（衰减加权）与 box 聚合。
因此：
- 统一部分：**校准应继续挂在共享 model-config 层**（现位置正确），每联赛
  一套参数、显式 opt-in（现纪律正确）。
- league-specific 部分：候选采纳（哪族预测器用 fitted）已经由双窗规则
  league-specific（NBA S+Sim、EL/ACB S、8-H 三联赛各异）。
- Ensemble 权重若要启用应视为**第三层校准**（成员组合层），但现有证据
  （等权无增量 + EXP-003 训练窗过敏感）不支持启用。

## 6. 跨联赛架构分析（要求 7）

**可以统一的**（已有跨联赛证据）：frozen defaults 作为共同基线；league
隔离与 as-of 纪律；审计/泄漏框架；校准协议（双窗规则）；per-season
regulation 等环境参数表。
**必须 league-specific**（数据支持）：校准参数（六联赛六套互不相同）、
候选预测器族（NBA: fitted S+Sim；EL/ACB: fitted S、Elo 仍为冻结最优；
NBL: sim；CBA/BSL: Elo）、未来第 3+ 赛季后的复核结论。
**架构含义**：不同联赛采用不同 prediction-family 是**数据已经支持的现状**
（各联赛 candidate 不同），但切换机制必须保持显式（`--params` /
per-league 配置），不得引入隐式分支（可解释性与审计要求）。

## 7. 候选架构（比较，不排名；不实施破坏性修改）

| 维度 | A：Elo + Strength 双核心 | B：Elo + Strength + Simulation（=现状） | C：Elo 主核心 + score 辅助/诊断 | D：配置驱动的 per-league family 选择 |
|---|---|---|---|---|
| 信息独立性 | 保留最不相关的两源（r 最低对之一）+ sim 转诊断 | 三源，但 S-Sim 高度重合 | 主源最稳（深历史），score 层降诊断 | 按联赛证据选择成员组合 |
| 已有实验结果 | 与 C2/8-I 一致（fitted strength 是多联赛候选；sim 无冻结增量） | 现状；sim 校准后在 NBA 成候选 | 与"Elo 深历史最优"一致；丢弃 sim 的 NBA 校准增量 | 与全部既有结果一致（逐联赛选择已有数据） |
| 计算成本 | 最低（去 MC 主链路，sim 仅按需诊断） | 最高（MC 是 window-backtest 主要成本） | 最低 | 随联赛配置浮动 |
| 可解释性 | 高（两链路语义清晰） | 高但 sim 主路径在部分联赛证据弱 | 最高 | 高（每联赛配置显式可审计） |
| 校准兼容性 | 现挂点不变 | 现挂点不变 | 现挂点不变 | 现挂点不变 + family 配置层 |
| 后续扩展性 | sim 保留可随时回升（C2 模式） | 天然 | 需重建"主源切换"机制 | 天然（新联赛=新配置行） |
| 主要风险 | NBA fitted_sim 增量被闲置（除非诊断入口调用） | 维持现状成本；默认权重语义含混 | 浅历史联赛（NBL）Elo 主源证据不足 | 配置错误可能掩盖证据变化（需复核节奏） |

（D 方案由现有代码结构自然推导：`--params`/per-league 校准已实现 90%，
缺的只是一个显式的、经审计的 per-league family 配置与复核纪律。）

## 8. 已验证事实 / 工程推论 / 未验证假设

**已被实验支持的事实**：
- S-Sim 高相关（r 0.89-0.98）、错误重合 85-93%、p_sim−p_strength 系统偏移
  +0.008..+0.017 且随边际增大（8-I 存档再统计）。
- 冻结参数下等权 ensemble 六联赛无增量；EXP-003 权重塌缩。
- NBA 17/17 季 Elo 冻结最优 + disagreement 子集大幅领先；EL/ACB 多数季
  Elo 最优但随历史深度变化；NBL sim、CBA Elo、BSL 翻转。
- 校准双窗候选逐联赛不同；CBA λ=0.02 候选未被双季复现。
- 生产主概率出口 = EnsembleAgent 默认权重 (0,0,1)（源码事实）。

**合理工程推论**：
- Ensemble 定位为实验/诊断层；默认权重语义应在文档/输出中显式化。
- 校准继续挂共享 model-config 层（现位置正确）。
- per-league family 选择的显式配置是 D 方案的最小实现路径。
- MC 是主要计算成本，诊断化可释放窗评估成本。

**尚未验证的假设**：
- 回合级时序/OT/犯规等路径在**校准后**是否带来 logloss 之外的增量
  （score 分布用途未测）。
- D 方案的配置复核节奏能否防住"证据漂移"（如 BSL 翻转所示）。
- 拟合权重在更长训练窗下是否仍塌缩（单窗证据）。

## 9. 状态

零代码行为变更（本轮仅新增本报告）；不删除 Simulation/Ensemble；frozen
defaults 与全部参数文件逐字节未动；storage 零污染；git clean。
全量测试 **632 通过 + 1 既有环境分支跳过**。
