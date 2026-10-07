"""Écriture des liens PDF dans la couche SIG.

Organisation en deux temps, pour éviter les écritures partielles :

1. **Plan** (Python pur, sans arcpy) : pour chaque entité concernée, on lit la
   valeur actuelle du champ, on calcule la valeur finale (modes ``Compléter``
   / ``Remplacer``), on vérifie sa longueur. Rien n'est écrit.
2. **Écriture** : un seul ``UpdateCursor`` ouvert sur ``["OID@", champ_resultat]``
   uniquement. Seules les entités du plan dont la valeur change sont mises à
   jour. Aucune suppression, aucune insertion, aucune géométrie.

``FieldLinkWriter`` est la stratégie V1 (champ texte). Une future
``AttachmentLinkWriter`` (pièces jointes ArcGIS) pourra offrir la même méthode
``write(links_by_oid)``.
"""

from __future__ import annotations

import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Hashable,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

from .models import LINK_SEPARATOR, PdfResult, WriteMode

# --- Capacité du champ ------------------------------------------------------


class LengthUnit(str, Enum):
    """Unité dans laquelle la capacité d'un champ texte est mesurée."""

    CHARACTERS = "caractères"
    BYTES = "octets"
    """Shapefile (fichier DBF) : la largeur est en octets ; un caractère accentué
    occupe 2 octets en UTF-8. Mesure prudente."""


@dataclass(frozen=True)
class FieldCapacity:
    """Longueur maximale d'un champ texte. ``max_length=None`` : pas de limite connue."""

    max_length: Optional[int]
    unit: LengthUnit = LengthUnit.CHARACTERS

    def measure(self, text: str) -> int:
        if self.unit is LengthUnit.BYTES:
            return len(text.encode("utf-8"))
        return len(text)

    def fits(self, text: str) -> bool:
        return self.max_length is None or self.measure(text) <= self.max_length

    def describe(self) -> str:
        if self.max_length is None:
            return "illimitée"
        return "{} {}".format(self.max_length, self.unit.value)


# --- Manipulation des listes de chemins -------------------------------------


def parse_links(value: Any) -> List[str]:
    """Découpe la valeur actuelle du champ en liste de chemins.

    NULL, chaîne vide ou espaces → liste vide. Le séparateur ``|`` est
    reconnu avec ou sans espaces autour (``|`` est interdit dans les chemins
    Windows, il ne peut donc pas couper un chemin en deux).
    """
    if value is None:
        return []
    return [part.strip() for part in str(value).split("|") if part.strip()]


def path_key(path: str) -> str:
    """Clé de comparaison d'un chemin selon les règles Windows.

    Insensible à la casse et au sens des barres obliques :
    ``C:/Rapports/A.pdf`` et ``c:\\rapports\\a.PDF`` sont le même fichier.
    """
    return unicodedata.normalize("NFC", path.strip()).replace("/", "\\").casefold()


def dedupe_links(links: Iterable[str]) -> List[str]:
    """Supprime les doublons en gardant la première écriture rencontrée."""
    seen = set()
    unique = []
    for link in links:
        key = path_key(link)
        if link.strip() and key not in seen:
            seen.add(key)
            unique.append(link.strip())
    return unique


def format_links(links: Sequence[str]) -> str:
    return LINK_SEPARATOR.join(links)


def merge_links(existing: Sequence[str], new: Sequence[str], mode: WriteMode) -> List[str]:
    """Liste finale de chemins selon le mode d'écriture.

    - ``Compléter`` : chemins existants, puis nouveaux chemins absents ; doublons supprimés ;
    - ``Remplacer`` : nouveaux chemins uniquement (doublons supprimés).
    """
    if mode is WriteMode.REMPLACER:
        return dedupe_links(new)
    return dedupe_links(list(existing) + list(new))


# --- Plan de mise à jour ----------------------------------------------------


class UpdateAction(str, Enum):
    ECRIRE = "ECRIRE"
    INCHANGE = "INCHANGE"
    """La valeur finale est identique à l'actuelle : l'entité n'est pas touchée."""
    TROP_LONG = "TROP_LONG"
    """La valeur finale dépasse la capacité du champ : l'entité n'est pas touchée."""


@dataclass
class LinkUpdate:
    """Décision pour une entité."""

    oid: Hashable
    old_value: Optional[str]
    new_value: str
    incoming: List[str]
    """Chemins proposés pour cette entité par le traitement en cours."""
    added: List[str]
    """Chemins qui n'étaient pas déjà dans le champ."""
    removed: List[str]
    """Chemins qui disparaissent (mode Remplacer, ou doublons supprimés)."""
    action: UpdateAction
    length: int
    """Longueur de ``new_value`` dans l'unité du champ."""


@dataclass
class WritePlan:
    updates: List[LinkUpdate] = field(default_factory=list)
    missing_oids: List[Hashable] = field(default_factory=list)
    """Entités prévues mais introuvables à la lecture (hors sélection, supprimées…)."""
    mode: WriteMode = WriteMode.COMPLETER
    capacity: FieldCapacity = FieldCapacity(None)

    def _with(self, action: UpdateAction) -> List[LinkUpdate]:
        return [u for u in self.updates if u.action is action]

    @property
    def to_write(self) -> List[LinkUpdate]:
        return self._with(UpdateAction.ECRIRE)

    @property
    def unchanged(self) -> List[LinkUpdate]:
        return self._with(UpdateAction.INCHANGE)

    @property
    def too_long(self) -> List[LinkUpdate]:
        return self._with(UpdateAction.TROP_LONG)


def plan_updates(
    rows: Iterable[Tuple[Hashable, Any]],
    links_by_oid: Dict[Hashable, Sequence[str]],
    mode: WriteMode,
    capacity: FieldCapacity,
) -> WritePlan:
    """Calcule, sans rien écrire, la nouvelle valeur de chaque entité concernée.

    ``rows`` : couples ``(OID, valeur actuelle du champ)`` lus dans la couche.
    Les entités absentes de ``links_by_oid`` sont ignorées : elles ne seront
    jamais modifiées, quel que soit le mode.
    """
    plan = WritePlan(mode=mode, capacity=capacity)
    seen = set()
    for oid, current in rows:
        if oid not in links_by_oid or oid in seen:
            continue
        seen.add(oid)
        incoming = dedupe_links(links_by_oid[oid])
        if not incoming:
            continue
        existing = parse_links(current)
        final = merge_links(existing, incoming, mode)
        new_value = format_links(final)
        existing_keys = {path_key(p) for p in existing}
        final_keys = {path_key(p) for p in final}
        added = [p for p in final if path_key(p) not in existing_keys]

        if [path_key(p) for p in final] == [path_key(p) for p in existing]:
            action = UpdateAction.INCHANGE
        elif not capacity.fits(new_value):
            action = UpdateAction.TROP_LONG
        else:
            action = UpdateAction.ECRIRE

        plan.updates.append(
            LinkUpdate(
                oid=oid,
                old_value=current,
                new_value=new_value,
                incoming=incoming,
                added=added,
                removed=[p for p in existing if path_key(p) not in final_keys],
                action=action,
                length=capacity.measure(new_value),
            )
        )
    plan.missing_oids = [oid for oid in links_by_oid if oid not in seen]
    return plan


# --- Résultat de l'écriture -------------------------------------------------


@dataclass
class WriteResult:
    plan: WritePlan
    written: List[LinkUpdate] = field(default_factory=list)
    conflicts: List[LinkUpdate] = field(default_factory=list)
    """Valeur modifiée par quelqu'un d'autre entre la lecture et l'écriture : non écrasée."""
    vanished: List[Hashable] = field(default_factory=list)
    """Entités du plan disparues au moment de l'écriture."""
    dry_run: bool = False
    edit_session: bool = False

    def messages(self, field_name: str = "LIEN_PDF", max_examples: int = 5) -> List[Tuple[str, str]]:
        """Messages ``(niveau, texte)`` pour ``AddMessage`` / ``AddWarning``."""
        plan = self.plan
        verb = "seraient mises à jour (simulation)" if self.dry_run else "mises à jour"
        written = plan.to_write if self.dry_run else self.written
        messages = [
            ("INFO", "Mode d'écriture : {}.".format(plan.mode.value)),
            ("INFO", "Entités {} : {}".format(verb, len(written))),
            ("INFO", "Entités déjà à jour (aucun nouveau lien) : {}".format(len(plan.unchanged))),
        ]
        if plan.too_long:
            examples = ", ".join(
                "OID {} ({} {})".format(u.oid, u.length, plan.capacity.unit.value) for u in plan.too_long[:max_examples]
            )
            messages.append(
                (
                    "WARNING",
                    "Entités NON mises à jour, valeur trop longue pour le champ {} (capacité {}) : {} — {}{}".format(
                        field_name,
                        plan.capacity.describe(),
                        len(plan.too_long),
                        examples,
                        ", …" if len(plan.too_long) > max_examples else "",
                    ),
                )
            )
        if self.conflicts:
            messages.append(
                (
                    "WARNING",
                    "Entités NON mises à jour, valeur modifiée pendant le traitement (non écrasée) : {}".format(
                        ", ".join("OID {}".format(u.oid) for u in self.conflicts)
                    ),
                )
            )
        missing = list(plan.missing_oids) + list(self.vanished)
        if missing:
            messages.append(
                (
                    "WARNING",
                    "Entités introuvables au moment de l'écriture : {}".format(
                        ", ".join("OID {}".format(oid) for oid in missing)
                    ),
                )
            )
        return messages


class WriteError(RuntimeError):
    """Échec pendant l'écriture. ``written`` liste les entités déjà modifiées."""

    def __init__(self, message: str, written: Sequence[LinkUpdate] = ()):
        super().__init__(message)
        self.written = list(written)


# --- Préparation et suivi côté PDF ------------------------------------------


def collect_links(results: Iterable[PdfResult]) -> Dict[Hashable, List[str]]:
    """Construit ``{OID: [chemins des PDF]}`` à partir des résultats associés."""
    links: Dict[Hashable, List[str]] = {}
    for result in results:
        if not result.is_associated:
            continue
        path = str(result.document.path)
        for oid in result.oids:
            links.setdefault(oid, []).append(path)
    return {oid: dedupe_links(paths) for oid, paths in links.items()}


def annotate_results(results: Iterable[PdfResult], write_result: WriteResult, field_name: str = "LIEN_PDF") -> None:
    """Ajoute aux PDF concernés un avertissement « lien NON écrit » (pour le rapport CSV)."""
    by_key: Dict[str, List[PdfResult]] = {}
    for result in results:
        by_key.setdefault(path_key(str(result.document.path)), []).append(result)

    def warn(paths: Iterable[str], message: str) -> None:
        for path in paths:
            for result in by_key.get(path_key(path), []):
                if message not in result.warnings:
                    result.warnings.append(message)

    capacity = write_result.plan.capacity
    for update in write_result.plan.too_long:
        warn(
            update.incoming,
            "Lien NON écrit pour l'entité OID {} : valeur finale de {} {} supérieure à la capacité du champ {} ({}).".format(
                update.oid, update.length, capacity.unit.value, field_name, capacity.describe()
            ),
        )
    for update in write_result.conflicts:
        warn(
            update.incoming,
            "Lien NON écrit pour l'entité OID {} : le champ {} a été modifié pendant le traitement.".format(
                update.oid, field_name
            ),
        )
    missing = set(write_result.plan.missing_oids) | set(write_result.vanished)
    for result in results:
        for oid in result.oids:
            if oid in missing:
                message = "Lien NON écrit pour l'entité OID {} : entité introuvable au moment de l'écriture.".format(oid)
                if message not in result.warnings:
                    result.warnings.append(message)


# --- Écriture dans un champ texte -------------------------------------------

CursorFactory = Callable[..., Any]


class FieldLinkWriter:
    """Écrit les chemins des PDF dans un champ texte de la couche.

    ``dataset`` est la couche ArcGIS Pro (la sélection et la requête de
    définition sont donc respectées par les curseurs) ou un chemin.

    ``search_cursor`` / ``update_cursor`` valent par défaut
    ``arcpy.da.SearchCursor`` / ``arcpy.da.UpdateCursor`` ; ils sont
    injectables pour tester la logique sans ArcGIS Pro.
    ``edit_session`` : fonction renvoyant un gestionnaire de contexte (session
    de mise à jour ``arcpy.da.Editor`` pour les géodatabases d'entreprise) ;
    par défaut aucune session.
    """

    def __init__(
        self,
        dataset: Any,
        field_name: str,
        mode: WriteMode,
        capacity: FieldCapacity,
        search_cursor: Optional[CursorFactory] = None,
        update_cursor: Optional[CursorFactory] = None,
        edit_session: Optional[Callable[[], ContextManager[Any]]] = None,
    ) -> None:
        if not field_name or not field_name.strip():
            raise ValueError("Nom du champ résultat vide.")
        self.dataset = dataset
        self.field_name = field_name
        self.mode = mode
        self.capacity = capacity
        self._search_cursor = search_cursor
        self._update_cursor = update_cursor
        self._edit_session = edit_session

    @property
    def fields(self) -> List[str]:
        """Seuls champs jamais demandés aux curseurs : l'ObjectID et le champ résultat."""
        return ["OID@", self.field_name]

    def _cursor(self, kind: str) -> CursorFactory:
        factory = self._search_cursor if kind == "search" else self._update_cursor
        if factory is None:
            from .arcgis_utils import import_arcpy

            da = import_arcpy().da
            factory = da.SearchCursor if kind == "search" else da.UpdateCursor
        return factory

    def plan(self, links_by_oid: Dict[Hashable, Sequence[str]]) -> WritePlan:
        """Lit les valeurs actuelles (lecture seule) et calcule le plan. N'écrit rien."""
        if not links_by_oid:
            return WritePlan(mode=self.mode, capacity=self.capacity)
        with self._cursor("search")(self.dataset, self.fields) as cursor:
            rows = [(row[0], row[1]) for row in cursor if row[0] in links_by_oid]
        return plan_updates(rows, links_by_oid, self.mode, self.capacity)

    def write(self, links_by_oid: Dict[Hashable, Sequence[str]], dry_run: bool = False) -> WriteResult:
        """Calcule le plan puis écrit les entités à mettre à jour.

        - ``dry_run=True`` : simulation, aucun curseur d'écriture n'est ouvert ;
        - si rien n'est à écrire, aucun curseur d'écriture n'est ouvert ;
        - une entité dont la valeur a changé depuis la lecture n'est pas écrasée.
        """
        plan = self.plan(links_by_oid)
        result = WriteResult(plan=plan, dry_run=dry_run)
        to_write = {u.oid: u for u in plan.to_write}
        if dry_run or not to_write:
            return result

        session = self._edit_session() if self._edit_session else nullcontext(False)
        try:
            with session as in_session:
                result.edit_session = bool(in_session)
                with self._cursor("update")(self.dataset, self.fields) as cursor:
                    for row in cursor:
                        update = to_write.pop(row[0], None)
                        if update is None:
                            continue
                        if (row[1] or "") != (update.old_value or ""):
                            result.conflicts.append(update)
                            continue
                        cursor.updateRow([row[0], update.new_value])
                        result.written.append(update)
        except Exception as exc:
            raise WriteError(
                "Échec de l'écriture dans le champ {} après {} entité(s) mise(s) à jour ({}). {}".format(
                    self.field_name,
                    len(result.written),
                    ", ".join("OID {}".format(u.oid) for u in result.written) or "aucune",
                    "Une session de mise à jour était active : ArcGIS doit avoir annulé ces modifications."
                    if result.edit_session
                    else "Vérifiez ces entités.",
                )
                + " Cause : {}".format(exc),
                written=result.written,
            ) from exc

        result.vanished = list(to_write)
        return result
