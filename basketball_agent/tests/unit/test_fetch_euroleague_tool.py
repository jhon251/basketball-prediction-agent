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
