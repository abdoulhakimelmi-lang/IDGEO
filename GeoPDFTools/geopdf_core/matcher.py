"""Correspondance entre les identifiants de la couche SIG et les PDF.

Principe :

1. **Normalisation** : le texte des PDF, les noms de fichiers et les
   identifiants passent par la même fonction ``normalize_text`` (Unicode NFKC,
   espaces insécables, tirets typographiques, caractères invisibles,
   majuscules). La comparaison devient ainsi insensible à la casse et aux
   variantes Unicode.
2. **Index** : ``IdentifierIndex`` charge une seule fois tous les
   identifiants (``{clé normalisée: [OID, …]}``) et compile **une seule**
   expression régulière en forme d'arbre (trie). Le texte d'un PDF est
   parcouru une seule fois, quel que soit le nombre d'identifiants.
3. **Bornes** : un identifiant n'est accepté que s'il n'est pas collé à une
   lettre ou un chiffre (``N57_001`` n'est pas trouvé dans ``N57_0010``), et,
   s'il commence ou finit par un chiffre, s'il ne prolonge pas une suite
   numérique (``PR12`` n'est pas trouvé dans ``PR12+350``, ``N57_001`` n'est
   pas trouvé dans ``N57_001,5``).
4. **Identifiants à risque** : les identifiants courts ou numériques courts
   (``12``, ``001``, ``2026``) sont cherchés uniquement dans le nom des
   fichiers, sauf autorisation explicite, pour éviter les faux positifs.

Ce module ne dépend ni d'arcpy ni de PyMuPDF.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Iterable, List, Optional, Pattern, Sequence, Tuple

from .models import DetectionSource, IdentifierMatch, PdfDocument, PdfResult, PdfStatus, SearchMode

# --- Paramètres de prudence -------------------------------------------------

MIN_ID_LENGTH = 4
"""En dessous de ce nombre de lettres/chiffres, un identifiant est jugé « court »."""

MIN_NUMERIC_ID_LENGTH = 6
"""Un identifiant purement numérique de moins de chiffres que cela est jugé à risque."""

RISK_SHORT = "COURT"
RISK_NUMERIC = "NUMERIQUE"
RISK_NO_DIGIT = "SANS_CHIFFRE"
RISKS_EXCLUDED_FROM_CONTENT = frozenset({RISK_SHORT, RISK_NUMERIC})
"""Risques qui excluent l'identifiant de la recherche dans le contenu (sauf autorisation)."""

# --- Normalisation ----------------------------------------------------------

_INVISIBLE_CHARS = "­​‌‍⁠﻿"
"""Trait d'union conditionnel, espaces de largeur nulle, BOM : supprimés."""

_DASH_CHARS = "‐‑‒–—―−﹘﹣－"
"""Tirets typographiques (‐ ‑ ‒ – — ― −…) : remplacés par le tiret simple ``-``."""

_TRANSLATION = {ord(c): None for c in _INVISIBLE_CHARS}
_TRANSLATION.update({ord(c): "-" for c in _DASH_CHARS})

_WHITESPACE_RUN = re.compile(r"\s+")

SEPARATOR_CHARS = "_-/+."
"""Séparateurs internes reconnus en mode tolérant (en plus de l'espace)."""

_SEPARATOR_RUN = re.compile(r"[\s_\-/+.]+")

# Bornes utilisées dans l'expression régulière.
_NOT_AFTER_ALNUM = r"(?<![^\W_])"  # caractère précédent : pas une lettre ni un chiffre
_NOT_BEFORE_ALNUM = r"(?![^\W_])"  # caractère suivant : pas une lettre ni un chiffre
_CHAIN_SEPARATORS = r"[_\-/.,+:]"
_NOT_AFTER_NUMBER_CHAIN = r"(?<!\d" + _CHAIN_SEPARATORS + r")"  # ex. refuse « 7_ » devant « 001 »
_NOT_BEFORE_NUMBER_CHAIN = r"(?!" + _CHAIN_SEPARATORS + r"\d)"  # ex. refuse « ,5 » ou « +350 » après
_TOLERANT_SEPARATOR = r"(?:\s?[_\-/+.]\s?|\s)"


def normalize_text(text: str) -> str:
    """Forme canonique d'un texte pour la comparaison.

    - NFKC : ligatures (« ﬁ » → « fi »), caractères pleine chasse
      (« Ｎ５７ » → « N57 »), espaces insécables → espace ;
    - suppression des caractères invisibles (trait d'union conditionnel…) ;
    - tirets typographiques → ``-`` ;
    - toute suite d'espaces, tabulations, retours à la ligne → un espace ;
    - majuscules (recherche insensible à la casse).
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATION)
    return _WHITESPACE_RUN.sub(" ", text).upper()


def normalize_identifier(value: Any) -> str:
    """Normalise une valeur lue dans la couche ; ``""`` si elle est vide ou nulle.

    Les nombres entiers stockés en réel (``12.0``) deviennent ``"12"``.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value.is_integer():
            value = int(value)
    return normalize_text(str(value)).strip()


def canonical_key(normalized: str, tolerant_separators: bool = False) -> str:
    """Clé de comparaison d'un identifiant déjà normalisé.

    En mode tolérant, toute suite de séparateurs (``_ - / + .`` et espaces)
    devient ``_`` : ``N57-002``, ``N57 _002`` et ``N57_002`` ont la même clé.
    """
    if not tolerant_separators:
        return normalized
    return _SEPARATOR_RUN.sub("_", normalized).strip("_")


def classify_risk(key: str) -> List[str]:
    """Raisons pour lesquelles un identifiant risque de produire des faux positifs."""
    significant = [c for c in key if c.isalnum()]
    reasons = []
    if len(significant) < MIN_ID_LENGTH:
        reasons.append(RISK_SHORT)
    if significant and all(c.isdigit() for c in significant) and len(significant) < MIN_NUMERIC_ID_LENGTH:
        reasons.append(RISK_NUMERIC)
    if significant and not any(c.isdigit() for c in significant):
        reasons.append(RISK_NO_DIGIT)
    return reasons


# --- Index des identifiants -------------------------------------------------


@dataclass
class IdentifierEntry:
    """Un identifiant de la couche et toutes les entités qui le portent."""

    key: str
    value: str
    oids: List[Hashable] = field(default_factory=list)
    variants: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)

    @property
    def is_duplicate(self) -> bool:
        return len(self.oids) > 1


class IdentifierIndex:
    """Identifiants de la couche, chargés une fois, et moteur de recherche compilé.

    ``records`` est un itérable de couples ``(oid, valeur)``, par exemple
    produit plus tard par un ``arcpy.da.SearchCursor``.
    """

    def __init__(
        self,
        records: Iterable[Tuple[Hashable, Any]],
        tolerant_separators: bool = False,
        allow_risky_in_content: bool = False,
    ) -> None:
        self.tolerant_separators = tolerant_separators
        self.allow_risky_in_content = allow_risky_in_content
        self.entries: Dict[str, IdentifierEntry] = {}
        self.ignored_count = 0

        for oid, raw_value in records:
            normalized = normalize_identifier(raw_value)
            key = canonical_key(normalized, tolerant_separators)
            if not key:
                self.ignored_count += 1
                continue
            display = str(raw_value).strip() if not isinstance(raw_value, float) else normalized
            entry = self.entries.get(key)
            if entry is None:
                entry = IdentifierEntry(key=key, value=display, risks=classify_risk(key))
                self.entries[key] = entry
            if display not in entry.variants:
                entry.variants.append(display)
            entry.oids.append(oid)

        content_keys = [k for k, e in self.entries.items() if not self._excluded_from_content(e)]
        self._name_pattern = _compile_trie(list(self.entries), tolerant_separators)
        self._content_pattern = _compile_trie(content_keys, tolerant_separators)

    # -- Informations ----------------------------------------------------

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, key: str) -> Optional[IdentifierEntry]:
        return self.entries.get(key)

    @property
    def duplicates(self) -> List[IdentifierEntry]:
        """Identifiants portés par plusieurs entités."""
        return [e for e in self.entries.values() if e.is_duplicate]

    @property
    def risky(self) -> List[IdentifierEntry]:
        return [e for e in self.entries.values() if e.risks]

    @property
    def excluded_from_content(self) -> List[IdentifierEntry]:
        """Identifiants cherchés uniquement dans le nom des fichiers."""
        return [e for e in self.entries.values() if self._excluded_from_content(e)]

    def _excluded_from_content(self, entry: IdentifierEntry) -> bool:
        return not self.allow_risky_in_content and bool(RISKS_EXCLUDED_FROM_CONTENT & set(entry.risks))

    def summary_messages(self, max_examples: int = 5) -> List[Tuple[str, str]]:
        """Messages ``(niveau, texte)`` à afficher dans ArcGIS Pro (``INFO`` / ``WARNING``)."""

        def examples(entries: Sequence[IdentifierEntry]) -> str:
            shown = ", ".join(e.value for e in entries[:max_examples])
            return shown + (", …" if len(entries) > max_examples else "")

        messages = [("INFO", "{} identifiant(s) distinct(s) chargé(s).".format(len(self)))]
        if self.ignored_count:
            messages.append(("WARNING", "{} valeur(s) vide(s) ou nulle(s) ignorée(s).".format(self.ignored_count)))
        if self.duplicates:
            messages.append(
                (
                    "WARNING",
                    "{} identifiant(s) présent(s) sur plusieurs entités : {}. "
                    "Le PDF sera associé à toutes ces entités.".format(len(self.duplicates), examples(self.duplicates)),
                )
            )
        excluded = self.excluded_from_content
        if excluded:
            messages.append(
                (
                    "WARNING",
                    "{} identifiant(s) court(s) ou numérique(s) cherché(s) uniquement dans le nom "
                    "des fichiers (risque de faux positifs dans le texte) : {}.".format(len(excluded), examples(excluded)),
                )
            )
        no_digit = [e for e in self.entries.values() if RISK_NO_DIGIT in e.risks and not self._excluded_from_content(e)]
        if no_digit:
            messages.append(
                (
                    "WARNING",
                    "{} identifiant(s) sans chiffre, qui peuvent correspondre à des mots courants : {}.".format(
                        len(no_digit), examples(no_digit)
                    ),
                )
            )
        return messages

    # -- Recherche -------------------------------------------------------

    def find(self, text: str, in_content: bool = True) -> List[str]:
        """Clés des identifiants présents dans ``text``, dans l'ordre d'apparition, sans doublon.

        ``in_content=False`` (recherche dans un nom de fichier) inclut aussi
        les identifiants à risque.
        """
        pattern = self._content_pattern if in_content else self._name_pattern
        if pattern is None or not text:
            return []
        found: Dict[str, None] = {}
        for match in pattern.finditer(normalize_text(text)):
            key = canonical_key(match.group(0), self.tolerant_separators)
            if key in self.entries:
                found.setdefault(key, None)
        return list(found)


# --- Construction de l'expression régulière en arbre ------------------------

_END = ""  # marqueur de fin d'identifiant dans l'arbre (aucun atome n'est vide)
_SEP = None  # atome « séparateur » en mode tolérant


def _atoms(key: str, tolerant: bool) -> List[Optional[str]]:
    """Découpe une clé en atomes : un caractère, ou ``_SEP`` en mode tolérant."""
    if tolerant:
        return [_SEP if c == "_" else c for c in key]
    return list(key)


def _compile_trie(keys: Sequence[str], tolerant: bool) -> Optional[Pattern[str]]:
    """Compile une expression régulière unique reconnaissant toutes les clés.

    Les identifiants partageant un début commun sont factorisés
    (``N57_00(?:1|2|3)``) : le moteur n'essaie pas chaque identifiant un par
    un. À chaque nœud, les prolongements sont essayés avant la fin
    d'identifiant : l'identifiant le plus long l'emporte (``N57_001_A`` plutôt
    que ``N57_001`` si les deux existent).
    """
    if not keys:
        return None
    trie: Dict[Any, Any] = {}
    for key in keys:
        node = trie
        for atom in _atoms(key, tolerant):
            node = node.setdefault(atom, {})
        node[_END] = True

    branches = []
    for atom in sorted(trie, key=_atom_sort_key):
        start = _NOT_AFTER_ALNUM + (_NOT_AFTER_NUMBER_CHAIN if atom and atom.isdigit() else "")
        branches.append(start + _atom_regex(atom) + _node_regex(trie[atom], atom))
    return re.compile(_alternation(branches))


def _node_regex(node: Dict[Any, Any], last_atom: Optional[str]) -> str:
    branches = [
        _atom_regex(atom) + _node_regex(child, atom)
        for atom, child in sorted(((a, c) for a, c in node.items() if a != _END), key=lambda ac: _atom_sort_key(ac[0]))
    ]
    if _END in node:  # en dernier : la fin n'est essayée que si aucun prolongement ne convient
        end = _NOT_BEFORE_ALNUM + (_NOT_BEFORE_NUMBER_CHAIN if last_atom and last_atom.isdigit() else "")
        branches.append(end)
    return _alternation(branches)


def _alternation(branches: List[str]) -> str:
    return branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"


def _atom_regex(atom: Optional[str]) -> str:
    return _TOLERANT_SEPARATOR if atom is _SEP else re.escape(atom)


def _atom_sort_key(atom: Optional[str]) -> str:
    return "" if atom is _SEP else atom


# --- Correspondance d'un PDF ------------------------------------------------


def filename_text(document: PdfDocument) -> str:
    """Texte utilisé pour la recherche dans le nom : nom du fichier sans ``.pdf``."""
    return document.path.stem


def match_document(document: PdfDocument, index: IdentifierIndex, mode: SearchMode) -> PdfResult:
    """Cherche les identifiants dans un PDF déjà lu et détermine son statut.

    Statut :
    - au moins un identifiant : ``ASSOCIE`` (1) ou ``PLUSIEURS_CORRESPONDANCES`` (≥ 2),
      même si le contenu est illisible mais que le **nom** a permis l'association
      (l'erreur de lecture reste dans le rapport) ;
    - aucun identifiant : ``PDF_ILLISIBLE`` / ``PDF_SANS_TEXTE`` si le contenu
      devait être lu et n'a pas pu l'être, sinon ``AUCUNE_CORRESPONDANCE``.
    """
    in_name = index.find(filename_text(document), in_content=False) if mode.uses_filename else []
    in_content = []
    if mode.uses_content and document.read_status is None:
        in_content = index.find(document.full_text, in_content=True)

    name_keys = set(in_name)
    content_keys = set(in_content)
    matches = []
    warnings = []
    for key in dict.fromkeys(in_name + in_content):  # union sans doublon, ordre conservé
        entry = index.entries[key]
        source = DetectionSource.from_flags(key in name_keys, key in content_keys)
        matches.append(IdentifierMatch(key=key, value=entry.value, oids=list(entry.oids), source=source))
        if entry.is_duplicate:
            warnings.append(
                "Identifiant {} présent sur {} entités : le PDF est associé à toutes.".format(
                    entry.value, len(entry.oids)
                )
            )

    if len(matches) > 1:
        status = PdfStatus.PLUSIEURS_CORRESPONDANCES
        warnings.append("Plusieurs identifiants trouvés : {}.".format(", ".join(m.value for m in matches)))
    elif matches:
        status = PdfStatus.ASSOCIE
    elif mode.uses_content and document.read_status is not None:
        status = document.read_status
    else:
        status = PdfStatus.AUCUNE_CORRESPONDANCE

    return PdfResult(document=document, status=status, matches=matches, warnings=warnings)
