import warnings; warnings.filterwarnings('ignore')
import sys, numpy as np, pandas as pd
sys.path.insert(0, r"C:\Training\jul26_bde_crypto")
from scipy import stats
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from src import config
from src.features import FAMILLES, colonnes_explicatives, construire_groupes
from src.labeling import etiqueter_groupes
from src.preprocessing import INTERVAL_SECONDS
from src.scoring import bon_sens_directionnel

PROFIL, LARGEUR, HORIZON = 'swing', 5.0, 48
brut = pd.read_parquet(config.ROOT/'data'/'extract'/f'{PROFIL}.parquet')
var = construire_groupes(brut, FAMILLES)
etiq = etiqueter_groupes(brut, largeur=LARGEUR, horizon=HORIZON)
cle = ['symbol','interval','open_time']
jeu = var.merge(etiq[cle+['label']], on=cle, how='inner').dropna(subset=['label'])
jeu = jeu.sort_values('open_time').reset_index(drop=True)
jeu['pas_de_temps'] = np.log(jeu['interval'].map(INTERVAL_SECONDS))
cols = colonnes_explicatives(jeu)

def modele():
    return Pipeline([('i',SimpleImputer(strategy='median')),('n',StandardScaler()),
        ('m',RandomForestClassifier(n_estimators=100,min_samples_leaf=20,n_jobs=-1,random_state=0))])

# --- A. Le modele COMMUN, decompose par paire ---
c = int(len(jeu)*0.8)
p = modele(); p.fit(jeu[cols][:c], jeu['label'][:c].astype(int))
test = jeu.iloc[c:].copy(); test['pred'] = p.predict(test[cols])

print(f"PROFIL {PROFIL.upper()} — le modele COMMUN marche-t-il mieux sur certaines paires ?\n")
print(f"{'paire':<10}{'bon sens':>10}{'ordres':>9}{'p brute':>10}{'p corrigee':>12}   verdict")
print('-'*66)
lignes = []
for paire in sorted(test['symbol'].unique()):
    s = test[test['symbol']==paire]
    v, pr = s['label'].astype(int).to_numpy(), s['pred'].to_numpy()
    m = (pr!=0)&(v!=0); n = int(m.sum())
    if n < 100: continue
    sens = (pr[m]==v[m]).mean()
    pv = stats.binomtest(round(sens*n), n, 0.5, alternative='greater').pvalue
    lignes.append((paire, sens, n, pv))
# Correction de Bonferroni : 5 paires testees = 5 chances de faux positif
k = len(lignes)
for paire, sens, n, pv in sorted(lignes, key=lambda x:-x[1]):
    pc = min(pv*k, 1.0)
    verdict = 'significatif' if pc < 0.05 else 'bruit'
    print(f'{paire:<10}{sens:>10.4f}{n:>9,}{pv:>10.4f}{pc:>12.4f}   {verdict}')

# --- B. Un modele DEDIE par paire ---
print(f"\nUn modele ENTRAINE sur une seule paire fait-il mieux ?\n")
print(f"{'paire':<10}{'commun':>10}{'dedie':>10}{'ecart':>9}{'lignes':>10}")
print('-'*49)
for paire in sorted(jeu['symbol'].unique()):
    sous = jeu[jeu['symbol']==paire].reset_index(drop=True)
    cs = int(len(sous)*0.8)
    if len(sous)-cs < 200: continue
    pp = modele(); pp.fit(sous[cols][:cs], sous['label'][:cs].astype(int))
    pr = pp.predict(sous[cols][cs:]); v = sous['label'][cs:].astype(int).to_numpy()
    dedie = bon_sens_directionnel(v, pr)
    commun = next(l[1] for l in lignes if l[0]==paire)
    print(f'{paire:<10}{commun:>10.4f}{dedie:>10.4f}{dedie-commun:>+9.4f}{len(sous):>10,}')
