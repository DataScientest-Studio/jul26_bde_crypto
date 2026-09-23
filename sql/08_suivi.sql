-- =============================================================================
--  Suivi du carnet : jusqu'ou le bot a-t-il regarde le marche ?
--
--  Le carnet de positions n'avancait que lorsque l'interface etait ouverte :
--  chaque appel ne jugeait que la DERNIERE bougie. Page fermee (ou Docker
--  arrete) pendant trois jours = trois jours de signaux jamais vus, et un trou
--  dans l'historique.
--
--  Cette table note, pour chaque paire, pas de temps et style, la derniere
--  bougie evaluee. Au prochain appel, l'API rejoue tout ce qui manque depuis
--  ce repere, dans l'ordre, avec les memes regles que le rejeu historique.
-- =============================================================================

CREATE TABLE IF NOT EXISTS carnet_suivi (
    symbol                   TEXT        NOT NULL,
    interval                 TEXT        NOT NULL,
    style                    TEXT        NOT NULL,
    derniere_bougie_evaluee  TIMESTAMPTZ NOT NULL,
    mis_a_jour               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, interval, style)
);

COMMENT ON TABLE carnet_suivi IS
    'Derniere bougie cloturee evaluee par le bot, par paire, pas de temps et style : '
    'point de depart du rattrapage quand le suivi a ete interrompu.';
