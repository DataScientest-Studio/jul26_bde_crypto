-- =====================================================================
--  CryptoBot - Etape 2 : donnees de reference
--
--  Ces valeurs sont la transcription exacte de src/config.py. Elles
--  vivent a deux endroits, ce qui est un compromis assume : la base doit
--  pouvoir etre lue et comprise sans ouvrir le code Python, et le
--  collecteur doit pouvoir tourner sans base. Le test
--  tests/test_db_consistency.py verifie que les deux ne divergent pas.
-- =====================================================================

INSERT INTO trading_profiles (profile, label, description, history_days) VALUES
    ('scalping',    'Scalping',
     'Positions de quelques minutes. Cherche de tres petits mouvements, tres frequents.',
     180),
    ('day_trading', 'Day trading',
     'Positions ouvertes et fermees dans la journee.',
     730),
    ('swing',       'Swing trading',
     'Positions tenues plusieurs jours a plusieurs semaines.',
     NULL);

-- owner_table applique la regle "la profondeur la plus longue l'emporte".
INSERT INTO intervals (interval, duration_seconds, owner_table) VALUES
    ('1m',      60, 'candles_scalping'),
    ('5m',     300, 'candles_scalping'),
    -- 15m est reclame par scalping (180 j) et day_trading (730 j) :
    -- day_trading gagne, il le conserve plus longtemps.
    ('15m',    900, 'candles_day_trading'),
    ('1h',    3600, 'candles_day_trading'),
    -- 4h est reclame par day_trading (730 j) et swing (illimite) :
    -- swing gagne.
    ('4h',   14400, 'candles_swing'),
    ('1d',   86400, 'candles_swing'),
    ('1w',  604800, 'candles_swing');

-- is_owner = false : le profil utilise ce pas de temps mais le lit chez
-- le voisin, via la vue correspondante. Il n'y a donc aucun doublon.
INSERT INTO profile_intervals (profile, interval, is_owner, retention_days) VALUES
    ('scalping',    '1m',  TRUE,  180),
    ('scalping',    '5m',  TRUE,  180),
    ('scalping',    '15m', FALSE, 180),

    ('day_trading', '15m', TRUE,  730),
    ('day_trading', '1h',  TRUE,  730),
    ('day_trading', '4h',  FALSE, 730),

    ('swing',       '4h',  TRUE,  NULL),
    ('swing',       '1d',  TRUE,  NULL),
    ('swing',       '1w',  TRUE,  NULL);
