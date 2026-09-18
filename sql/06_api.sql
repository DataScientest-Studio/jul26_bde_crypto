-- =====================================================================
--  CryptoBot - Etape 4 : trace des predictions de l'API
--
--  Chaque appel a /prediction est archive ici. Sans cette table, on ne
--  pourrait pas repondre a "le modele s'est-il mis a acheter beaucoup
--  plus qu'avant ?", qui est le premier signe visible d'une derive.
--
--  A appliquer sur une base deja creee :
--    docker exec -i crypto_postgres psql -U cryptobot -d cryptobot < sql/06_api.sql
-- =====================================================================

CREATE TABLE IF NOT EXISTS api_predictions (
    prediction_id      BIGSERIAL PRIMARY KEY,
    demande_a          TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol             TEXT        NOT NULL,
    interval           TEXT        NOT NULL,
    style              TEXT        NOT NULL,
    bougie_open_time   TIMESTAMPTZ NOT NULL,
    probabilite_hausse DOUBLE PRECISION NOT NULL,
    seuil              DOUBLE PRECISION NOT NULL,
    decision           TEXT        NOT NULL,

    CONSTRAINT probabilite_valide CHECK (probabilite_hausse BETWEEN 0 AND 1),
    CONSTRAINT decision_connue    CHECK (decision IN ('acheter', 'vendre', 'attendre')),
    CONSTRAINT style_connu        CHECK (style IN ('agressif', 'conservateur'))
);

COMMENT ON TABLE api_predictions IS
    'Une ligne par appel a /prediction : sert a surveiller la derive des '
    'decisions du modele dans le temps.';

CREATE INDEX IF NOT EXISTS idx_predictions_recentes ON api_predictions (demande_a DESC);

-- Repartition des decisions par jour : une bascule brutale (par exemple
-- 90 % de "vendre" alors qu'on etait a 50 %) signale une derive.
CREATE OR REPLACE VIEW v_predictions_par_jour AS
    SELECT date_trunc('day', demande_a) AS jour,
           style,
           COUNT(*)                                             AS predictions,
           COUNT(*) FILTER (WHERE decision = 'acheter')          AS achats,
           COUNT(*) FILTER (WHERE decision = 'vendre')           AS ventes,
           COUNT(*) FILTER (WHERE decision = 'attendre')         AS attentes,
           ROUND(AVG(probabilite_hausse)::numeric, 4)            AS probabilite_moyenne
    FROM api_predictions
    GROUP BY 1, 2
    ORDER BY 1 DESC, 2;
