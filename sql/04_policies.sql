-- =====================================================================
--  CryptoBot - Etape 2 : compression et retention
--
--  C'est ce que TimescaleDB apporte au-dela de PostgreSQL nu, et la
--  raison pour laquelle une table par profil prend tout son sens : les
--  politiques s'appliquent PAR TABLE, donc par profil.
-- =====================================================================

-- ---------------------------------------------------------------------
--  Compression
-- ---------------------------------------------------------------------
--  Une bougie ne change jamais apres sa cloture : les tranches anciennes
--  sont donc en lecture seule et peuvent etre compressees sans risque.
--
--  segmentby = 'symbol, interval' regroupe physiquement les lignes d'une
--  meme paire et d'un meme pas de temps, ce qui est exactement notre
--  motif de lecture. La compression sert alors aussi les performances,
--  et pas seulement l'espace disque.
-- ---------------------------------------------------------------------

ALTER TABLE candles_scalping SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, interval',
    timescaledb.compress_orderby   = 'open_time DESC'
);
ALTER TABLE candles_day_trading SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, interval',
    timescaledb.compress_orderby   = 'open_time DESC'
);
ALTER TABLE candles_swing SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, interval',
    timescaledb.compress_orderby   = 'open_time DESC'
);

-- Le delai avant compression est cale sur la densite de chaque profil.
-- Le scalping produit 74 Mo pour six mois de 1m : c'est lui qui gagne le
-- plus a etre compresse tot.
SELECT add_compression_policy('candles_scalping',    INTERVAL '14 days');
SELECT add_compression_policy('candles_day_trading', INTERVAL '90 days');
SELECT add_compression_policy('candles_swing',       INTERVAL '365 days');

-- ---------------------------------------------------------------------
--  Retention
-- ---------------------------------------------------------------------
--  DESACTIVE VOLONTAIREMENT.
--
--  Ces politiques SUPPRIMENT definitivement les tranches trop anciennes.
--  C'est la traduction litterale des profondeurs de chaque profil, et
--  c'est ce qui justifie le decoupage par profil : purger devient un DROP
--  de tranche, pas un DELETE massif.
--
--  Nous les laissons commentees tant que le projet est en construction :
--  une suppression automatique de six mois de donnees pendant l'etape 3
--  ferait perdre le jeu d'entrainement. A activer quand la collecte
--  tournera en continu (etape 5), et seulement apres avoir verifie que le
--  pipeline realimente bien les tables.
--
--    SELECT add_retention_policy('candles_scalping',    INTERVAL '180 days');
--    SELECT add_retention_policy('candles_day_trading', INTERVAL '730 days');
--    -- Aucune retention sur candles_swing : history_days y vaut NULL.
-- ---------------------------------------------------------------------
