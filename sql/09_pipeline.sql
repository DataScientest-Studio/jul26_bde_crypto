-- =============================================================================
--  Etape 5 : ce que le pipeline automatise laisse derriere lui
--
--  Trois journaux, ecrits par les taches Airflow et lus par Grafana :
--    controles_qualite   chaque verification des donnees fraichement collectees
--    derive_mesures      chaque mesure de derive, paire par paire
--    reentrainements     chaque duel champion / challenger, et sa decision
--
--  Plus un compte en LECTURE SEULE pour Grafana : un tableau de bord n'a
--  aucune raison de pouvoir modifier ou effacer une donnee.
-- =============================================================================

CREATE TABLE IF NOT EXISTS controles_qualite (
    controle_id        BIGSERIAL PRIMARY KEY,
    controle_le        TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol             TEXT        NOT NULL,
    interval           TEXT        NOT NULL,
    derniere_bougie    TIMESTAMPTZ,
    retard_minutes     DOUBLE PRECISION,
    -- Bougies attendues mais absentes sur la fenetre controlee.
    bougies_manquantes INTEGER     NOT NULL DEFAULT 0,
    -- Bougies dont le haut est sous le bas, ou la cloture hors de [bas, haut].
    bougies_incoherentes INTEGER   NOT NULL DEFAULT 0,
    statut             TEXT        NOT NULL,
    CONSTRAINT statut_controle CHECK (statut IN ('ok', 'alerte', 'echec'))
);
CREATE INDEX IF NOT EXISTS idx_controles_recents ON controles_qualite (controle_le DESC);

CREATE TABLE IF NOT EXISTS derive_mesures (
    mesure_id          BIGSERIAL PRIMARY KEY,
    mesure_le          TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol             TEXT        NOT NULL,
    interval           TEXT        NOT NULL,
    fenetre_jours      INTEGER     NOT NULL,
    bougies_analysees  INTEGER     NOT NULL,
    psi_median         DOUBLE PRECISION,
    psi_maximum        DOUBLE PRECISION,
    variables_en_derive_forte   INTEGER NOT NULL,
    variables_en_derive_moderee INTEGER NOT NULL,
    verdict            TEXT
);
CREATE INDEX IF NOT EXISTS idx_derive_recente ON derive_mesures (mesure_le DESC);

CREATE TABLE IF NOT EXISTS reentrainements (
    reentrainement_id  BIGSERIAL PRIMARY KEY,
    lance_le           TIMESTAMPTZ NOT NULL DEFAULT now(),
    date_coupure       TIMESTAMPTZ NOT NULL,
    extrait_sha256     TEXT        NOT NULL,
    fenetre_jours      INTEGER     NOT NULL,
    -- Periode d'evaluation : jamais vue par le challenger, commune aux deux.
    evaluation_debut   TIMESTAMPTZ NOT NULL,
    evaluation_fin     TIMESTAMPTZ NOT NULL,
    champion_version   TEXT,
    champion_accuracy  DOUBLE PRECISION,
    champion_ordres    INTEGER,
    challenger_version TEXT        NOT NULL,
    challenger_accuracy DOUBLE PRECISION,
    challenger_ordres  INTEGER,
    decision           TEXT        NOT NULL,
    raison             TEXT        NOT NULL,
    mlflow_run_id      TEXT,
    CONSTRAINT decision_connue CHECK (decision IN ('promu', 'rejete'))
);

COMMENT ON TABLE reentrainements IS
    'Un duel par reentrainement : le modele en service (champion) et le nouveau '
    '(challenger) sont mesures sur la MEME periode, que le challenger n''a jamais vue. '
    'Le challenger ne remplace le champion que s''il fait au moins aussi bien.';

-- --- Compte de lecture pour Grafana -------------------------------------------
-- Le mot de passe vient de l'environnement du conteneur (GRAFANA_DB_PASSWORD) :
-- \getenv est une commande de psql, qui execute ce fichier a l'initialisation.
-- Sans la variable, \getenv laisse la valeur intacte : on part d'une chaine vide.
\set mot_de_passe_grafana ''
\getenv mot_de_passe_grafana GRAFANA_DB_PASSWORD
SELECT :'mot_de_passe_grafana' <> '' AS mot_de_passe_fourni \gset
\if :mot_de_passe_fourni
    SELECT format('CREATE ROLE grafana_lecture LOGIN PASSWORD %L', :'mot_de_passe_grafana')
     WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_lecture') \gexec
    SELECT format('GRANT CONNECT ON DATABASE %I TO grafana_lecture', current_database()) \gexec
    GRANT USAGE ON SCHEMA public TO grafana_lecture;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_lecture;
    -- Les tables creees plus tard seront lisibles, sans rien de plus.
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_lecture;
\else
    \echo 'GRAFANA_DB_PASSWORD absent : compte grafana_lecture non cree'
\endif
