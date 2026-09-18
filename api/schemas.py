"""Formats d'entree et de sortie de l'API.

Pydantic valide les requetes AVANT qu'elles n'atteignent le code : une paire
inconnue ou un style mal orthographie est refuse avec un message clair, et
c'est aussi ce qui alimente la documentation automatique sur /docs.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

INTERVALLES = Literal["15m", "1h", "4h"]
STYLES = Literal["agressif", "conservateur"]


class DemandeDePrediction(BaseModel):
    symbole: str = Field(..., examples=["BTCUSDT"], min_length=5, max_length=20)
    interval: INTERVALLES = "1h"
    # Le bouton du produit final : conservateur = peu d'ordres mais plus surs.
    style: STYLES = "conservateur"


class Prediction(BaseModel):
    symbole: str
    interval: str
    style: str
    bougie: str
    probabilite_hausse: float
    seuil_du_style: float
    decision: Literal["acheter", "vendre", "attendre"]
    sens: int
    avertissement: str


class Sante(BaseModel):
    statut: str
    modele: str
    bases: dict[str, str]
    version: str


class VariableEnDerive(BaseModel):
    variable: str
    psi: float | None
    statut: str


class Derive(BaseModel):
    reference: dict
    bougies_analysees: int
    psi_median: float | None
    psi_maximum: float | None
    variables_en_derive_forte: int
    variables_en_derive_moderee: int
    verdict: str
    detail: list[VariableEnDerive]
