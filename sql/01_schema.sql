-- =====================================================================
--  CryptoBot - Etape 2 : schema de la couche exploitable
--  Modele decrit et justifie dans docs/architecture_etape2.pdf
--
--  Ce script est joue automatiquement a la creation du conteneur.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------
--  1. Referentiel
-- ---------------------------------------------------------------------

CREATE TABLE trading_profiles (
    profile       TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    description   TEXT,
    -- NULL signifie "tout l'historique disponible" (cas du swing).
    history_days  INTEGER,
    CONSTRAINT history_days_positif CHECK (history_days IS NULL OR history_days > 0)
);

COMMENT ON TABLE trading_profiles IS
    'Les trois strategies visees. Chacune definit ses pas de temps et sa profondeur.';

CREATE TABLE intervals (
    interval          TEXT PRIMARY KEY,
    duration_seconds  INTEGER NOT NULL,
    -- Table de faits qui STOCKE physiquement ce pas de temps. Les autres
    -- profils qui l'utilisent y accedent par une vue (voir 04_views.sql).
    owner_table       TEXT NOT NULL,
    CONSTRAINT duration_positive CHECK (duration_seconds > 0)
);

COMMENT ON COLUMN intervals.owner_table IS
    'Regle "la profondeur la plus longue l''emporte" : chaque pas de temps '
    'n''est stocke qu''une fois, dans le profil qui le conserve le plus longtemps.';

CREATE TABLE profile_intervals (
    profile         TEXT NOT NULL REFERENCES trading_profiles(profile) ON DELETE CASCADE,
    interval        TEXT NOT NULL REFERENCES intervals(interval)       ON DELETE CASCADE,
    -- false = le profil utilise ce pas de temps mais ne le stocke pas.
    is_owner        BOOLEAN NOT NULL,
    retention_days  INTEGER,
    PRIMARY KEY (profile, interval)
);

COMMENT ON COLUMN profile_intervals.is_owner IS
    'Encode dans le modele, et non dans le code, quel profil stocke quoi. '
    'Une ligne (scalping, 15m, false) se lit : le scalping utilise le 15m '
    'mais ne le stocke pas.';

CREATE TABLE symbols (
    symbol        TEXT PRIMARY KEY,
    base_asset    TEXT NOT NULL,
    quote_asset   TEXT NOT NULL,
    status        TEXT NOT NULL,
    -- Precisions de cotation, issues des filtres exchangeInfo.
    tick_size     NUMERIC(20, 10),
    step_size     NUMERIC(20, 10),
    listed_since  TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE symbols IS
    'Referentiel des paires. Alimente par /api/v3/exchangeInfo. Seuls les '
    'champs stables et scalaires sont ici ; les filtres polymorphes restent '
    'dans MongoDB (collection exchange_info).';

-- ---------------------------------------------------------------------
--  2. Faits : une table par profil de trading
-- ---------------------------------------------------------------------
--  Les trois tables ont EXACTEMENT la meme structure. On la definit une
--  fois, puis on clone : le SQL lui-meme garantit qu'elles ne peuvent pas
--  diverger, ce qu'une triple copie manuelle ne garantirait pas.
-- ---------------------------------------------------------------------

CREATE TABLE candles_scalping (
    symbol           TEXT        NOT NULL,
    interval         TEXT        NOT NULL,
    open_time        TIMESTAMPTZ NOT NULL,
    close_time       TIMESTAMPTZ NOT NULL,

    open             DOUBLE PRECISION NOT NULL,
    high             DOUBLE PRECISION NOT NULL,
    low              DOUBLE PRECISION NOT NULL,
    close            DOUBLE PRECISION NOT NULL,

    volume           DOUBLE PRECISION NOT NULL,
    quote_volume     DOUBLE PRECISION NOT NULL,
    nb_trades        INTEGER          NOT NULL,
    taker_buy_base   DOUBLE PRECISION NOT NULL,
    taker_buy_quote  DOUBLE PRECISION NOT NULL,

    -- Provenance : REST pour l'historique, WS pour le temps reel.
    source           TEXT NOT NULL DEFAULT 'REST',
    -- Identifiant du document MongoDB d'origine. Pas de cle etrangere
    -- possible entre deux moteurs : le lien est logique, pas contraint.
    raw_ref          TEXT,
    inserted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- La cle metier validee a l'etape 1 : zero doublon sur 2 M de lignes.
    -- TimescaleDB impose que la colonne de partitionnement (open_time) en
    -- fasse partie.
    PRIMARY KEY (symbol, interval, open_time),

    -- Coherence OHLC : verifiee a l'etape 1, on l'inscrit dans le schema
    -- pour qu'aucune ecriture future ne puisse l'enfreindre.
    CONSTRAINT ohlc_coherent CHECK (
        high >= GREATEST(open, close, low) AND
        low  <= LEAST(open, close, high)
    ),
    CONSTRAINT volumes_positifs CHECK (volume >= 0 AND quote_volume >= 0),
    CONSTRAINT bougie_ordonnee  CHECK (close_time > open_time),
    CONSTRAINT source_connue    CHECK (source IN ('REST', 'WS'))
);

CREATE TABLE candles_day_trading (LIKE candles_scalping INCLUDING ALL);
CREATE TABLE candles_swing       (LIKE candles_scalping INCLUDING ALL);

-- LIKE ne copie pas les cles etrangeres : on les ajoute aux trois tables.
ALTER TABLE candles_scalping
    ADD CONSTRAINT fk_scalping_symbol   FOREIGN KEY (symbol)   REFERENCES symbols(symbol),
    ADD CONSTRAINT fk_scalping_interval FOREIGN KEY (interval) REFERENCES intervals(interval);
ALTER TABLE candles_day_trading
    ADD CONSTRAINT fk_day_symbol        FOREIGN KEY (symbol)   REFERENCES symbols(symbol),
    ADD CONSTRAINT fk_day_interval      FOREIGN KEY (interval) REFERENCES intervals(interval);
ALTER TABLE candles_swing
    ADD CONSTRAINT fk_swing_symbol      FOREIGN KEY (symbol)   REFERENCES symbols(symbol),
    ADD CONSTRAINT fk_swing_interval    FOREIGN KEY (interval) REFERENCES intervals(interval);

-- ---------------------------------------------------------------------
--  3. Hypertables
-- ---------------------------------------------------------------------
--  TimescaleDB decoupe chaque table en tranches temporelles ("chunks").
--  Une requete sur une periode ne lit que les tranches concernees, et la
--  purge d'anciennes donnees devient un DROP de tranche plutot qu'un
--  DELETE massif.
--
--  L'intervalle de tranche est dimensionne pour que chacune reste de
--  taille raisonnable compte tenu de la densite du profil :
--    scalping    1m/5m tres denses  -> tranches de 7 jours
--    day_trading 15m/1h             -> tranches de 30 jours
--    swing       4h/1d/1w tres peu denses -> tranches de 365 jours
-- ---------------------------------------------------------------------

SELECT create_hypertable('candles_scalping',    'open_time',
                         chunk_time_interval => INTERVAL '7 days',
                         migrate_data => TRUE);
SELECT create_hypertable('candles_day_trading', 'open_time',
                         chunk_time_interval => INTERVAL '30 days',
                         migrate_data => TRUE);
SELECT create_hypertable('candles_swing',       'open_time',
                         chunk_time_interval => INTERVAL '365 days',
                         migrate_data => TRUE);

-- Index de lecture : le modele de l'etape 3 lira toujours "une paire, un
-- pas de temps, sur une periode". L'index descendant sert aussi le cas
-- "les N dernieres bougies", qui sera celui de l'API a l'etape 4.
CREATE INDEX idx_scalping_lecture ON candles_scalping    (symbol, interval, open_time DESC);
CREATE INDEX idx_day_lecture      ON candles_day_trading (symbol, interval, open_time DESC);
CREATE INDEX idx_swing_lecture    ON candles_swing       (symbol, interval, open_time DESC);

-- ---------------------------------------------------------------------
--  4. Journal des collectes
-- ---------------------------------------------------------------------
--  C'est docs/rapport_qualite.json de l'etape 1, transpose en base.
--  Sans cette table, impossible de savoir QUAND une donnee est entree ni
--  si la collecte s'est bien passee. Elle servira a mesurer la derive
--  (etape 4) et a monitorer la production (etape 5).
-- ---------------------------------------------------------------------

CREATE TABLE ingestion_runs (
    run_id            BIGSERIAL PRIMARY KEY,
    profile           TEXT REFERENCES trading_profiles(profile),
    symbol            TEXT NOT NULL,
    interval          TEXT NOT NULL,
    target_table      TEXT NOT NULL,

    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,

    rows_read         INTEGER,
    rows_inserted     INTEGER,
    rows_skipped      INTEGER,

    period_start      TIMESTAMPTZ,
    period_end        TIMESTAMPTZ,
    missing_candles   INTEGER,
    completeness_pct  NUMERIC(9, 4),

    source            TEXT,
    raw_collection    TEXT,
    status            TEXT NOT NULL DEFAULT 'running',
    error_message     TEXT,

    CONSTRAINT status_connu CHECK (status IN ('running', 'success', 'failed'))
);

CREATE INDEX idx_runs_recents ON ingestion_runs (started_at DESC);
CREATE INDEX idx_runs_cible   ON ingestion_runs (symbol, interval, started_at DESC);

COMMENT ON TABLE ingestion_runs IS
    'Une ligne par (paire, pas de temps) et par execution du pipeline. '
    'Permet de repondre a "quand cette donnee est-elle entree, et la '
    'collecte etait-elle saine ?".';
