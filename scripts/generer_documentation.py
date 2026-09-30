"""Genere la documentation PDF, une par etape, a partir des sources Markdown.

    docs/documentation/sources/etapeN.md   le texte (versionne, relu, modifie)
    docs/documentation/images/            les schemas
    docs/documentation/*.pdf              le resultat

Chaque source commence par un en-tete :

    ---
    titre: Collecte des donnees
    sous_titre: Binance, bougies et schema commun
    etape: 1
    fichier: CryptoBot_etape1_collecte
    ---

Chaine : Markdown -> HTML (bibliotheque markdown) -> PDF (navigateur Edge ou
Chrome sans interface) -> numeros de page (pymupdf).

Usage :
    python -m scripts.generer_documentation            # les cinq etapes
    python -m scripts.generer_documentation --etapes 2 3
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
DOSSIER = RACINE / "docs" / "documentation"
SOURCES = DOSSIER / "sources"
NAVIGATEURS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "msedge", "google-chrome", "chromium",
]

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

STYLE = """
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&family=IBM+Plex+Sans:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
@page { size: A4; margin: 20mm 19mm 22mm; }
* { box-sizing: border-box; }
body { font-family: 'Source Serif 4', Georgia, serif; font-size: 10.6pt; line-height: 1.5;
       color: #1b1f24; margin: 0; counter-reset: h2; }
h1, h2, h3, h4, th, .garde, .sommaire { font-family: 'IBM Plex Sans', Arial, sans-serif; }
code, pre { font-family: 'IBM Plex Mono', Consolas, monospace; }

/* Page de garde */
.garde { height: 247mm; display: flex; flex-direction: column; justify-content: space-between;
         page-break-after: always; border-top: 3px solid #1f3a5f; padding-top: 14mm; }
.garde .projet { font-size: 11pt; color: #1f3a5f; font-weight: 600; letter-spacing: .02em; }
.garde h1 { font-size: 30pt; line-height: 1.12; margin: 8mm 0 4mm; color: #11161c; font-weight: 700; }
.garde .sous-titre { font-size: 14pt; color: #4a5561; font-weight: 500; }
.garde table { border-collapse: collapse; font-size: 10pt; width: 100%; }
.garde td { padding: 5px 0; border-top: 1px solid #d5dbe2; }
.garde td:first-child { color: #66717d; width: 34%; }

/* Sommaire */
.sommaire { page-break-after: always; }
.sommaire h2 { counter-increment: none; }
.sommaire h2::before { content: none; }
.sommaire ul { list-style: none; padding-left: 0; font-size: 10.5pt; }
.sommaire ul ul { padding-left: 6mm; font-size: 10pt; color: #3a444f; }
.sommaire li { margin: 3px 0; }
.sommaire a { color: inherit; text-decoration: none; }
.sommaire .toc > ul { counter-reset: s1; }
.sommaire .toc > ul > li { counter-increment: s1; margin-top: 2.5mm; font-weight: 600; }
.sommaire .toc > ul > li > a::before { content: counter(s1) ". "; color: #1f3a5f; }
.sommaire .toc > ul > li > ul { counter-reset: s2; font-weight: 400; }
.sommaire .toc > ul > li > ul > li { counter-increment: s2; }
.sommaire .toc > ul > li > ul > li > a::before { content: counter(s1) "." counter(s2) " "; color: #1f3a5f; }

/* Titres numerotes : la numerotation est structurelle, pas decorative */
h2 { font-size: 16pt; color: #11161c; margin: 9mm 0 3mm; padding-bottom: 2mm;
     border-bottom: 1px solid #c9d1da; counter-increment: h2; counter-reset: h3;
     page-break-after: avoid; }
h2::before { content: counter(h2) ". "; color: #1f3a5f; }
h3 { font-size: 12pt; color: #1b2430; margin: 6mm 0 2mm; counter-increment: h3; page-break-after: avoid; }
h3::before { content: counter(h2) "." counter(h3) " "; color: #1f3a5f; }
h4 { font-size: 10.6pt; margin: 4mm 0 1mm; page-break-after: avoid; }
h2.saut { page-break-before: always; }

p { margin: 0 0 2.6mm; text-align: justify; hyphens: auto; }
ul, ol { margin: 0 0 3mm; padding-left: 6mm; }
li { margin-bottom: 1mm; }
strong { font-weight: 600; }
a { color: #1f3a5f; }

table { border-collapse: collapse; width: 100%; margin: 2mm 0 4mm; font-size: 9.4pt;
        page-break-inside: avoid; }
th { text-align: left; font-weight: 600; font-size: 8.8pt; color: #33404d;
     border-bottom: 1.5px solid #1f3a5f; padding: 4px 6px; }
td { border-bottom: 1px solid #dde2e8; padding: 4px 6px; vertical-align: top; }
td code, p code, li code { font-size: 8.8pt; background: #eef1f4; padding: 0 3px; border-radius: 2px; }

pre { background: #f4f6f8; border-left: 3px solid #1f3a5f; padding: 3mm 4mm; font-size: 8.4pt;
      line-height: 1.45; white-space: pre-wrap; page-break-inside: avoid; margin: 2mm 0 4mm; }
pre code { background: none; padding: 0; }

figure { margin: 3mm 0 5mm; page-break-inside: avoid; text-align: center; }
figure img { max-width: 100%; max-height: 160mm; }
figcaption { font-size: 9pt; color: #55606c; margin-top: 2mm; font-style: italic; }

blockquote { margin: 3mm 0; padding: 2mm 4mm; border-left: 3px solid #9aa7b4; color: #36414d; }
blockquote p:last-child { margin-bottom: 0; }
"""


def lire_source(chemin: Path) -> tuple[dict, str]:
    texte = chemin.read_text(encoding="utf-8")
    entete, corps = {}, texte
    if texte.startswith("---"):
        _, bloc, corps = texte.split("---", 2)
        for ligne in bloc.strip().splitlines():
            cle, _, valeur = ligne.partition(":")
            entete[cle.strip()] = valeur.strip()
    return entete, corps


def en_html(entete: dict, corps: str) -> str:
    import markdown

    moteur = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "attr_list", "md_in_html"],
                               extension_configs={"toc": {"toc_depth": "2-3"}})
    contenu = moteur.convert(corps)
    # Une image seule dans un paragraphe devient une figure legendee (texte alternatif).
    contenu = re.sub(r'<p><img alt="([^"]*)" src="([^"]+)" ?/?></p>',
                     r'<figure><img src="\2" alt="\1"><figcaption>\1</figcaption></figure>', contenu)
    aujourd_hui = date.today()
    date_texte = f"{aujourd_hui.day} {MOIS[aujourd_hui.month - 1]} {aujourd_hui.year}"
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>CryptoBot, étape {entete['etape']} : {entete['titre']}</title><style>{STYLE}</style></head>
<body>
<section class="garde">
  <div>
    <div class="projet">CryptoBot · Documentation technique</div>
    <h1>Étape {entete['etape']}<br>{entete['titre']}</h1>
    <div class="sous-titre">{entete['sous_titre']}</div>
  </div>
  <table>
    <tr><td>Projet</td><td>CryptoBot, bot de trading piloté par le machine learning</td></tr>
    <tr><td>Formation</td><td>Data Engineer, DataScientest</td></tr>
    <tr><td>Dépôt</td><td>DataScientest-Studio/jul26_bde_crypto</td></tr>
    <tr><td>Date</td><td>{date_texte}</td></tr>
  </table>
</section>
<section class="sommaire"><h2>Sommaire</h2>{moteur.toc}</section>
{contenu}
</body></html>"""


def navigateur() -> str:
    for candidat in NAVIGATEURS:
        if Path(candidat).exists() or shutil.which(candidat):
            return candidat
    raise SystemExit("Aucun navigateur Edge ou Chrome trouve pour produire les PDF.")


def numeroter(pdf: Path, titre: str) -> int:
    """Pied de page sur chaque page sauf la garde : titre et numero."""
    import pymupdf

    document = pymupdf.open(pdf)
    total = len(document)
    for index, page in enumerate(document):
        if index == 0:
            continue
        largeur, hauteur = page.rect.width, page.rect.height
        page.insert_text((54, hauteur - 30), titre, fontsize=7.5, fontname="helv",
                         color=(0.42, 0.46, 0.51))
        texte = f"{index + 1} / {total}"
        page.insert_text((largeur - 54 - pymupdf.get_text_length(texte, "helv", 7.5), hauteur - 30),
                         texte, fontsize=7.5, fontname="helv", color=(0.42, 0.46, 0.51))
    temporaire = pdf.with_suffix(".tmp.pdf")
    document.save(temporaire, garbage=3, deflate=True)
    document.close()
    temporaire.replace(pdf)
    return total


def generer(etape: int) -> Path:
    source = SOURCES / f"etape{etape}.md"
    entete, corps = lire_source(source)
    html = en_html(entete, corps)
    sortie = DOSSIER / f"{entete['fichier']}.pdf"
    # Le HTML est ecrit a cote des images pour que leurs chemins relatifs marchent.
    with tempfile.NamedTemporaryFile("w", suffix=".html", dir=DOSSIER, delete=False,
                                     encoding="utf-8") as f:
        f.write(html)
        page = Path(f.name)
    try:
        subprocess.run([navigateur(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        "--virtual-time-budget=10000", f"--print-to-pdf={sortie}", page.as_uri()],
                       check=True, capture_output=True, timeout=180)
    finally:
        page.unlink(missing_ok=True)
    pages = numeroter(sortie, f"CryptoBot · Étape {etape} · {entete['titre']}")
    print(f"Étape {etape} : {sortie.relative_to(RACINE)} ({pages} pages)")
    return sortie


def main():
    parser = argparse.ArgumentParser(description="Documentation PDF des etapes")
    parser.add_argument("--etapes", nargs="+", type=int, default=[1, 2, 3, 4, 5])
    args = parser.parse_args()
    for etape in args.etapes:
        generer(etape)


if __name__ == "__main__":
    sys.exit(main())
