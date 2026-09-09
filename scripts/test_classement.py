import warnings; warnings.filterwarnings('ignore')
import sys, numpy as np, pandas as pd
sys.path.insert(0, r"C:\Training\jul26_bde_crypto")
from scipy import stats
from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from src import config
from src.features import FAMILLES, colonnes_explicatives, construire_groupes
from src.labeling import etiqueter_groupes
from src.preprocessing import INTERVAL_SECONDS
from src.backtest import simuler

brut = pd.read_parquet(config.ROOT/'data'/'extract'/'swing.parquet')
var = construire_groupes(brut, FAMILLES)
etiq = etiqueter_groupes(brut, largeur=5.0, horizon=48)
cle = ['symbol','interval','open_time']
jeu = var.merge(etiq[cle+['label','rendement_a_la_sortie','bougies_avant_sortie']], on=cle, how='inner')
jeu = jeu.dropna(subset=['label']).sort_values('open_time').reset_index(drop=True)
jeu['pas_de_temps'] = np.log(jeu['interval'].map(INTERVAL_SECONDS))
cols = [c for c in colonnes_explicatives(jeu) if c not in ('rendement_a_la_sortie','bougies_avant_sortie')]

MODELES = {
 'foret':RandomForestClassifier(n_estimators=100,min_samples_leaf=20,n_jobs=-1,random_state=0),
 'boosting':HistGradientBoostingClassifier(random_state=0,max_iter=150),
 'logistique':LogisticRegression(max_iter=1000)}
PAIRES = sorted(jeu['symbol'].unique())
n = len(jeu)

def performances(debut, fin):
    """Performance de backtest de chaque couple (modele, paire) sur une periode."""
    c = int(n*debut); f = int(n*fin)
    res = {}
    for nom, m in MODELES.items():
        p = Pipeline([('i',SimpleImputer(strategy='median')),('n',StandardScaler()),('m',m)])
        p.fit(jeu[cols][:c], jeu['label'][:c].astype(int))
        t = jeu.iloc[c:f].copy(); t['pred'] = p.predict(t[cols])
        for paire in PAIRES:
            s = t[t.symbol==paire].reset_index(drop=True)
            if len(s) < 300: continue
            r = simuler(s['pred'].to_numpy(), s['rendement_a_la_sortie'],
                        s['bougies_avant_sortie'], fraction_engagee=0.10)
            res[(nom,paire)] = r['performance_pct']
    return res

print("UN CLASSEMENT ETABLI SUR UNE PERIODE TIENT-IL SUR LA SUIVANTE ?\n")
p1 = performances(0.55, 0.775)   # periode A
p2 = performances(0.775, 1.0)    # periode B, posterieure

communs = sorted(set(p1) & set(p2), key=lambda k: -p1[k])
print(f"{'modele':<12}{'paire':<10}{'periode A':>12}{'rang A':>8}{'periode B':>12}{'rang B':>8}")
print('-'*62)
rangs1 = {k:i+1 for i,k in enumerate(sorted(communs, key=lambda k:-p1[k]))}
rangs2 = {k:i+1 for i,k in enumerate(sorted(communs, key=lambda k:-p2[k]))}
for k in communs[:8]:
    print(f"{k[0]:<12}{k[1]:<10}{p1[k]:>11.1f}%{rangs1[k]:>8}{p2[k]:>11.1f}%{rangs2[k]:>8}")
print('   ...')
for k in communs[-3:]:
    print(f"{k[0]:<12}{k[1]:<10}{p1[k]:>11.1f}%{rangs1[k]:>8}{p2[k]:>11.1f}%{rangs2[k]:>8}")

rho, pv = stats.spearmanr([rangs1[k] for k in communs],[rangs2[k] for k in communs])
print()
print(f"Correlation des rangs entre les deux periodes : rho = {rho:+.3f}  (p = {pv:.3f})")
print(f"  rho = +1 : le classement se reproduit parfaitement")
print(f"  rho =  0 : le classement d hier ne dit RIEN sur demain")
print()
gagnant_A = communs[0]
print(f"Le gagnant de la periode A ({gagnant_A[0]} sur {gagnant_A[1]}) finit {rangs2[gagnant_A]}e sur {len(communs)} en periode B.")
