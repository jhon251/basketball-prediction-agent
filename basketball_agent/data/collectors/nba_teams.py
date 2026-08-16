"""NBA team identity map (stable IDs + historical aliases).

One canonical ID per franchise so historical relocations / renames never
split a team's history (spec §6: "球队历史改名不会造成错误拆分"):

* NJN -> BKN (New Jersey / Brooklyn Nets)
* SEA -> OKC (Seattle SuperSonics / Oklahoma City Thunder)
* VAN -> MEM (Vancouver / Memphis Grizzlies)
* NOH/NOK -> NOP (New Orleans Hornets / Pelicans)
* CHH -> CHA (Charlotte Hornets (1988-2002) -> Bobcats -> Hornets 2.0)
"""
from __future__ import annotations

NBA_TEAM_IDS: tuple[str, ...] = (
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
)

# canonical id -> historical alias ids (lowercased)
_TEAM_ALIASES: dict[str, tuple[str, ...]] = {
    "BKN": ("njn", "brk", "brooklyn", "new jersey"),
    "OKC": ("sea", "seattle"),
    "MEM": ("van", "vancouver"),
    "NOP": ("noh", "nok", "new orleans"),
    "CHA": ("chh", "bobcats"),
    "GSW": ("gsw", "golden state"),
    "WAS": ("wsh", "washington"),
    "PHX": ("pho", "phoenix"),
}

ALIAS_TO_ID: dict[str, str] = {}
for _canonical in NBA_TEAM_IDS:
    ALIAS_TO_ID[_canonical.lower()] = _canonical
for _canonical, _aliases in _TEAM_ALIASES.items():
    for _alias in _aliases:
        ALIAS_TO_ID[_alias] = _canonical

TEAM_NAMES: dict[str, str] = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}


def canonical_team_id(raw: str) -> str | None:
    """Map any known spelling/alias to the canonical franchise ID."""
    key = raw.strip().lower()
    if key in TEAM_NAMES:
        return key.upper()
    if key in {name.lower() for name in TEAM_NAMES.values()}:
        for tid, name in TEAM_NAMES.items():
            if name.lower() == key:
                return tid
    return ALIAS_TO_ID.get(key)
