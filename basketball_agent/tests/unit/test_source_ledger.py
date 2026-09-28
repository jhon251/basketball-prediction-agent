"""Source-id ledger tests (Phase 10-E.1)."""
from __future__ import annotations

import pytest

from basketball_agent.collection.ledger import SourceGameLedger


@pytest.fixture()
def ledger(tmp_path) -> SourceGameLedger:
    return SourceGameLedger(tmp_path / "agent_log")


class TestLedger:
    def test_record_and_lookup(self, ledger):
        assert ledger.record("BSL", "2026-27", "346279", "BSL26-0001") == "recorded"
        assert ledger.game_id_for("BSL", "346279") == "BSL26-0001"
        assert ledger.game_id_for("BSL", "346280") is None
        assert ledger.has("BSL", "346279")

    def test_remap_refused(self, ledger):
        ledger.record("BSL", "2026-27", "346279", "BSL26-0001")
        with pytest.raises(ValueError, match="refusing re-map"):
            ledger.record("BSL", "2026-27", "346279", "BSL26-0002")

    def test_same_mapping_idempotent(self, ledger):
        assert ledger.record("BSL", "2026-27", "346279", "BSL26-0001") == "recorded"
        assert ledger.record("BSL", "2026-27", "346279", "BSL26-0001") == "duplicate"

    def test_league_isolation(self, ledger):
        ledger.record("BSL", "2026-27", "346279", "BSL26-0001")
        assert ledger.game_id_for("CBA", "346279") is None
        assert ledger.entries("CBA") == {}

    def test_max_game_seq(self, ledger):
        ledger.record("BSL", "2026-27", "346279", "BSL26-0007")
        ledger.record("BSL", "2026-27", "346280", "BSL26-0011")
        assert ledger.max_game_seq("BSL", "BSL26") == 11
        assert ledger.max_game_seq("BSL", "BSL25") == 0
