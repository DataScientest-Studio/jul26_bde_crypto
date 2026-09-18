-- =====================================================================
--  CryptoBot - Positions virtuelles (trading sur papier)
--
--  L'API predisait bougie par bougie, sans rien memoriser. C'est correct
--  pour MESURER un modele, mais un bot doit suivre ses positions : sans
--  etat, impossible de dire si un trade a rapporte quelque chose.
--
--  Cette table joue le role du carnet de positions. Aucun argent reel,
--  aucun ordre envoye a Binance : on note ce que le bot AURAIT fait.
--
--  A appliquer sur une base deja creee :
--    docker exec -i crypto_postgres psql -U cryptobot -d cryptobot < sql/07_positions.sql
-- =====================================================================

CREATE TABLE IF NOT EXISTS positions_virtuelles (
    position_id        BIGSERIAL PRIMARY KEY,
    symbol             TEXT        NOT NULL,
    interval           TEXT        NOT NULL,
    style              TEXT        NOT NULL,
    sens               SMALLINT    NOT NULL,

    -- Bougie dont la cloture a declenche l'ordre. Sert aussi de garde-fou :
    -- une meme bougie ne peut pas ouvrir deux positions.
    bougie_signal      TIMESTAMPTZ NOT NULL,
    ouverte_a          TIMESTAMPTZ NOT NULL DEFAULT now(),

    prix_entree        DOUBLE PRECISION NOT NULL,
    take_profit        DOUBLE PRECISION NOT NULL,
    stop_loss          DOUBLE PRECISION NOT NULL,
    echeance           TIMESTAMPTZ NOT NULL,
    probabilite_hausse DOUBLE PRECISION,

    statut             TEXT NOT NULL DEFAULT 'ouverte',
    fermee_a           TIMESTAMPTZ,
    prix_sortie        DOUBLE PRECISION,
    rendement_brut_pct DOUBLE PRECISION,
    rendement_net_pct  DOUBLE PRECISION,

    CONSTRAINT sens_connu   CHECK (sens IN (-1, 1)),
    CONSTRAINT statut_connu CHECK (statut IN ('ouverte', 'take profit', 'stop loss', 'echeance')),
    -- Une bougie ne declenche qu'une position par paire, pas de temps et style.
    CONSTRAINT une_position_par_bougie UNIQUE (symbol, interval, style, bougie_signal)
);

COMMENT ON TABLE positions_virtuelles IS
    'Carnet de positions simulees : ce que le bot aurait fait, sans argent reel.';

CREATE INDEX IF NOT EXISTS idx_positions_ouvertes
    ON positions_virtuelles (symbol, interval, style, statut);

-- Bilan par style : c'est la reponse a "est-ce que ca rapporte ?".
CREATE OR REPLACE VIEW v_positions_bilan AS
    SELECT style,
           COUNT(*) FILTER (WHERE statut = 'ouverte')                      AS ouvertes,
           COUNT(*) FILTER (WHERE statut <> 'ouverte')                     AS fermees,
           COUNT(*) FILTER (WHERE statut = 'take profit')                  AS take_profit,
           COUNT(*) FILTER (WHERE statut = 'stop loss')                    AS stop_loss,
           COUNT(*) FILTER (WHERE statut = 'echeance')                     AS echeance,
           ROUND(AVG(rendement_net_pct) FILTER (WHERE statut <> 'ouverte')::numeric, 4) AS gain_moyen_net_pct,
           ROUND(SUM(rendement_net_pct) FILTER (WHERE statut <> 'ouverte')::numeric, 4) AS cumul_net_pct
    FROM positions_virtuelles
    GROUP BY style;
