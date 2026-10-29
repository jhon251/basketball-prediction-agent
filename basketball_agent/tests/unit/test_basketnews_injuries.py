"""Phase 12-J: BasketNews EuroLeague injury-report extractor."""
from __future__ import annotations

from pathlib import Path

import pytest

from basketball_agent.data.collectors.basketnews_injuries import (
    STATUS_MAP, UnknownInjuryStatusError, UnknownTeamError,
    html_to_pipes, load_snapshot_file, normalize_name, parse_report,
    parse_round_targets, resolve_display_team,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / (
    "basketnews_injuries_20261025.md")


@pytest.fixture(scope="module")
def report():
    return parse_report(load_snapshot_file(FIXTURE))


class TestStatusMap:
    def test_seven_statuses_pinned(self):
        assert STATUS_MAP["Out"] == ("OUT", 0.0)
        assert STATUS_MAP["Ready"] == ("AVAILABLE", 1.0)
        assert STATUS_MAP["Doubtful"] == ("DOUBTFUL", 0.25)
        assert STATUS_MAP["Expected"] == ("PROBABLE", 0.75)
        assert STATUS_MAP["Questionable"] == ("QUESTIONABLE", 0.5)
        assert STATUS_MAP["Game-time"] == ("QUESTIONABLE", 0.5)
        assert STATUS_MAP["Uncertain"] == ("QUESTIONABLE", 0.5)


class TestParseReport:
    def test_team_sections_and_record_count(self, report):
        assert report.n_team_sections == 4
        assert len(report.records) == 10

    def test_team_resolution_including_doubled_names(self, report):
        by_team: dict[str, list] = {}
        for r in report.records:
            by_team.setdefault(r.team_id, []).append(r)
        assert set(by_team) == {"EL-IST", "EL-ZAL", "EL-BES", "EL-ULK"}

    def test_every_status_appears_mapped(self, report):
        by_status = {r.player_name: r.status for r in report.records}
        assert by_status["Isaia Cordinier"] == "OUT"
        assert by_status["Vincent Poirier"] == "AVAILABLE"
        assert by_status["Darius Thompson"] == "QUESTIONABLE"  # Game-time
        assert by_status["Ąžuolas Tubelis"] == "PROBABLE"      # Expected
        assert by_status["Nikola Marcan"] == "QUESTIONABLE"
        assert by_status["Arnas Butkevicius"] == "DOUBTFUL"
        assert by_status["Kyle Allman"] == "OUT"
        assert by_status["Ethan Happ"] == "QUESTIONABLE"       # Uncertain
        assert by_status["Neoklis Avdalas"] == "OUT"
        assert by_status["Georgios Papagiannis"] == "OUT"

    def test_out_probability_is_hard_zero(self, report):
        for r in report.records:
            if r.status == "OUT":
                assert r.probability == 0.0

    def test_round_targeting(self, report):
        by_name = {r.player_name: r for r in report.records}
        assert by_name["Isaia Cordinier"].round_targets is None  # open
        assert by_name["Vincent Poirier"].round_targets == frozenset({5})
        assert by_name["Nikola Marcan"].round_targets == frozenset({5, 6})
        assert by_name["Neoklis Avdalas"].round_targets is None
        assert by_name["Kyle Allman"].round_targets == frozenset({5, 6})

    def test_comments_kept_and_dashes_dropped(self, report):
        by_name = {r.player_name: r for r in report.records}
        assert by_name["Darius Thompson"].comments == "ankle sprain"
        assert by_name["Vincent Poirier"].comments == ""
        assert by_name["Neoklis Avdalas"].comments == "shoulder surgery"

    def test_prose_team_mention_does_not_flip_section(self, report):
        # the intro mentions "Real Madrid" and "Panathinaikos AKTOR
        # Athens" — neither may become the active section
        assert all(r.team_id != "EL-MAD" for r in report.records)
        assert all(r.team_id != "EL-PAN" for r in report.records)

    def test_unknown_status_fails_loud(self):
        text = ("| Image 1: Zalgiris Kaunas Zalgiris Kaunas | | | | |\n"
                "| Pos | Player | Status | Round | Comments |\n"
                "| C | X Player | Maybe | Round 5 | — |\n")
        with pytest.raises(UnknownInjuryStatusError):
            parse_report(text)

    def test_unknown_team_section_fails_loud(self):
        text = "| Image 1: CSKA Moscow CSKA Moscow | | | | |\n"
        with pytest.raises(UnknownTeamError):
            parse_report(text)


class TestRoundTargets:
    def test_single_and_ranges(self):
        assert parse_round_targets("Round 5") == frozenset({5})
        assert parse_round_targets("Round 5-6") == frozenset({5, 6})
        assert parse_round_targets("Round 1-3") == frozenset({1, 2, 3})
        assert parse_round_targets("Round 4-12") == frozenset(range(4, 13))

    def test_open_ended_is_none(self):
        assert parse_round_targets("Indefinitely") is None
        assert parse_round_targets("Long-term") is None
        assert parse_round_targets("Out for season") is None
        assert parse_round_targets("—") is None

    def test_inverted_range_fails(self):
        with pytest.raises(ValueError):
            parse_round_targets("Round 6-5")

    def test_round_word_without_number_is_open_ended(self):
        assert parse_round_targets("Round ?") is None


class TestResolveDisplayTeam:
    @pytest.mark.parametrize("display,expected", [
        ("Zalgiris Kaunas Zalgiris Kaunas", "EL-ZAL"),
        ("Besiktas Istanbul Besiktas Istanbul", "EL-BES"),
        ("Armani Olimpia Milan Armani Olimpia Milan", "EL-MIL"),
        ("Kosner Baskonia Vitoria-Gasteiz Kosner Baskonia Vitoria-Gasteiz",
         "EL-BAS"),
        ("Real Madrid Real Madrid", "EL-MAD"),
    ])
    def test_display_names_resolve(self, display, expected):
        assert resolve_display_team(display) == expected


class TestNormalizeName:
    def test_diacritics_folded(self):
        assert normalize_name("Ąžuolas Tubelis") == "azuolas tubelis"

    def test_boxscore_last_first_form(self):
        # the audit joins report "First Last" to boxscore "LAST, FIRST"
        assert normalize_name("AZUOLAS TUBELIS") == "azuolas tubelis"
        first, last = "TUBELIS, AZUOLAS".split(",", 1)
        assert normalize_name(f"{last.strip()} {first.strip()}") == (
            "azuolas tubelis")


class TestHtmlSnapshot:
    def test_html_table_becomes_pipe_lines(self):
        html = """<html><body>
        <h2>Image 3: Anadolu Efes Istanbul Anadolu Efes Istanbul</h2>
        <table><tr><th>Pos</th><th>Player</th><th>Status</th>
        <th>Round</th><th>Comments</th></tr>
        <tr><td>SG</td><td>Isaia Cordinier</td><td>Out</td>
        <td>Indefinitely</td><td>rehab</td></tr></table></body></html>"""
        report = parse_report(html_to_pipes(html))
        assert report.n_team_sections == 1
        assert len(report.records) == 1
        assert report.records[0].team_id == "EL-IST"
        assert report.records[0].status == "OUT"

    def test_html_saved_page_detection(self, tmp_path):
        page = tmp_path / "page.html"
        page.write_text("<html><body><p>hello</p></body></html>",
                        encoding="utf-8")
        assert "<" not in load_snapshot_file(page)
