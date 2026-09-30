-- Basketball Simulation & Prediction Agent V1 — storage schema.
-- SQLite DDL; idempotent (CREATE TABLE IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS schema_version (
    version      INTEGER PRIMARY KEY,
    applied_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    team_id      TEXT PRIMARY KEY,
    league       TEXT NOT NULL,
    name         TEXT NOT NULL,
    abbreviation TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    player_id       TEXT PRIMARY KEY,
    league          TEXT NOT NULL,
    name            TEXT NOT NULL,
    current_team_id TEXT,
    FOREIGN KEY (current_team_id) REFERENCES teams (team_id)
);

CREATE TABLE IF NOT EXISTS games (
    game_id      TEXT PRIMARY KEY,
    league       TEXT NOT NULL,
    season       TEXT NOT NULL,
    game_date    TEXT NOT NULL,          -- ISO-8601, timezone-aware
    home_team_id TEXT NOT NULL REFERENCES teams (team_id),
    away_team_id TEXT NOT NULL REFERENCES teams (team_id),
    home_score   INTEGER CHECK (home_score IS NULL OR home_score >= 0),
    away_score   INTEGER CHECK (away_score IS NULL OR away_score >= 0),
    status       TEXT NOT NULL,
    competition  TEXT,                   -- NULL = untagged (legacy rows)
    inserted_at  TEXT NOT NULL,
    CHECK (home_team_id <> away_team_id)
);

CREATE TABLE IF NOT EXISTS team_game_stats (
    game_id     TEXT NOT NULL REFERENCES games (game_id),
    team_id     TEXT NOT NULL REFERENCES teams (team_id),
    is_home     INTEGER NOT NULL,
    minutes     INTEGER NOT NULL CHECK (minutes >= 0),
    points      INTEGER NOT NULL CHECK (points >= 0),
    possessions REAL NOT NULL CHECK (possessions >= 0),
    fg  INTEGER NOT NULL CHECK (fg >= 0),
    fga INTEGER NOT NULL CHECK (fga >= 0),
    fg3 INTEGER NOT NULL CHECK (fg3 >= 0),
    fg3a INTEGER NOT NULL CHECK (fg3a >= 0),
    ft  INTEGER NOT NULL CHECK (ft >= 0),
    fta INTEGER NOT NULL CHECK (fta >= 0),
    orb INTEGER NOT NULL CHECK (orb >= 0),
    drb INTEGER NOT NULL CHECK (drb >= 0),
    trb INTEGER NOT NULL CHECK (trb >= 0),
    ast INTEGER NOT NULL CHECK (ast >= 0),
    tov INTEGER NOT NULL CHECK (tov >= 0),
    stl INTEGER NOT NULL CHECK (stl >= 0),
    blk INTEGER NOT NULL CHECK (blk >= 0),
    pf  INTEGER NOT NULL CHECK (pf >= 0),
    PRIMARY KEY (game_id, team_id)
);

CREATE TABLE IF NOT EXISTS player_game_stats (
    game_id   TEXT NOT NULL REFERENCES games (game_id),
    player_id TEXT NOT NULL REFERENCES players (player_id),
    team_id   TEXT NOT NULL REFERENCES teams (team_id),
    minutes   REAL NOT NULL CHECK (minutes >= 0),
    points    INTEGER NOT NULL CHECK (points >= 0),
    fga  INTEGER NOT NULL CHECK (fga >= 0),
    fg3a INTEGER NOT NULL CHECK (fg3a >= 0),
    fta  INTEGER NOT NULL CHECK (fta >= 0),
    rebounds  INTEGER NOT NULL CHECK (rebounds >= 0),
    assists   INTEGER NOT NULL CHECK (assists >= 0),
    turnovers INTEGER NOT NULL CHECK (turnovers >= 0),
    steals    INTEGER NOT NULL CHECK (steals >= 0),
    blocks    INTEGER NOT NULL CHECK (blocks >= 0),
    fouls     INTEGER NOT NULL CHECK (fouls >= 0),
    PRIMARY KEY (game_id, player_id)
);

CREATE TABLE IF NOT EXISTS player_availability (
    player_id                TEXT NOT NULL REFERENCES players (player_id),
    game_id                  TEXT NOT NULL REFERENCES games (game_id),
    status                   TEXT NOT NULL,
    availability_probability REAL NOT NULL
        CHECK (availability_probability >= 0 AND availability_probability <= 1),
    source                   TEXT NOT NULL,
    source_time              TEXT NOT NULL,
    PRIMARY KEY (player_id, game_id, source_time)
);

CREATE INDEX IF NOT EXISTS idx_games_date        ON games (game_date);
CREATE INDEX IF NOT EXISTS idx_team_stats_team   ON team_game_stats (team_id, game_id);
CREATE INDEX IF NOT EXISTS idx_player_stats_plyr ON player_game_stats (player_id, game_id);
CREATE INDEX IF NOT EXISTS idx_availability_game ON player_availability (game_id, source_time);

-- Predictions are stored with their full context and settled against the
-- final result once the game is FINAL (spec §33: Prediction -> Game ->
-- Result -> Evaluation -> Calibration -> Model Improvement).
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id          TEXT NOT NULL REFERENCES games (game_id),
    as_of            TEXT NOT NULL,
    prob_home        REAL NOT NULL CHECK (prob_home >= 0 AND prob_home <= 1),
    prob_away        REAL NOT NULL CHECK (prob_away >= 0 AND prob_away <= 1),
    expected_margin  REAL,
    expected_total   REAL,
    n_simulations    INTEGER NOT NULL,
    seed             INTEGER,
    confidence       REAL,
    risk_flags       TEXT,
    model_version    TEXT NOT NULL,
    snapshot_id      TEXT,
    created_at       TEXT NOT NULL,
    settled          INTEGER NOT NULL DEFAULT 0,
    settled_at       TEXT,
    actual_home_score INTEGER,
    actual_away_score INTEGER,
    brier            REAL,
    log_loss         REAL,
    correct          INTEGER
);

CREATE TABLE IF NOT EXISTS game_odds (
    odds_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id         TEXT NOT NULL REFERENCES games (game_id),
    league          TEXT NOT NULL,
    bookmaker       TEXT NOT NULL,
    market          TEXT NOT NULL CHECK (market IN ('MONEYLINE', 'SPREAD', 'TOTAL')),
    kind            TEXT NOT NULL CHECK (kind IN ('OPEN', 'CLOSE')),
    line            REAL,
    price_decimal   REAL NOT NULL CHECK (price_decimal >= 1.01 AND price_decimal <= 100.0),
    price_home      REAL,
    price_away      REAL,
    captured_at     TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT '',
    UNIQUE (game_id, bookmaker, market, kind, captured_at)
);
