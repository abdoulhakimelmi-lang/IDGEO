"""Structures de données et constantes partagées par tous les modules.

Ce module ne contient aucune logique de traitement : il définit uniquement
le « vocabulaire » de l'outil (statuts, modes, résultats de lecture).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

# --- Constantes -------------------------------------------------------------

DEFAULT_RESULT_FIELD = "LIEN_PDF"
"""Nom par défaut du champ qui reçoit les chemins des PDF."""

LINK_SEPARATOR = " | "
"""Séparateur entre plusieurs chemins de PDF dans le champ résultat."""

PDF_EXTENSION = ".pdf"
"""Extension recherchée (comparaison insensible à la casse)."""


# --- Énumérations -----------------------------------------------------------


class PdfStatus(str, Enum):
    """Statut final d'un PDF, tel qu'écrit dans le rapport CSV."""

    ASSOCIE = "ASSOCIE"
    PLUSIEURS_CORRESPONDANCES = "PLUSIEURS_CORRESPONDANCES"
    AUCUNE_CORRESPONDANCE = "AUCUNE_CORRESPONDANCE"
    PDF_SANS_TEXTE = "PDF_SANS_TEXTE"
    PDF_ILLISIBLE = "PDF_ILLISIBLE"


class SearchMode(str, Enum):
    """Où chercher les identifiants. La valeur est le libellé affiché dans ArcGIS Pro."""

    NOM = "Nom du fichier"
    CONTENU = "Contenu du PDF"
    NOM_ET_CONTENU = "Nom + contenu"

    @property
    def uses_filename(self) -> bool:
        return self in (SearchMode.NOM, SearchMode.NOM_ET_CONTENU)

    @property
    def uses_content(self) -> bool:
        return self in (SearchMode.CONTENU, SearchMode.NOM_ET_CONTENU)

    @classmethod
    def labels(cls) -> List[str]:
        """Libellés pour la liste de choix du paramètre ArcGIS Pro."""
        return [mode.value for mode in cls]


class WriteMode(str, Enum):
    """Comportement d'écriture dans le champ résultat."""

    COMPLETER = "Compléter"
    REMPLACER = "Remplacer"

    @classmethod
    def labels(cls) -> List[str]:
        return [mode.value for mode in cls]


DEFAULT_SEARCH_MODE = SearchMode.NOM_ET_CONTENU
DEFAULT_WRITE_MODE = WriteMode.COMPLETER


# --- Résultat de lecture d'un PDF -------------------------------------------


@dataclass
class PdfDocument:
    """Résultat de la lecture d'un PDF par ``pdf_reader.read_pdf``.

    La lecture ne lève jamais d'exception pour un PDF défectueux : le problème
    est décrit dans ``error`` afin que le traitement continue avec les autres
    fichiers.
    """

    path: Path
    page_count: int = 0
    pages_text: List[str] = field(default_factory=list)
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Nom du fichier, avec extension (ex. ``rapport_001.pdf``)."""
        return self.path.name

    @property
    def is_readable(self) -> bool:
        return self.error is None

    @property
    def has_text(self) -> bool:
        """Vrai si au moins une page contient du texte non vide."""
        return any(text.strip() for text in self.pages_text)

    @property
    def full_text(self) -> str:
        """Texte de toutes les pages, séparées par un saut de ligne."""
        return "\n".join(self.pages_text)

    @property
    def read_status(self) -> Optional[PdfStatus]:
        """Statut imposé par la lecture seule, ou ``None`` si le texte est exploitable.

        ``None`` signifie que le statut final dépendra de la recherche
        d'identifiants (étape 2).
        """
        if not self.is_readable:
            return PdfStatus.PDF_ILLISIBLE
        if not self.has_text:
            return PdfStatus.PDF_SANS_TEXTE
        return None


# --- Résultat de la recherche d'identifiants (étape 2) ----------------------


class DetectionSource(str, Enum):
    """Où un identifiant a été trouvé (colonne ``source_detection`` du rapport)."""

    NOM = "NOM"
    CONTENU = "CONTENU"
    NOM_ET_CONTENU = "NOM+CONTENU"

    @classmethod
    def from_flags(cls, in_name: bool, in_content: bool) -> Optional["DetectionSource"]:
        if in_name and in_content:
            return cls.NOM_ET_CONTENU
        if in_name:
            return cls.NOM
        if in_content:
            return cls.CONTENU
        return None


class ContentState(str, Enum):
    """État du contenu d'un PDF (colonne ``contenu_pdf`` du rapport).

    Permet de distinguer un PDF ``ASSOCIE`` grâce à son contenu d'un PDF
    ``ASSOCIE`` uniquement grâce à son nom parce que le contenu n'a pas pu
    être exploité.
    """

    EXPLOITABLE = "EXPLOITABLE"
    ILLISIBLE = "ILLISIBLE"
    SANS_TEXTE = "SANS_TEXTE"
    NON_ANALYSE = "NON_ANALYSE"
    """Le mode de recherche « Nom du fichier » ne lit pas le contenu."""


@dataclass
class IdentifierMatch:
    """Un identifiant de la couche trouvé dans un PDF."""

    key: str
    """Forme normalisée (majuscules, Unicode normalisé) servant à la comparaison."""
    value: str
    """Valeur telle qu'elle figure dans la couche (pour l'affichage et le rapport)."""
    oids: List[int]
    """ObjectID des entités portant cet identifiant (plusieurs si doublon dans la couche)."""
    source: DetectionSource


@dataclass
class PdfResult:
    """Résultat complet pour un PDF : lecture + correspondances + statut final."""

    document: PdfDocument
    status: PdfStatus
    matches: List[IdentifierMatch] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    content_state: ContentState = ContentState.NON_ANALYSE

    @property
    def is_associated(self) -> bool:
        return self.status in (PdfStatus.ASSOCIE, PdfStatus.PLUSIEURS_CORRESPONDANCES)

    @property
    def associated_without_content(self) -> bool:
        """Associé par le nom alors que le contenu, demandé, n'était pas exploitable."""
        return self.is_associated and self.content_state in (ContentState.ILLISIBLE, ContentState.SANS_TEXTE)

    @property
    def identifiers(self) -> List[str]:
        """Identifiants trouvés (valeurs de la couche), dans l'ordre de détection."""
        return [match.value for match in self.matches]

    @property
    def match_count(self) -> int:
        """Nombre d'identifiants distincts trouvés."""
        return len(self.matches)

    @property
    def oids(self) -> List[int]:
        """ObjectID de toutes les entités à associer, sans doublon."""
        return list(dict.fromkeys(oid for match in self.matches for oid in match.oids))

    @property
    def entity_count(self) -> int:
        return len(self.oids)

    @property
    def detection_source(self) -> Optional[DetectionSource]:
        """Source globale : NOM, CONTENU, NOM+CONTENU, ou ``None`` sans correspondance."""
        in_name = any(m.source in (DetectionSource.NOM, DetectionSource.NOM_ET_CONTENU) for m in self.matches)
        in_content = any(
            m.source in (DetectionSource.CONTENU, DetectionSource.NOM_ET_CONTENU) for m in self.matches
        )
        return DetectionSource.from_flags(in_name, in_content)
