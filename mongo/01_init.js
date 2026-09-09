// =====================================================================
//  CryptoBot - Etape 2 : initialisation de la couche brute
//
//  MongoDB stocke ce que PostgreSQL ne sait pas modeliser proprement :
//  des documents dont la forme varie d'un type a l'autre.
//
//  Ce script est joue automatiquement a la creation du conteneur.
// =====================================================================

const dbName = process.env.MONGO_INITDB_DATABASE || "cryptobot";
db = db.getSiblingDB(dbName);

// ---------------------------------------------------------------------
//  raw_klines : les reponses de /api/v3/klines, intactes
// ---------------------------------------------------------------------
//  payload est un tableau de tableaux positionnels. On ne le touche pas :
//  c'est justement ce qui permet de rejouer la normalisation si elle
//  evolue, sans redemander six ans de donnees a Binance.
// ---------------------------------------------------------------------
db.createCollection("raw_klines");
db.raw_klines.createIndex(
    { symbol: 1, interval: 1, fetched_at: -1 },
    { name: "idx_symbol_interval_fetched" }
);

// ---------------------------------------------------------------------
//  raw_stream : les messages WebSocket, tous types confondus
// ---------------------------------------------------------------------
//  C'est ici que le schema libre devient indispensable : kline, trade et
//  bookTicker n'ont aucun champ en commun au-dela du symbole.
//
//  On conserve aussi les bougies NON cloturees (x = false), ecartees par
//  le pipeline vers PostgreSQL. Elles ne servent pas au modele, mais
//  permettront a l'etape 5 de verifier qu'aucun message n'a ete perdu.
// ---------------------------------------------------------------------
db.createCollection("raw_stream");
db.raw_stream.createIndex(
    { stream_type: 1, symbol: 1, received_at: -1 },
    { name: "idx_type_symbol_received" }
);
// Les bougies cloturees sont les seules a remonter en PostgreSQL :
// un index partiel les retrouve sans indexer les 96 % restants.
db.raw_stream.createIndex(
    { symbol: 1, "payload.k.t": 1 },
    { name: "idx_closed_candles", partialFilterExpression: { "payload.k.x": true } }
);

// ---------------------------------------------------------------------
//  exchange_info : les metadonnees et leurs filtres polymorphes
// ---------------------------------------------------------------------
//  PRICE_FILTER, LOT_SIZE et NOTIONAL n'ont pas les memes cles. C'est
//  l'exemple qui a motive le choix d'une base document (section 03 du
//  rapport d'architecture).
//
//  Une paire peut apparaitre plusieurs fois : Binance modifie ses filtres
//  au fil du temps, et l'historique de ces changements a de la valeur.
// ---------------------------------------------------------------------
db.createCollection("exchange_info");
db.exchange_info.createIndex(
    { symbol: 1, fetched_at: -1 },
    { name: "idx_symbol_fetched" }
);

// ---------------------------------------------------------------------
//  order_book : instantanes du carnet d'ordres
// ---------------------------------------------------------------------
//  bids et asks arrivent en tableaux de tableaux : [["79324.00", "3.13"]].
//  Non conserve en historique (Binance n'en fournit aucun), mais capture
//  a partir de maintenant.
//
//  Seule collection avec une expiration automatique : ces instantanes
//  perdent leur valeur en quelques jours et grossissent vite.
// ---------------------------------------------------------------------
db.createCollection("order_book");
db.order_book.createIndex({ symbol: 1, captured_at: -1 }, { name: "idx_symbol_captured" });
db.order_book.createIndex(
    { captured_at: 1 },
    { name: "idx_ttl_30j", expireAfterSeconds: 60 * 60 * 24 * 30 }
);

print("Collections creees dans '" + dbName + "' : " + db.getCollectionNames().join(", "));
