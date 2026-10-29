# 篮球赛前预测 Agent

[English](README.md) | [简体中文](README.zh-CN.md)

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-1097%20passing-brightgreen)

统计建模 + 回合级蒙特卡洛沙盘推演的篮球赛前预测系统，由一组 Agent 负责
采集、验证、预测与自我审计。**所有概率都来自历史数据、显式公式或模拟——
绝不来自 LLM。**

对一场比赛输出：

| 输出 | 口径（双供给方） |
|---|---|
| 胜负概率（主/客） | Elo · 球队实力模型 · 回合级蒙特卡洛 |
| 让分赢盘（整数盘含走水） | MC 分差样本 · Elo 胜率映射分差的连续性修正正态 |
| 大小球（如 190 / 190.5） | MC 逐场总分 · 标注过的正态平滑 |
| 分差区间矩阵（客胜 26+…1-5、平、主胜 1-5…26+） | 样本计数 · 修正正态 |

**联赛**：NBA · 欧篮联（EuroLeague）· 西甲 ACB · 澳洲 NBL · CBA · 土超 BSL。
联赛间历史严格隔离（不共享评级与基线），支持未来赛程采集与
NBA / EuroLeague 伤停层。

## 快速开始

以下路径**无需任何 API key**（EuroLeague 官方端点免鉴权）。

```bash
pip install -e .

# 1) 抓取原始赛季包（用户侧，每季约 2 分钟）
python tools/fetch_euroleague_bundle.py --seasons E2024 E2025 --out-dir storage/raw/euroleague_live

# 2) 校验（全有或全无）+ 入库
basketball validate storage/raw/euroleague_live/euroleague_E2024_raw.json
basketball ingest   storage/raw/euroleague_live/euroleague_E2024_raw.json
basketball ingest   storage/raw/euroleague_live/euroleague_E2025_raw.json

# 3) 当前赛季赛程（枚举官方 Header 端点，已完赛自动落库）
basketball euroleague-fixtures --season E2026

# 4) 本地服务 + 中文网页界面
basketball serve --open            # http://127.0.0.1:8765
```

网页里：选联赛和两支球队（中英文队名都认），可选填让分（让 5.5 填 `-5.5`）
和/或大小球盘口（`190.5`），点**预测**。点赛程列表里的场次可自动填入对阵。

### 命令行预测

```bash
basketball predict <game_id> --as-of 2024-11-19T12:00:00+00:00 \
    --n-simulations 10000 --seed 42 --save
basketball settle <game_id>        # 比赛终场后结算
```

相同输入 + 相同数据快照 + 相同种子 ⇒ 输出逐位一致（可复现）。

### API

```bash
curl -X POST http://127.0.0.1:8765/api/predict \
  -H "Content-Type: application/json" \
  -d '{"game_id": "NBA26-0021", "home_line": -3.5, "total_line": 224.5}'
```

响应含 `win_probability`（三家族并列）、`spread`（双口径赢盘/走水）、
`totals`（大球/走水/小球）、`margin_ranges`（13 桶分差矩阵）、`risk`
（置信度 + 风险旗标）与 `explanation`。口径间有分歧时**两边都给**，网页
并排展示，分歧显著时触发 `model_disagreement` 风险旗标。

## 工作原理

```text
真实历史 → 校验 → 特征（as-of 安全）→ 模型（Elo / PPP / 节奏 / 投篮结构）
→ 回合级模拟 → 蒙特卡洛 → 分布
→ 胜负 / 让分 / 大小球 / 分差区间概率
→ walk-forward 回测 → 校准 → 诚实置信度
```

* **特征管线**：Elo、攻防评分、节奏、投篮、篮板、失误；指数时间衰减、
  休息天数、伤停可用性。特征只允许使用预测时点已公开的信息——混入未来
  信息是硬 **FAIL**（泄漏测试），绝不降级为警告。
* **模型 → 模拟器**：球队参数化为每回合分布（失误 / 上罚球线 / 两分或
  三分出手、前场板延续）；比赛时钟约束回合数，加时自然涌现。1 万次模拟
  每场约 1.7 秒。
* **评估**：只用 walk-forward 回测（无随机切分），logloss/Brier/ECE 对照
  常数概率基线；即使朴素 Elo 打败整个模拟器，结果也照实上报。
* **存储**：SQLite + 仓储接口隔离；快照是带 SHA-256 manifest 的逐字节
  数据库副本；每次预测引用其快照 id。

### 实测结论（按实测口径呈现，不是宣传口径）

* 17 个 NBA 赛季（20,440 场）：**朴素 Elo 在每个测试窗口都优于冻结参数
  的模拟器**——系统自己把这个结论写在报告里
  （[docs/MULTI_SEASON_RESULTS.md](docs/MULTI_SEASON_RESULTS.md)）。
* 各联赛让分默认口径由 walk-forward 盘口探针 logloss 决定，不靠品味：
  NBA / EuroLeague / ACB / CBA / BSL 用 `elo_normal`，NBL 是统计平手、
  保留模拟口径（[docs/PHASE12_A_SPREAD_API.md](docs/PHASE12_A_SPREAD_API.md) §12-H）。
* 模拟器曾有一个 bug（采样器每回合实际得分比记录目标低约 3.8%），由
  "大小球为何总偏小球"这个问题顺藤摸瓜找到，修复后重跑校准：30 场
  walk-forward 的 MC 总分偏差 −12.5 → −2.2（§12-L）。合同测试现把闭式
  解与采样器钉在蒙特卡洛噪声以内。

## 数据

**仓库不含任何数据。** 采集器全部对接公开端点，每个源的鉴权方式与已知
缺陷都记录在 [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)：

| 联赛 | 来源 | 鉴权 |
|---|---|---|
| NBA（历史） | stats.nba.com gamelog / llimllib parquet 镜像 | 无 |
| NBA（赛程、伤停） | site.api.espn.com | 无 |
| EuroLeague | live.euroleague.net API | 无 |
| ACB | api2.acb.com（站点自身公开客户端键） | 公开键 |
| NBL / BSL / CBA | 各联赛 live 端点（CBA 为 AES-JSON） | 无 |

请自行遵守各数据源的服务条款；本仓库只发布代码。

## 开发说明

* **测试**：`pip install -e ".[dev]"` 后 `pytest -q`——1,097 个测试，
  全程无需网络（采集器在接缝处打桩；实网冒烟由人工执行并记录在 docs/）。
* **docs/** 是真实的开发档案：分阶段设计笔记、决策记录、校准实验与
  如实的失败报告（如 Phase 8-B2 的"5 赛季窗口上模拟器 ≈ 随机"）。读
  [PHASE12_A_SPREAD_API.md](docs/PHASE12_A_SPREAD_API.md) 了解预测 API 层，
  采集前先读 [DATA_SOURCES.md](docs/DATA_SOURCES.md)。

## 项目结构

```text
basketball_agent/
├── data/            pydantic 模型、校验器、SQLite DDL + 仓储、采集器
├── features/        as-of 安全特征管线
├── models/          Elo · 攻防 PPP · 节奏 · 回合结果分布模型
├── simulation/      回合级引擎 + 蒙特卡洛执行器
├── agents/          预测、验证、风险、集成、决策、解释
├── evaluation/      walk-forward 回测、校准、联赛审计、DNP 对账
├── collection/      生产循环、赛程生命周期、伤停入库
├── api/             标准库 HTTP JSON API + 中文网页界面
├── cli/             `basketball` 入口（predict/settle/serve/ingest/evaluate/…）
└── tests/           单元 · 集成 · 回归 · 泄漏 · 模拟
tools/               用户侧赛季抓取器（EuroLeague、ACB、NBL、CBA、BSL）
```

## 许可

[MIT](LICENSE)——研究项目，不提供担保。本项目产出的概率仅供研究；若用于
投注，风险自担，并仍须遵守所在司法辖区与数据源的条款。
