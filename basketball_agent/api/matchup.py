"""Matchup resolution for the API layer (Phase 12-C).

Turns user input ("湖人", "Lakers", "LAL", "mil") into canonical team ids
and finds (or refreshes) the SCHEDULED game row a prediction needs.

Resolution order per league:
1. exact team_id match (case-insensitive) — ``NBL-SEM``/``ATL`` style;
2. exact abbreviation match;
3. NBA franchise aliases (``nba_teams.ALIAS_TO_ID`` + ESPN quirks);
4. Chinese alias table (NBA 30 teams);
5. unique case-insensitive substring match on the team's stored name.

Anything ambiguous or unmatched fails loudly with the candidate list —
the caller (HTTP layer) surfaces it as a 4xx, never a silent guess.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from basketball_agent.data.db.repository import Repository
from basketball_agent.data.schemas.base import GameStatus, League
from basketball_agent.data.schemas.game import Game

# 中文别名（Phase 12-H 扩展到全部联赛；CBA 的 DB 名本身就是中文）。
# 方向：别名 -> team_id；NBA 表保持旧名导出兼容。
NBA_CHINESE_ALIASES: dict[str, str] = {
    "老鹰": "ATL", "鹰队": "ATL",
    "凯尔特人": "BOS", "塞尔特克": "BOS", "绿军": "BOS",
    "篮网": "BKN",
    "黄蜂": "CHA",
    "公牛": "CHI",
    "骑士": "CLE",
    "独行侠": "DAL", "小牛": "DAL", "牛佬": "DAL",
    "掘金": "DEN", "金块": "DEN",
    "活塞": "DET",
    "勇士": "GSW",
    "火箭": "HOU",
    "步行者": "IND",
    "快船": "LAC", "快艇": "LAC",
    "湖人": "LAL",
    "灰熊": "MEM",
    "热火": "MIA",
    "雄鹿": "MIL", "公鹿": "MIL",
    "森林狼": "MIN",
    "鹈鹕": "NOP",
    "尼克斯": "NYK",
    "雷霆": "OKC",
    "魔术": "ORL",
    "76人": "PHI", "七六人": "PHI",
    "太阳": "PHX",
    "开拓者": "POR", "拓荒者": "POR",
    "国王": "SAC",
    "马刺": "SAS",
    "猛龙": "TOR", "速龙": "TOR",
    "爵士": "UTA",
    "奇才": "WAS", "巫师": "WAS",
}

NBL_CHINESE_ALIASES: dict[str, str] = {
    "36人": "NBL-ADL", "阿德莱德": "NBL-ADL",
    "子弹": "NBL-BRI", "布里斯班": "NBL-BRI",
    "毒蛇": "NBL-CNS", "凯恩斯": "NBL-CNS",
    "老鹰": "NBL-ILL", "伊拉瓦拉": "NBL-ILL",
    "墨尔本联": "NBL-MEL", "联队": "NBL-MEL",
    "破坏者": "NBL-NZL", "新西兰": "NBL-NZL",
    "野猫": "NBL-PER", "珀斯": "NBL-PER",
    "凤凰": "NBL-SEM", "东南墨尔本": "NBL-SEM",
    "国王": "NBL-SYD", "悉尼": "NBL-SYD",
    "跳蚁": "NBL-TAS", "塔斯马尼亚": "NBL-TAS",
}

EUROLEAGUE_CHINESE_ALIASES: dict[str, str] = {
    "阿斯维尔": "EL-ASV", "维尔勒班纳": "EL-ASV",
    "巴塞罗那": "EL-BAR",
    "巴斯克尼亚": "EL-BAS",
    "柏林阿尔巴": "EL-BER", "阿尔巴": "EL-BER",
    "中央陆军": "EL-CSK", "莫斯科中央陆军": "EL-CSK",
    "迪拜": "EL-DUB",
    "泽尼特": "EL-DYR", "圣彼得堡": "EL-DYR",
    "夏普尔": "EL-HTA",
    "艾菲斯": "EL-IST",
    "希姆基": "EL-KHI",
    "皇马": "EL-MAD", "皇家马德里": "EL-MAD",
    "摩纳哥": "EL-MCO",
    "米兰": "EL-MIL",
    "拜仁": "EL-MUN", "拜仁慕尼黑": "EL-MUN",
    "奥林匹亚科斯": "EL-OLY",
    "帕纳辛奈科斯": "EL-PAN", "帕纳辛纳科斯": "EL-PAN",
    "巴黎": "EL-PRS",
    "红星": "EL-RED", "贝尔格莱德红星": "EL-RED",
    "马卡比": "EL-TEL",
    "费内巴切": "EL-ULK",
    "尤尼斯": "EL-UNK", "喀山": "EL-UNK",
    "瓦伦西亚": "EL-VAL",
    "博洛尼亚": "EL-VIR", "维尔图斯": "EL-VIR",
    "扎尔吉里斯": "EL-ZAL", "萨拉基利斯": "EL-ZAL", "考纳斯": "EL-ZAL",
}

ACB_CHINESE_ALIASES: dict[str, str] = {
    "巴塞罗那": "ACB-BAR",
    "贝蒂斯": "ACB-BET",
    "巴斯克尼亚": "ACB-BKN",
    "布雷甘": "ACB-BRE",
    "布尔戈斯": "ACB-BUR",
    "萨拉戈萨": "ACB-CAZ",
    "拉科鲁尼亚": "ACB-COR",
    "格拉纳达": "ACB-COV",
    "大加那利": "ACB-DGC", "加那利": "ACB-DGC",
    "富恩拉夫拉达": "ACB-FUE",
    "圣塞巴斯蒂安": "ACB-GBC",
    "赫罗纳": "ACB-GIR",
    "莱里达": "ACB-ILE",
    "巴达洛纳": "ACB-JOV", "久文图特": "ACB-JOV",
    "特内里费": "ACB-LLT",
    "曼雷萨": "ACB-MAN",
    "安道尔": "ACB-MBA",
    "奥布拉多伊罗": "ACB-MOB",
    "大学生": "ACB-MOV", "马德里学生": "ACB-MOV",
    "皇马": "ACB-RMB", "皇家马德里": "ACB-RMB",
    "毕尔巴鄂": "ACB-SBB",
    "穆尔西亚": "ACB-UCM",
    "乌尼卡哈": "ACB-UNI", "马拉加": "ACB-UNI",
    "瓦伦西亚": "ACB-VBC",
    "帕伦西亚": "ACB-ZPA",
}

BSL_CHINESE_ALIASES: dict[str, str] = {
    "达鲁沙法卡": "BSL-164379",
    "亚洛瓦": "BSL-164393",
    "艾菲斯": "BSL-246936",
    "巴赫切希尔": "BSL-246960",
    "贝西克塔斯": "BSL-246961",
    "布尔萨": "BSL-246962",
    "切克梅杰": "BSL-246963",
    "埃罗克": "BSL-246964",
    "费内巴切": "BSL-246965",
    "加拉塔萨雷": "BSL-246966",
    "卡尔舍亚卡": "BSL-246967",
    "马尼萨": "BSL-246968",
    "梅尔凯曾芬迪": "BSL-246969",
    "梅尔辛": "BSL-246970",
    "石化": "BSL-246971", "阿利亚加": "BSL-246971",
    "托法什": "BSL-246972",
    "特拉布宗": "BSL-246973",
    "土耳其电信": "BSL-246974",
    "班德尔马": "BSL-265993",
    "恰伊罗瓦": "BSL-265994",
    "代尼兹利": "BSL-266028",
}

CHINESE_ALIASES_BY_LEAGUE: dict[str, dict[str, str]] = {
    "NBA": NBA_CHINESE_ALIASES,
    "NBL": NBL_CHINESE_ALIASES,
    "EUROLEAGUE": EUROLEAGUE_CHINESE_ALIASES,
    "ACB": ACB_CHINESE_ALIASES,
    "BSL": BSL_CHINESE_ALIASES,
}


class TeamResolutionError(ValueError):
    """Input did not resolve to exactly one team in the league."""


def resolve_team(repo: Repository, league: str, text: str) -> str:
    """User text -> canonical team_id inside one league (fail-loud)."""
    query = str(text).strip()
    if not query:
        raise TeamResolutionError("empty team input")
    league = str(league).upper()
    teams = [t for t in repo.list_teams() if t.league.value == league]
    if not teams:
        raise TeamResolutionError(f"league {league!r} has no teams in the DB")

    lowered = query.lower()
    # 1) exact id / 2) exact abbreviation
    for t in teams:
        if lowered == t.team_id.lower() or lowered == t.abbreviation.lower():
            return t.team_id
    # 3) NBA aliases (western aliases like 'gsw', 'ny', plus ESPN quirks)
    if league == "NBA":
        from basketball_agent.data.collectors.nba_scoreboard import (
            ESPN_ABBR_TO_NBA,
        )
        from basketball_agent.data.collectors.nba_teams import ALIAS_TO_ID

        for table in (ALIAS_TO_ID, ESPN_ABBR_TO_NBA):
            hit = table.get(lowered) or table.get(query.upper())
            if hit is not None:
                return hit
    # 4) Chinese aliases (every league with a table; CBA DB names already
    # match at step 5).  NBA's franchise map is normative (30 fixed codes);
    # other leagues use opaque source ids, so the entry must exist in the
    # roster — a stale table line fails loudly instead of mis-resolving.
    hit_cn = CHINESE_ALIASES_BY_LEAGUE.get(league, {}).get(query)
    if hit_cn is not None:
        if league == "NBA" or any(t.team_id == hit_cn for t in teams):
            return hit_cn
    # 5) unique substring on stored name (e.g. "bucks", "Milwaukee")
    candidates = {t.team_id for t in teams
                  if lowered in t.name.lower()}
    if len(candidates) == 1:
        return candidates.pop()
    if len(candidates) > 1:
        raise TeamResolutionError(
            f"{query!r} is ambiguous in {league}: "
            + ", ".join(sorted(
                f"{t.team_id} ({t.name})" for t in teams
                if t.team_id in candidates)))
    raise TeamResolutionError(
        f"{query!r} matches no {league} team. Known: "
        + ", ".join(sorted(
            f"{t.team_id} ({t.name})" for t in teams)))


def find_scheduled(
    repo: Repository,
    league: str,
    home_id: str,
    away_id: str,
    *,
    now: datetime | None = None,
    horizon_days: int = 14,
) -> Game | None:
    """The next SCHEDULED game between the two teams (home/away fixed).

    Swapped home/away is a different matchup — NOT matched, by design.
    """
    now = now or datetime.now(timezone.utc)
    window_end = now + timedelta(days=horizon_days)
    league_enum = League(league.upper())
    candidates = [
        g for g in repo.list_games(status=GameStatus.SCHEDULED,
                                   league=league_enum)
        if g.home_team_id == home_id and g.away_team_id == away_id
        and now <= g.game_date <= window_end
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda g: g.game_date)


def ensure_nba_upcoming(db, state_dir, *, n_days: int = 8):
    """One live ESPN refresh so find_scheduled can see the near future."""
    from basketball_agent.collection.nba_fixtures import refresh_nba_upcoming

    return refresh_nba_upcoming(db, state_dir, n_days=n_days)
