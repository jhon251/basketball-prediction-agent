"""BasketNews EuroLeague injury-report adapter (Phase 12-J).

Source: the continuously-updated article
``https://basketnews.com/news-212393-euroleague-injury-report-updated.html``
("EuroLeague Injury Report (updated daily)").  The article body is a set of
per-team tables ``| Pos | Player | Status | Round | Comments |`` covering all
20 EuroLeague clubs; the site itself publishes the status colour guide:

    Ready / Expected / Questionable / Game-time decision /
    Doubtful / Out / Uncertain

Acquisition reality (probed 2026-10-25): basketnews.com answers **403** to
every plain-HTTP client path (homepage, article, RSS) even with full browser
headers — Cloudflare-level bot protection, not a header problem.  The daily
snapshot therefore enters the system as a RENDERED TEXT/HTML FILE (browser
save-as, or any rendering service), exactly like the other user-side raw
bundles; ``fetch_article`` is kept for environments where the block is
lifted and fails loudly with that guidance otherwise.

Hard mapping rules (same doctrine as espn_injuries):
* OUT must be 0.0, AVAILABLE must be 1.0; every other status maps
  explicitly, unknown statuses fail loudly (never guessed);
* team names resolve through the euroleague_live alias table (exact
  display-name map below), unknown teams fail loudly;
* the ``Round`` column names the round(s) a status applies to
  ("Round 5", "Round 5-6", or open-ended "Indefinitely"/"Long-term"/
  "Out for season" -> ``None`` = applies to the audit window);
* player identity is the NAME STRING until a roster source exists —
  records join to boxscores by normalised full name (diacritics folded),
  and unresolved names are counted, never guessed.
"""
from __future__ import annotations

import re
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ARTICLE_URL = (
    "https://basketnews.com/news-212393-euroleague-injury-report-updated.html"
)
_TIMEOUT = 30
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "basketball-agent/1.0 (+injury collection)"
)

# BasketNews per-team table display name -> official EuroLeague CodeTeam
# value (all 20 clubs of the 2026-27 report; resolved further through
# euroleague_live.resolve_team so the alias table stays the single truth).
TEAM_DISPLAY_TO_CODE: dict[str, str] = {
    "anadolu efes istanbul": "IST",
    "armani olimpia milan": "MIL",
    "besiktas istanbul": "BES",
    "crvena zvezda meridianbet belgrade": "RED",
    "dubai basketball": "DUB",
    "fc barcelona": "BAR",
    "fc bayern munich": "MUN",
    "fenerbahce beko istanbul": "ULK",
    "hapoel ibi tel aviv": "HTA",
    "kosner baskonia vitoria-gasteiz": "BAS",
    "ldlc asvel villeurbanne": "ASV",
    "maccabi rapyd tel aviv": "TEL",
    "olympiacos piraeus": "OLY",
    "panathinaikos aktor athens": "PAN",
    "paris basketball": "PRS",
    "partizan mozzart bet belgrade": "PAR",
    "real madrid": "MAD",
    "valencia basket": "VAL",
    "virtus bologna": "VIR",
    "zalgiris kaunas": "ZAL",
}

# Report status -> (PlayerAvailabilityStatus, availability_probability).
# OUT/AVAILABLE are pinned by DATA_REQUIREMENTS (0.0 / 1.0); "Uncertain"
# (coach's-decision / unknown) and "Game-time" share the least-informative
# QUESTIONABLE bucket (0.5) — documented estimates, never guessed.
STATUS_MAP: dict[str, tuple[str, float]] = {
    "Ready": ("AVAILABLE", 1.0),
    "Expected": ("PROBABLE", 0.75),
    "Questionable": ("QUESTIONABLE", 0.5),
    "Game-time": ("QUESTIONABLE", 0.5),
    "Doubtful": ("DOUBTFUL", 0.25),
    "Out": ("OUT", 0.0),
    "Uncertain": ("QUESTIONABLE", 0.5),
}

_ROUND_RANGE_RE = re.compile(r"Round\s+(\d+)(?:\s*-\s*(\d+))?", re.IGNORECASE)


class BasketNewsFetchError(RuntimeError):
    """Network / HTTP failure fetching the BasketNews article."""


class UnknownInjuryStatusError(ValueError):
    """The report used a status outside STATUS_MAP (never guessed)."""


class UnknownTeamError(ValueError):
    """A team section name outside the 20-club display map."""


def normalize_name(raw: str) -> str:
    """Diacritics-folded, casefolded, whitespace-collapsed identity key.

    Shared with the DNP audit: report names ("Ąžuolas Tubelis") and
    Boxscore names ("TUBELIS, AZUOLAS") must meet on one key.
    """
    text = unicodedata.normalize("NFKD", str(raw))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().split())


def parse_round_targets(raw: str) -> frozenset[int] | None:
    """``Round 5`` -> {5}, ``Round 5-6`` -> {5, 6}, open-ended -> None.

    Open-ended markers ("Indefinitely", "Long-term", "Out for season")
    mean the status applies to every game in the window, not to a round.
    Unparseable round text fails loudly — it is targeting information,
    not decoration.
    """
    text = str(raw or "").strip()
    if not text or text == "—":
        return None
    match = _ROUND_RANGE_RE.search(text)
    if match is None:
        # open-ended (no round number at all) is the expected alternative
        if not re.search(r"\d", text):
            return None
        raise ValueError(f"unparseable Round targeting: {raw!r}")
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else first
    if last < first:
        raise ValueError(f"inverted Round range: {raw!r}")
    return frozenset(range(first, last + 1))


class _HTMLToPipes(HTMLParser):
    """Table-preserving HTML -> pipe-line text.

    Cell boundaries become ``|`` and row boundaries newlines so an HTML
    snapshot parses with the same code path as the rendered-markdown one.
    Non-tabular markup is dropped.
    """

    _CELL_TAGS = {"td", "th"}
    _ROW_TAGS = {"tr"}
    _SKIP = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._lines: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._CELL_TAGS and self._buf:
            self._buf.append("|")
        elif tag in self._ROW_TAGS:
            if self._buf:
                self._flush_line()

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._ROW_TAGS:
            if self._buf:
                self._flush_line()

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._buf.append(data.strip())

    def _flush_line(self):
        line = " ".join(self._buf).strip()
        self._buf = []
        if line:
            self._lines.append(line)

    def close(self):
        super().close()
        if self._buf:
            self._flush_line()

    def text(self) -> str:
        return "\n".join(self._lines)


def html_to_pipes(html_text: str) -> str:
    parser = _HTMLToPipes()
    parser.feed(html_text)
    parser.close()
    return parser.text()


def load_snapshot_file(path: Path | str) -> str:
    """Rendered snapshot (markdown-ish text or saved HTML) -> pipe lines."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    if "<html" in raw[:2048].lower() or "<table" in raw.lower()[:2048]:
        return html_to_pipes(raw)
    return raw


def fetch_article(*, timeout: int = _TIMEOUT) -> str:
    """Direct fetch of the article HTML.

    Currently answers 403 to every plain client (Cloudflare, probed
    2026-10-25): this call is kept for environments where that is lifted
    and raises with the snapshot-file guidance otherwise.
    """
    req = urllib.request.Request(ARTICLE_URL, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — network boundary
        raise BasketNewsFetchError(
            f"direct fetch of basketnews.com failed ({type(exc).__name__}: "
            f"{exc}); the site blocks plain HTTP clients — save the rendered "
            f"article ({ARTICLE_URL}) to a file and ingest the snapshot "
            "instead"
        ) from exc


def resolve_display_team(section_text: str) -> str:
    """One team-section line -> canonical ``EL-XXX`` id.

    The rendered table doubles the team name ("Zalgiris Kaunas Zalgiris
    Kaunas"); a longest-substring match against the display map handles
    both spellings.  Unknown sections fail loudly.
    """
    text = normalize_name(section_text)
    for display in sorted(TEAM_DISPLAY_TO_CODE, key=len, reverse=True):
        if display in text:
            return resolve_display_code(TEAM_DISPLAY_TO_CODE[display])
    raise UnknownTeamError(
        f"unknown team section {section_text!r} (extend "
        "basketnews_injuries.TEAM_DISPLAY_TO_CODE; never guess)"
    )


def resolve_display_code(code: str) -> str:
    from basketball_agent.data.collectors.euroleague_live import resolve_team

    return resolve_team(code)


@dataclass(frozen=True)
class BasketNewsInjuryRecord:
    team_id: str                 # canonical EL-XXX
    player_name: str             # as printed by the source
    position: str                # PG/SG/SF/PF/C (informational)
    status_raw: str              # "Out", "Questionable", ...
    status: str                  # PlayerAvailabilityStatus value
    probability: float
    round_targets: frozenset[int] | None   # None = open-ended
    round_raw: str
    comments: str


@dataclass
class BasketNewsReport:
    records: list[BasketNewsInjuryRecord] = field(default_factory=list)
    n_team_sections: int = 0
    fetched_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))


_TEAM_HEADER_RE = re.compile(r"Image\s+\d+\s*:\s*(.+)")


def parse_report(text: str, *, fetched_at: datetime | None = None) -> BasketNewsReport:
    """Pipe-line text of the injury article -> structured records.

    Fail-loud on unknown statuses and unknown team sections; per-row
    structural noise (column headers, separators, prose) is skipped.
    """
    fetched_at = fetched_at or datetime.now(timezone.utc)
    report = BasketNewsReport(fetched_at=fetched_at)
    current_team: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cells = [c.strip() for c in line.split("|")]
        if cells and not cells[0]:
            cells = cells[1:]
        if cells and not cells[-1]:
            cells = cells[:-1]
        if not cells:
            continue
        section = _team_section(cells)
        if section is not None:
            current_team = resolve_display_team(section)
            report.n_team_sections += 1
            continue
        if current_team is None:
            continue
        record = _data_row(cells, current_team)
        if record is not None:
            report.records.append(record)
    return report


def _team_section(cells: list[str]) -> str | None:
    """Return the section text if this line introduces a team table.

    ``Image N: <name>`` prefixes are definitive sections; a bare
    single-cell line only counts when the WHOLE line is a known team
    name (possibly doubled by the renderer) — prose that merely mentions
    a team must not flip the active section.
    """
    first = cells[0]
    match = _TEAM_HEADER_RE.search(first)
    if match is not None:
        return match.group(1)
    if len(cells) == 1:
        text = normalize_name(first)
        for display in TEAM_DISPLAY_TO_CODE:
            if text in (display, f"{display} {display}"):
                return first
    return None


def _data_row(cells: list[str], team_id: str) -> BasketNewsInjuryRecord | None:
    if len(cells) < 5:
        return None
    position, player, status_raw = cells[0], cells[1], cells[2]
    if (position.upper() in ("POS", "P")
            and player.upper() == "PLAYER"):
        return None  # column header row (the live table titles it "P")
    if set(player) <= {"-"} or set(status_raw) <= {"-"}:
        return None  # markdown separator row
    if not player or not status_raw:
        return None
    mapped = STATUS_MAP.get(status_raw)
    if mapped is None:
        raise UnknownInjuryStatusError(
            f"status {status_raw!r} for {player!r} ({team_id}) not in "
            "STATUS_MAP — the report format changed; extend the map, "
            "never guess"
        )
    round_raw = cells[3]
    return BasketNewsInjuryRecord(
        team_id=team_id,
        player_name=player,
        position=position.upper(),
        status_raw=status_raw,
        status=mapped[0],
        probability=mapped[1],
        round_targets=parse_round_targets(round_raw),
        round_raw=round_raw,
        comments=cells[4] if cells[4] not in ("—", "-") else "",
    )
