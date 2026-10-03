"""Production auto-prediction loop (Phase 10-E.2).

One operational pass over the six isolated leagues:

    1. collection discovery (CollectionScheduler, per-league isolated)
    2. scheduled-game upsert (row-based sources: BSL/NBL) — calendar facts
       enter the core as SCHEDULED rows; the internal game_id is assigned
       here and recorded in the source-id ledger so the later Final update
       reuses the SAME id (never renumbered, never duplicated)
    3. prediction pass — SCHEDULED games with kickoff strictly in the
       future, no saved prediction and no decision-log record are
       predicted (frozen core, as_of = loop boundary) and persisted
       (SettlementStore + Decision Log).  Retroactive predictions for
       started/finished games are FORBIDDEN: those count as `missed`.
    4. settlement pass — FINAL games with saved unsettled predictions are
       settled through the unchanged SettlementStore; settlement events
       are appended to the Decision Log per stored decision record.

Explicit rules (boolean over existing facts; no weights, no scores, no
LLM): see run().  Failures are isolated per game and per league, recorded
in the report and the collector state; a crashed pass leaves at most
"missing" records that the next pass safely retries (the Decision Log
append is content-hash idempotent).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from basketball_agent.agents.decision import DecisionAgent
from basketball_agent.agents.decision_log import DecisionLog
from basketball_agent.agents.decision_record import DecisionRecordAgent
from basketball_agent.agents.persistence import PersistResult, settle_and_log
from basketball_agent.agents.persistence import persist as persist_bundle
from basketball_agent.agents.prediction_agent import PredictionAgent
from basketball_agent.agents.risk_analysis import RiskAnalysisAgent
from basketball_agent.collection.ledger import SourceGameLedger
from basketball_agent.collection.scheduler import CollectionScheduler
from basketball_agent.collection.state import CollectorState, now_utc


@dataclass
class LoopReport:
    discovered: int = 0
    upserted_scheduled: int = 0
    skipped_no_date: int = 0
    predicted: list[dict] = field(default_factory=list)
    skipped_already_predicted: int = 0
    skipped_missed_kickoff: int = 0
    skipped_insufficient: int = 0
    settled: list[dict] = field(default_factory=list)
    awaiting_refresh: int = 0
    errors: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (f"discovered={self.discovered} "
                f"upserted_scheduled={self.upserted_scheduled} "
                f"skipped_no_date={self.skipped_no_date} "
                f"predicted={len(self.predicted)} "
                f"already_predicted={self.skipped_already_predicted} "
                f"missed={self.skipped_missed_kickoff} "
                f"insufficient={self.skipped_insufficient} "
                f"awaiting_refresh={self.awaiting_refresh} "
                f"settled={len(self.settled)} errors={len(self.errors)}")


def _norm_season(label: str) -> str:
    parts = str(label).split("-")
    if len(parts) == 2 and len(parts[1]) == 4:
        return f"{parts[0]}-{parts[1][-2:]}"
    return str(label)


class ProductionLoop:
    def __init__(self, db, scheduler: CollectionScheduler,
                 core: PredictionAgent, log: DecisionLog,
                 state: CollectorState, *, state_dir: Path | str) -> None:
        self._db = db
        self._scheduler = scheduler
        self._core = core
        self._log = log
        self._state = state
        self._state_dir = Path(state_dir)
        self._decision = DecisionAgent(core)
        self._risk = RiskAnalysisAgent()
        self._record = DecisionRecordAgent()
        self._ledger = SourceGameLedger(self._state_dir)

    # ------------------------------------------------------------------ run
    def run(self, leagues: list[str] | None = None, *,
            now: datetime | None = None,
            max_predictions: int | None = None,
            skip_collection: bool = False) -> LoopReport:
        now = now or datetime.now(timezone.utc)
        report = LoopReport()

        if not skip_collection:
            results = self._scheduler.run(leagues)
            report.discovered = sum(r.new_games + r.updated_games
                                    for r in results)

        self._upsert_scheduled_pass(report, leagues=leagues, now=now)
        self._prediction_pass(report, now=now, leagues=leagues,
                              max_predictions=max_predictions)
        self._settlement_pass(report, leagues=leagues)

        self._state.update(
            "LOOP", status="success" if not report.errors else "partial",
            source="production-loop",
            new_games=len(report.predicted),
            updated_games=len(report.settled),
            errors=[f"{e.get('stage')}/{e.get('league', '')}: "
                    f"{e.get('error', '')[:120]}" for e in report.errors],
            extra={"last_loop_summary": report.summary(),
                   "last_loop_at": now_utc()})
        return report

    # ------------------------------------------------- scheduled-game upsert
    def _upsert_scheduled_pass(self, report: LoopReport, *,
                               leagues: list[str] | None,
                               now: datetime) -> None:
        """Row-based sources (BSL/NBL): not-yet-played matches enter the
        core as SCHEDULED rows.  The internal game_id is assigned here
        (sequence continuation) and recorded in the ledger so the later
        Final update reuses the SAME id."""
        from basketball_agent.data.collectors.bsl_livestats import (
            resolve_team as bsl_resolve,
        )
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.schemas.base import (
            Competition, GameStatus, League,
        )

        repo = Repository(self._db)
        row_sources = {"BSL", "NBL", "CBA"}
        for league in (leagues or sorted(row_sources)):
            if league not in row_sources:
                continue
            try:
                discovered = self._scheduler.discover_rows(league)
            except Exception as exc:  # noqa: BLE001 — isolation
                report.errors.append({"stage": "upsert", "league": league,
                                      "error": str(exc)})
                continue
            season_norm = _norm_season(
                self._scheduler.last_season(league) or "")
            if not season_norm:
                continue
            prefix = f"{league}{season_norm[:4][-2:]}"
            next_seq = self._ledger.max_game_seq(league, prefix) + 1
            upserted = 0
            for r in discovered.get("rows", []):
                source_id = str(r["source_id"])
                if self._ledger.has(league, source_id):
                    continue  # already upserted/ingested (ledger is the truth)
                if str(r.get("status_raw")) == "1":
                    continue  # played: enters via the season-refresh gate
                if _has_score(r):
                    continue  # live/mid-game: not predictable either
                if not str(r.get("date") or "").strip():
                    # no parseable kickoff on the calendar row: a scheduled
                    # game cannot enter the core without a date — counted,
                    # never guessed (NBL 2026-27 rows with a blank legacy
                    # date field surfaced as 145 per-row errors once)
                    report.skipped_no_date += 1
                    continue
                game_id = f"{prefix}-{next_seq:04d}"
                next_seq += 1
                try:
                    if league == "BSL":
                        home = bsl_resolve(r["home_id"], season=season_norm)
                        away = bsl_resolve(r["away_id"], season=season_norm)
                    elif league == "CBA":
                        home = f"CBA-{r['home_id']}"
                        away = f"CBA-{r['away_id']}"
                    else:
                        home, away = r["home_id"], r["away_id"]
                    Repository(self._db).insert_game(type("G", (), {
                        "game_id": game_id,
                        "league": League[league],
                        "season": season_norm,
                        "game_date": datetime.strptime(
                            r["date"] + " " + r.get("match_time", "00:00"),
                            "%Y-%m-%d %H:%M").replace(
                            tzinfo=_league_tz(league)),
                        "home_team_id": home, "away_team_id": away,
                        "home_score": None, "away_score": None,
                        "status": GameStatus.SCHEDULED,
                        "competition": Competition.REGULAR_SEASON,
                    }))
                    self._state.update_seen_games(
                        league, {source_id: _fingerprint(r)})
                    self._ledger.record(league, season_norm, source_id,
                                        game_id)
                    upserted += 1
                except Exception as exc:  # noqa: BLE001 — per-row isolation
                    report.errors.append({
                        "stage": "upsert", "league": league,
                        "source_id": source_id, "error": str(exc)})
            report.upserted_scheduled += upserted

    # ------------------------------------------------------ prediction pass
    def _prediction_pass(self, report: LoopReport, *, now: datetime,
                         leagues: list[str] | None,
                         max_predictions: int | None) -> None:
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.schemas.base import GameStatus

        repo = Repository(self._db)
        scheduled = repo.list_games(status=GameStatus.SCHEDULED)
        by_league: dict[str, list] = {}
        for g in scheduled:
            if leagues and g.league.value not in leagues:
                continue
            by_league.setdefault(g.league.value, []).append(g)

        budget = max_predictions
        for league in sorted(by_league):
            for g in sorted(by_league[league], key=lambda x: x.game_date):
                if budget is not None and budget <= 0:
                    return
                if g.game_date <= now:
                    # kickoff already passed (or in progress): a "pre-game"
                    # prediction would be future-leaking — never generated
                    report.skipped_missed_kickoff += 1
                    continue
                if self._log.records_for_game(g.game_id):
                    report.skipped_already_predicted += 1
                    continue
                try:
                    prediction = self._core.predict(g.game_id, as_of=now)
                    self._save_prediction(prediction)
                    agent_decision = self._decision.decide(
                        g.game_id, as_of=now)
                    risk = self._risk.analyze(agent_decision)
                    record = self._record.build(agent_decision, risk)
                    persisted = persist_bundle(agent_decision, risk,
                                               record, self._log)
                    rid, status = persisted.record_id, persisted.status
                    report.predicted.append({
                        "game_id": g.game_id, "league": league,
                        "record_id": rid, "log_status": status,
                        "prob_home": prediction.win_probability_home})
                    if budget is not None:
                        budget -= 1
                except Exception as exc:  # noqa: BLE001 — isolation
                    report.skipped_insufficient += 1
                    report.errors.append({
                        "stage": "predict", "league": league,
                        "game_id": g.game_id,
                        "error": f"{type(exc).__name__}: {exc}"})

    def _save_prediction(self, prediction) -> None:
        from basketball_agent.evaluation.settlement import SettlementStore

        SettlementStore(self._db).save(prediction)

    # ------------------------------------------------------ settlement pass
    def _settlement_pass(self, report: LoopReport,
                         leagues: list[str] | None) -> None:
        from basketball_agent.data.db.repository import Repository
        from basketball_agent.data.schemas.base import GameStatus

        repo = Repository(self._db)
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT game_id FROM predictions "
                "WHERE settled = 0").fetchall()
        for row in rows:
            game_id = row["game_id"]
            game = repo.get_game(game_id)
            if game is None:
                continue
            if leagues and game.league.value not in leagues:
                continue
            if game.status is not GameStatus.FINAL:
                continue  # not decided yet; settles on a later pass
            try:
                outcomes = settle_and_log(self._db, self._log, game_id)
                for rid, status, settled in outcomes:
                    report.settled.append({
                        "game_id": game_id, "record_id": rid,
                        "log_status": status, "correct": settled.correct})
            except Exception as exc:  # noqa: BLE001 — isolation
                report.errors.append({"stage": "settle",
                                      "league": game.league.value,
                                      "game_id": game_id,
                                      "error": str(exc)})


def _league_tz(league: str):
    """Kickoff timezone per league (BSL/NBL row sources)."""
    if league == "BSL":
        from datetime import timedelta
        return timezone(timedelta(hours=3))  # Türkiye permanent UTC+3
    return timezone.utc


def _has_score(row: dict) -> bool:
    """Source scores are strings; empty string means not played yet."""
    hs = row.get("home_score")
    as_ = row.get("away_score")
    return hs not in (None, "") and as_ not in (None, "")


def _fingerprint(row: dict) -> str:
    import hashlib

    payload = json.dumps({"s": row.get("status_raw"),
                          "h": row.get("home_score"),
                          "a": row.get("away_score")}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
