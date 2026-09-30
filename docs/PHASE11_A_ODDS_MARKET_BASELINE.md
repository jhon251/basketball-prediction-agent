# Phase 11-A — Odds Data Domain & Market Baseline（2026-09-29）

**性质**：引入赔率数据域，把规格书 §26 预留至今的 market baseline 从
unavailable 变成真实数据管线。**不改动任何模型/特征/预测语义/冻结参数。**

## 1. 赔率数据域

**pydantic schema**：`data/schemas/odds.py`（`GameOdds`，frozen，`extra=forbid`）
- `game_id / league / bookmaker / market(MONEYLINE|SPREAD|TOTAL) /
  kind(OPEN|CLOSE) / line(SPREAD/TOTAL 数值) / price_decimal(欧洲盘 >1.0) /
  captured_at(UTC) / source`

**SQLite DDL**：`game_odds` 表（UNIQUE 约束 game_id+bookmaker+market+kind+
captured_at；FK game_id→games；CHECK price 1.0-100 / market/kind 枚举）

**Repository**：`data/db/odds_repository.py`（guarded insert：game_id FK
存在性、价格边界 1.01-100、SPREAD/TOTAL 必须带 line、MONEYLINE 不带 line、
captured_at 必填、duplicate 幂等跳过、coverage 聚合查询）

## 2. NBA 历史收盘赔率（数据获取按序执行）

### 探针结论（2026-09-29 实测）
| 源 | 可达 | 障碍 |
|---|---|---|
| Kaggle NBA odds 数据集 | ⛔ | 需 Kaggle 认证才能下载 CSV |
| SBR 档案 | ⛔ | 反爬 + JS 渲染 |
| covers.com | ⛔ | 同上 |
| **用户投喂 CSV** | **✅ 管线就绪** | nba_odds_csv.py 适配器已建 |

### DATA_REQUIREMENTS_ODDS.md
```
SOURCE_STATUS: UNAVAILABLE
所需 CSV 列（canonical header）：
  game_date, away_team, home_team,
  close_moneyline_home, close_moneyline_away,
  close_spread, close_spread_odds,
  close_total, close_total_odds,
  bookmaker, season
对齐方式：home_team/away_team 与 games 表 team_id 精确匹配；
         game_date ±1 天容差。
用户将 CSV 放入任意路径后运行：
  basketball odds ingest --file <path> [--column-map <json>]
```

### 适配器管线（先用测试 fixture 开发完，用户放真文件即可入库）
`data/collectors/nba_odds_csv.py`：
- 读 CSV → 映射列名 → 逐行匹配 games 表（home/away team_id 精确 + 日期
  ±1 天容差）→ 产出 validated odds rows → OddsRepository.insert_odds
- Unmatchable 行 → `UnknownTeamError`（不猜不匹配）
- 坏行 → skipped + reason（不中断整批）
- MONEYLINE 额外计算 fair_prob_home（去水后概率）

## 3. 去水与市场概率

模块：`evaluation/devig.py`

| 函数 | 说明 |
|---|---|
| `american_to_decimal(am)` | +150→2.50 / -150→1.667 |
| `decimal_to_implied(dec)` | 1/dec |
| `multiplicative_devig(pa, pb)` | 归一化隐含概率至和=1，返回 margin |
| `moneyline_pair_to_fair(amh, ama)` | 便捷组合 |
| `spread_cover_probability(margin, std)` | 正态近似（仅用于 ROI 回测） |

**Multiplicative de-vig 是保守近似**：按比例缩减每个结果的隐含概率，
使其和=1。真实抽水通常不对称（favourite- longshot bias），multiplicative
高估 favourite——对于 baseline 对比这种偏差可接受且已文档化。

## 4. 盈利评估层

模块：`evaluation/roi.py`

### Market baseline（第四基线）
在既有 walk-forward 窗口上，market closing probability 作为第四列与
frozen elo/strength/simulation 同框报告 log loss / Brier / accuracy。

### Flat-stake ROI 回测
```python
flat_stake_roi(predictions, odds_rows, edge_thresholds=(0.0, 0.02, 0.05, 0.10))
```
- edge = model_p − market_p (de-vigged)
- 每个阈值报告 n_bets / n_wins / total_staked / total_return / ROI / profit
- 资金曲线按赛季窗口分段防单季运气

### 防泄漏硬规则
- 赔率表 `game_odds` 仅存在于 evaluation 层，**features/models 模块源码级
  不引用**（测试钉住）
- ROI 回测只读已 settled 的预测 + 已存 odds，永不重跑预测核心
- 收盘价仅用于事后比较，永不进入 feature

## 5. CLI

```powershell
basketball odds ingest --file odds.csv [--column-map '{"csv_col": "canonical"}']
basketball odds coverage
basketball evaluate --source settled     # 已有
basketball backtest --baseline market    # 融入既有回测（后续扩展）
```

## 6. 诚实结论

**大概率打不过 NBA 收盘价。** NBA 市场是全世界流动性最高、研究最充分的
体育博彩市场之一，closing line 效率极高。我们的冻结模型（logloss 0.55-0.63
区间）不太可能系统性超越 de-vig 后的收盘隐含概率（典型 logloss ~0.60-0.62
且已包含所有公开信息）。

**这本身就是本阶段最有价值的产出**——它给出了一个可量化的基准线：
- 如果模型 ROI 在零阈值下接近 0%，说明模型信息量接近市场
- 如果模型在某些 edge 阈值下有正 ROI，说明找到了市场低估的系统性偏差
- 如果模型 ROI 显著为负，说明模型不如市场——这也是有价值的诚实发现

## 7. 测试

新增 44 项（devig 12 + odds repository 12 + odds CSV pipeline 8 + ROI 5 +
integrity 5 + odds leakage guard 2）。全量 **876 通过 + 1 跳过**。

storage 零污染（odds 表为新增表，不影响既有 games/predictions/decisions）；git clean。
