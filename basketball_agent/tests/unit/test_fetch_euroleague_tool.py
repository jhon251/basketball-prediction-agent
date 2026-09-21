"""B4 fetch tool: bundle assembly must match the adapter contract (offline)."""
from __future__ import annotations

import pytest

from basketball_agent.data.collectors.euroleague_live import (
    DATASET_MARKER,
    EuroLeagueDataError,
    parse_euroleague_bundle,
)
from tools.fetch_euroleague_bundle import FetchError, _is_header_payload, build_bundle

pytestmark = pytest.mark.unit


class TestBuildBundle:
    def test_bundle_shape_matches_adapter_contract(self):
        headers = {"2": {"CodeTeamA": "BER", "CodeTeamB": "PAN", "Phase": "RS"},
                   "1": {"CodeTeamA": "MAD", "CodeTeamB": "MIL", "Phase": "RS"}}
        boxscores = {"1": {"Stats": []}, "2": {"Stats": []}}
        bundle = build_bundle("E2023", headers, boxscores, empty_game_codes=[3, 4])
        assert bundle["dataset"] == DATASET_MARKER
        assert bundle["season_code"] == "E2023"
        assert bundle["source"] == "https://live.euroleague.net/api"
        assert bundle["n_headers"] == 2 and bundle["n_boxscores"] == 2
        # keys sorted numerically as strings, which is what the adapter iterates
        assert list(bundle["headers"]) == ["1", "2"]
        assert bundle["empty_game_codes"] == [3, 4]

    def test_adapter_rejects_foreign_dataset(self):
        bundle = build_bundle("E2023", {}, {})
        bundle["dataset"] = "something-else"
        with pytest.raises(EuroLeagueDataError, match="not a EuroLeague bundle"):
            parse_euroleague_bundle(bundle)

    def test_empty_headers_rejected_by_adapter(self):
        with pytest.raises(EuroLeagueDataError, match="no headers"):
            parse_euroleague_bundle(build_bundle("E2023", {}, {}))


class TestHeaderDetection:
    def test_real_header_payload_detected(self):
        assert _is_header_payload({"CodeTeamA": "BER", "CodeTeamB": "PAN"}) is True
        assert _is_header_payload({"TeamA": "ALBA Berlin"}) is True

    def test_empty_payload_rejected(self):
        assert _is_header_payload({}) is False
        assert _is_header_payload(None) is False
        assert _is_header_payload({"foo": "bar"}) is False


class TestFetchErrorSurface:
    def test_fetch_error_is_runtime_error(self):
        assert issubclass(FetchError, RuntimeError)


class TestCheckpointing:
    def test_fetch_season_checkpoints_incrementally(self, monkeypatch):
        """A mid-season interruption must leave resumable progress on disk."""
        from tools import fetch_euroleague_bundle as tool

        def fake_fetch_json(url, **kwargs):
            if "Header" in url:
                code = int(url.split("gameCode=")[1].split("&")[0])
                if code <= 3:
                    return {"CodeTeamA": "RED", "CodeTeamB": "ASV", "Phase": "REGULAR SEASON"}
                return {}
            return {"Stats": []}

        monkeypatch.setattr(tool, "fetch_json", fake_fetch_json)
        checkpoints: list[dict] = []
        bundle = tool.fetch_season(
            "E2023", max_game_code=6, trailing_empty_stop=2,
            checkpoint=checkpoints.append, checkpoint_every=2, verbose=False,
        )
        assert bundle["n_headers"] == 3
        assert bundle["n_boxscores"] == 3
        # checkpoint fired during the run (before the final return value)
        assert checkpoints, "expected incremental checkpoints"
        assert checkpoints[0]["n_headers"] >= 1

    def test_resume_skips_already_fetched_codes(self, monkeypatch):
        from tools import fetch_euroleague_bundle as tool

        calls: list[str] = []

        def fake_fetch_json(url, **kwargs):
            calls.append(url)
            code = int(url.split("gameCode=")[1].split("&")[0])
            if "Header" in url and code <= 2:
                return {"CodeTeamA": "RED", "CodeTeamB": "ASV", "Phase": "REGULAR SEASON"}
            return {} if "Header" in url else {"Stats": []}

        monkeypatch.setattr(tool, "fetch_json", fake_fetch_json)
        existing = tool.build_bundle(
            "E2023", {"1": {"CodeTeamA": "RED", "CodeTeamB": "ASV"}}, {"1": {"Stats": []}}
        )
        bundle = tool.fetch_season(
            "E2023", max_game_code=4, trailing_empty_stop=2,
            existing=existing, verbose=False,
        )
        assert "gameCode=1&" in "".join(calls) or all("gameCode=1&" not in c for c in calls)
        assert bundle["n_headers"] == 2  # code 1 reused, code 2 fetched
        assert not any("gameCode=1&" in c for c in calls), "resume must not re-fetch code 1"
