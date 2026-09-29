# Phase 11-B — Production Coverage Fix（2026-09-29）

## 修复前后对比

| 指标 | 修复前 | 修复后 |
|---|---|---|
| EuroLeague 赛季 | 2019-20 .. 2024-25（6 季 / 1,867 场） | 2019-20 .. **2025-26**（7 季 / **2,265 场**） |
| BSL production FK/UNIQUE errors | **57**（三支新军未建卡） | **0** |
| SCHEDULED upsert 覆盖 | BSL / NBL（2 联赛） | BSL / NBL / **CBA**（3 联赛） |
| EL 新队 | — | Dubai Basketball (DUB) / Hapoel Tel Aviv (HTA) |

## 快照链

| 快照 | 说明 |
|---|---|
| `2365db5e7145` | Phase 10-E.2 末态（26,112 场） |
| `0dfa8dfb4147` | +EL 2025-26（26,863 场 = +744 净增含 PLAYOFFS/PLAY_IN） |

## 真实 production run 记录

```
production run --leagues BSL --max-predictions 3
  BSL: 4 SCHEDULED upserted, 3 predicted, 17 already_predicted,
       17 insufficient (new teams no history), 0 FK errors
```

## 已知限制

- CBA 2026-27 十月开赛，当前 discovery 返回 0 场；scheduled upsert 代码已就位，开赛后自动生效
- NBA 为 parquet 文件源（无逐场赛程端点），标为 unsupported（如实）
- BSL 2026-27 三支新军（Bandırma/Çayirova/Denizli）无跨季映射（升班/新俱乐部），已建 canonical team 行
- EL DUB/HTA 为 2025-26 新加入球队，已加入 TEAM_TIMEZONES + ALIAS_TO_TEAM
