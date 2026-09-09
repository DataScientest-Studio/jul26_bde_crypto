-- =====================================================================
--  CryptoBot - Etape 2 : vues par profil
--
--  STOCKER N'EST PAS UTILISER.
--
--  Chaque profil utilise trois pas de temps, mais n'en stocke pas
--  forcement trois. Ces vues restituent a chaque profil l'integralite de
--  ce dont il a besoin, en allant chercher chez le voisin ce qu'il ne
--  stocke pas lui-meme.
--
--  Le modele de l'etape 3 interroge CES VUES, jamais les tables
--  directement : il n'a pas a savoir ou chaque bougie reside physiquement.
-- =====================================================================

-- ---------------------------------------------------------------------
--  Scalping : 1m et 5m en propre, 15m emprunte au day trading
-- ---------------------------------------------------------------------
CREATE VIEW v_scalping AS
    SELECT * FROM candles_scalping
    UNION ALL
    -- Le day trading conserve 730 jours de 15m ; le scalping n'en veut
    -- que 180. On tronque a sa profondeur, sinon il verrait des donnees
    -- que sa strategie considere comme perimees.
    SELECT * FROM candles_day_trading
    WHERE interval = '15m'
      AND open_time >= now() - INTERVAL '180 days';

COMMENT ON VIEW v_scalping IS
    'Les trois pas de temps du scalping : 1m, 5m (stockes) et 15m (emprunte '
    'au day trading, tronque a 180 jours).';

-- ---------------------------------------------------------------------
--  Day trading : 15m et 1h en propre, 4h emprunte au swing
-- ---------------------------------------------------------------------
CREATE VIEW v_day_trading AS
    SELECT * FROM candles_day_trading
    UNION ALL
    SELECT * FROM candles_swing
    WHERE interval = '4h'
      AND open_time >= now() - INTERVAL '730 days';

COMMENT ON VIEW v_day_trading IS
    'Les trois pas de temps du day trading : 15m, 1h (stockes) et 4h '
    '(emprunte au swing, tronque a 730 jours).';

-- ---------------------------------------------------------------------
--  Swing : il stocke tout ce qu'il utilise, la vue est un simple alias
-- ---------------------------------------------------------------------
CREATE VIEW v_swing AS
    SELECT * FROM candles_swing;

COMMENT ON VIEW v_swing IS
    'Le swing possede ses trois pas de temps. La vue existe pour que les '
    'trois profils s''interrogent de la meme facon.';

-- ---------------------------------------------------------------------
--  Vue de supervision : etat de chaque jeu de donnees
-- ---------------------------------------------------------------------
--  Repond en une requete a "qu'est-ce qu'on a en base, et de quand date
--  la derniere bougie ?". Servira au monitoring de l'etape 5.
-- ---------------------------------------------------------------------
CREATE VIEW v_coverage AS
    WITH toutes AS (
        SELECT 'candles_scalping'    AS table_name, symbol, interval, open_time FROM candles_scalping
        UNION ALL
        SELECT 'candles_day_trading',              symbol, interval, open_time FROM candles_day_trading
        UNION ALL
        SELECT 'candles_swing',                    symbol, interval, open_time FROM candles_swing
    )
    SELECT
        t.table_name,
        t.symbol,
        t.interval,
        i.duration_seconds,
        COUNT(*)                                   AS rows_stored,
        MIN(t.open_time)                           AS first_candle,
        MAX(t.open_time)                           AS last_candle,
        -- Retard par rapport a maintenant, exprime en nombre de bougies :
        -- c'est l'indicateur qui dira si la collecte a decroche.
        FLOOR(EXTRACT(EPOCH FROM (now() - MAX(t.open_time))) / i.duration_seconds)::BIGINT
                                                   AS candles_behind
    FROM toutes t
    JOIN intervals i USING (interval)
    GROUP BY t.table_name, t.symbol, t.interval, i.duration_seconds;

COMMENT ON VIEW v_coverage IS
    'Etat de chaque couple (paire, pas de temps) : volume, bornes '
    'temporelles et retard de collecte exprime en nombre de bougies.';
