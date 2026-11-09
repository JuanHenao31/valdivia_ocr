"""Contraste de finca / labor leídas por el OCR contra el diccionario de Valdivia.

Las planillas son manuscritas y usan verbos ("Deshojando", "Recogiendo basura") donde el
diccionario usa sustantivos ("DESHOJE", "RECOGER BASURA"). Por eso la similitud combina:
- ratio de texto completo (difflib, sin dependencias), y
- ratio por "raíces" (primeras 5 letras de cada palabra significativa, en cualquier orden).
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.domain.labor_data import FINCAS, LABORES_GENERAL, LABORES_POR_FINCA

_STOP = {"DE", "LA", "EL", "DEL", "LOS", "LAS", "Y", "EN", "A"}
_STEM = 5


def normalize(s: str | None) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", str(s or "")) if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", s.upper()).split())


def _stems(s: str) -> str:
    return " ".join(sorted(w[:_STEM] for w in s.split() if w not in _STOP))


def similarity(a: str | None, b: str | None) -> int:
    """0-100."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0
    if na == nb:
        return 100
    full = SequenceMatcher(None, na, nb).ratio()
    # Por raíces se tope en 95: así un texto idéntico (LIMONARES) siempre gana a uno que
    # solo comparte raíz (LIMONAR).
    stem = min(SequenceMatcher(None, _stems(na), _stems(nb)).ratio(), 0.95)
    return round(100 * max(full, stem))


@dataclass
class Match:
    oficial: str | None  # mejor candidato del diccionario (None si no hay candidatos)
    score: int


def best_match(text: str | None, candidates: list[str]) -> Match:
    best = Match(None, 0)
    for c in candidates:
        sc = similarity(text, c)
        if sc > best.score:
            best = Match(c, sc)
            if sc == 100:
                break
    return best


class LaborCatalog:
    """Envuelve el diccionario; inyectable para tests."""

    def __init__(self, labores_por_finca: dict[str, list[str]] | None = None) -> None:
        self.por_finca = LABORES_POR_FINCA if labores_por_finca is None else labores_por_finca
        self.fincas = FINCAS if labores_por_finca is None else list(labores_por_finca)
        self.general = (LABORES_GENERAL if labores_por_finca is None
                        else sorted({l for ls in labores_por_finca.values() for l in ls}))

    @property
    def enabled(self) -> bool:
        return bool(self.fincas)

    def match_finca(self, text: str | None) -> Match:
        return best_match(text, self.fincas)

    def match_labor(self, finca_oficial: str | None, text: str | None) -> Match:
        """Contra las labores de la finca; si la finca no es conocida, contra todas."""
        return best_match(text, self.por_finca.get(finca_oficial or "", self.general))


DEFAULT_CATALOG = LaborCatalog()
