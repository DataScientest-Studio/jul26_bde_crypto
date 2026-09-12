-- =====================================================================
--  CryptoBot - Variables techniques calculees
--
--  Jusqu'ici les indicateurs etaient recalcules a chaque entrainement, en
--  memoire. Les stocker apporte trois choses :
--
--    - on entraine sur ce qui est EN BASE, pas sur un calcul refait a la
--      volee : deux personnes obtiennent le meme jeu ;
--    - l'API de l'etape 4 lira les variables deja pretes au lieu de
--      recalculer 100 bougies d'historique a chaque appel ;
--    - on peut comparer deux jeux de variables sur les memes bougies.
--
--  Ce fichier n'est PAS joue automatiquement sur un conteneur deja cree :
--  les scripts de sql/ ne s'executent qu'a la creation du volume. Pour
--  l'appliquer a une base existante :
--
--      docker exec -i crypto_postgres psql -U cryptobot -d cryptobot < sql/05_features.sql
-- =====================================================================

-- ---------------------------------------------------------------------
--  Pourquoi JSONB plutot que 26 colonnes nommees
-- ---------------------------------------------------------------------
--  Le jeu de variables BOUGE : il est passe de 26 a 35 colonnes en
--  ajoutant le contexte multi-echelles, et il bougera encore. Avec des
--  colonnes fixes, chaque essai imposerait une migration du schema, et
--  deux jeux differents ne pourraient pas cohabiter.
--
--  Le prix a payer est reel : pas de contrainte de type par variable, et
--  une requete sur une variable precise s'ecrit valeurs->>'rsi_14'. Le
--  compromis est assume - la table de faits (candles_*), elle, reste
--  strictement typee, car sa structure ne change jamais.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS technical_features (
    symbol        TEXT        NOT NULL,
    interval      TEXT        NOT NULL,
    open_time     TIMESTAMPTZ NOT NULL,

    -- Identifiant du jeu de variables utilise. Le detail du calcul (quelles
    -- familles, quelles fenetres, quelle version du code) est dans MongoDB,
    -- collection feature_configs. Meme principe que raw_ref sur les bougies :
    -- le lien entre les deux moteurs est logique, pas contraint.
    config_ref    TEXT        NOT NULL,

    valeurs       JSONB       NOT NULL,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- La cle inclut config_ref : deux jeux de variables peuvent coexister
    -- sur la meme bougie, ce qui est justement le but.
    PRIMARY KEY (symbol, interval, open_time, config_ref),

    CONSTRAINT valeurs_non_vides CHECK (jsonb_typeof(valeurs) = 'object'),

    FOREIGN KEY (symbol)   REFERENCES symbols(symbol),
    FOREIGN KEY (interval) REFERENCES intervals(interval)
);

COMMENT ON TABLE technical_features IS
    'Variables techniques calculees a partir des bougies. Une ligne par '
    '(paire, pas de temps, bougie, jeu de variables). Le detail du calcul '
    'est dans MongoDB, collection feature_configs.';

COMMENT ON COLUMN technical_features.config_ref IS
    'Identifiant du jeu de variables, ex. "base_v1" ou "contexte_v1". '
    'Renvoie a un document de feature_configs dans MongoDB.';

-- Hypertable : meme decoupage que les bougies du day trading, qui est la
-- source la plus dense de cette table.
SELECT create_hypertable('technical_features', 'open_time',
                         chunk_time_interval => INTERVAL '30 days',
                         migrate_data => TRUE,
                         if_not_exists => TRUE);

-- L'entrainement lit "une paire, un pas de temps, un jeu, par ordre de
-- temps". L'API de l'etape 4 lira "les N dernieres", d'ou l'ordre DESC.
CREATE INDEX IF NOT EXISTS idx_features_lecture
    ON technical_features (config_ref, symbol, interval, open_time DESC);

-- ---------------------------------------------------------------------
--  Vue de supervision
-- ---------------------------------------------------------------------
--  Repond a "quelles variables a-t-on calculees, sur quelles bougies, et
--  est-ce que ca couvre bien les donnees disponibles ?".
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_features_coverage AS
    SELECT
        config_ref,
        symbol,
        interval,
        COUNT(*)                                  AS lignes,
        -- Nombre de variables par ligne : si ce chiffre varie au sein d'un
        -- meme config_ref, c'est qu'un calcul a change sans changer
        -- d'identifiant. PostgreSQL n'a pas de fonction "compter les cles"
        -- d'un JSONB : on passe par jsonb_object_keys, qui retourne un
        -- ensemble, dans une sous-requete scalaire.
        MIN((SELECT count(*) FROM jsonb_object_keys(f.valeurs))) AS variables_min,
        MAX((SELECT count(*) FROM jsonb_object_keys(f.valeurs))) AS variables_max,
        MIN(open_time)                            AS premiere_bougie,
        MAX(open_time)                            AS derniere_bougie,
        MAX(computed_at)                          AS dernier_calcul
    FROM technical_features f
    GROUP BY config_ref, symbol, interval;

COMMENT ON VIEW v_features_coverage IS
    'Couverture des variables techniques par jeu, paire et pas de temps.';
