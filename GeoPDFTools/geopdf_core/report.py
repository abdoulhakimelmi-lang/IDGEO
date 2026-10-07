"""Rapport de contrôle CSV et résumé du traitement.

- Le CSV est écrit en UTF-8 avec BOM et séparateur ``;`` : Excel en
  français l'ouvre directement, accents compris.
- Une ligne par PDF analysé, quel que soit son statut.
- Le résumé (``summarize``) produit des lignes prêtes pour
  ``arcpy.AddMessage`` / ``arcpy.AddWarning`` (étape 5).

Ce module ne dépend ni d'arcpy ni de PyMuPDF.
"""

from __future__ import annotations

import csv
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple, Union

from .models import ContentState, PdfResult, PdfStatus

CSV_ENCODING = "utf-8-sig"
"""UTF-8 avec BOM : indispensable pour qu'Excel affiche correctement les accents."""

CSV_DELIMITER = ";"
"""Séparateur attendu par Excel avec les paramètres régionaux français."""

LIST_SEPARATOR = " | "
"""Séparateur entre plusieurs valeurs dans une même cellule."""

REPORT_COLUMNS = (
    "nom_pdf",
    "chemin_pdf",
    "statut",
    "identifiant_trouve",
    "source_detection",
    "nombre_correspondances",
    "nombre_entites",
    "nombre_pages",
    "contenu_pdf",
    "avertissement",
    "erreur",
    "detail_sources",
    "oid_entites",
)

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
"""Débuts de cellule qu'Excel interprète comme une formule."""


class ReportError(OSError):
    """Le rapport n'a pas pu être écrit (message en français)."""


# --- Lignes du rapport ------------------------------------------------------


def result_to_row(result: PdfResult) -> Dict[str, Union[str, int]]:
    """Convertit le résultat d'un PDF en ligne de rapport (clé = nom de colonne)."""
    document = result.document
    source = result.detection_source
    warnings = list(dict.fromkeys(result.warnings + document.warnings))
    return {
        "nom_pdf": document.name,
        "chemin_pdf": str(document.path),
        "statut": result.status.value,
        "identifiant_trouve": LIST_SEPARATOR.join(result.identifiers),
        "source_detection": source.value if source else "",
        "nombre_correspondances": result.match_count,
        "nombre_entites": result.entity_count,
        # Mode « Nom du fichier » : le PDF n'est pas ouvert, le nombre de pages est inconnu.
        "nombre_pages": "" if result.content_state is ContentState.NON_ANALYSE else document.page_count,
        "contenu_pdf": result.content_state.value,
        "avertissement": LIST_SEPARATOR.join(warnings),
        "erreur": document.error or "",
        "detail_sources": LIST_SEPARATOR.join(
            "{} [{}]".format(match.value, match.source.value) for match in result.matches
        ),
        "oid_entites": LIST_SEPARATOR.join(str(oid) for oid in result.oids),
    }


def _clean_cell(value: Union[str, int]) -> Union[str, int]:
    """Prépare une cellule : une seule ligne, et pas d'interprétation en formule par Excel."""
    if not isinstance(value, str):
        return value
    value = " ".join(value.splitlines())
    if value.startswith(_FORMULA_PREFIXES):
        # Une apostrophe en tête force Excel à afficher le texte tel quel
        # (protection contre l'injection de formules via un nom de fichier).
        value = "'" + value
    return value


# --- Écriture du CSV --------------------------------------------------------


def write_csv_report(results: Iterable[PdfResult], path: Union[str, "os.PathLike[str]"]) -> Path:
    """Écrit le rapport CSV et retourne son chemin absolu.

    Le fichier est d'abord écrit sous un nom temporaire puis renommé : en cas
    d'erreur, aucun rapport à moitié écrit ne remplace un rapport existant.
    Lève ``ReportError`` avec un message compréhensible en cas d'échec.
    """
    target = Path(path).expanduser().resolve()
    if not target.parent.is_dir():
        raise ReportError("Le dossier du rapport n'existe pas : {}".format(target.parent))

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding=CSV_ENCODING,
            newline="",
            dir=str(target.parent),
            prefix=".~" + target.stem + "_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            writer = csv.writer(handle, delimiter=CSV_DELIMITER, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(REPORT_COLUMNS)
            for result in results:
                row = result_to_row(result)
                writer.writerow([_clean_cell(row[column]) for column in REPORT_COLUMNS])
        os.replace(temp_name, str(target))
        temp_name = None
    except PermissionError as exc:
        raise ReportError(
            "Impossible d'écrire le rapport {} : accès refusé. "
            "Le fichier est peut-être ouvert dans Excel ({}).".format(target, exc)
        ) from exc
    except OSError as exc:
        raise ReportError("Impossible d'écrire le rapport {} : {}".format(target, exc)) from exc
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.remove(temp_name)  # uniquement notre fichier temporaire

    return target


# --- Résumé -----------------------------------------------------------------


@dataclass
class ReportSummary:
    """Comptages du traitement. Les cinq statuts forment une partition du total."""

    total: int
    by_status: Dict[PdfStatus, int]
    associated_without_content: int
    """PDF associés par le nom alors que leur contenu n'était pas exploitable."""
    entity_count: int
    """Nombre d'entités distinctes concernées par au moins un PDF."""

    def count(self, status: PdfStatus) -> int:
        return self.by_status.get(status, 0)

    def messages(self) -> List[Tuple[str, str]]:
        """Messages ``(niveau, texte)`` : ``INFO`` pour AddMessage, ``WARNING`` pour AddWarning."""

        def line(label: str, value: int, warn_if_positive: bool = False) -> Tuple[str, str]:
            level = "WARNING" if warn_if_positive and value else "INFO"
            return level, "{} : {}".format(label, value)

        result = [
            line("PDF analysés", self.total),
            line("Associés", self.count(PdfStatus.ASSOCIE)),
            line("Plusieurs correspondances", self.count(PdfStatus.PLUSIEURS_CORRESPONDANCES)),
            line("Sans correspondance", self.count(PdfStatus.AUCUNE_CORRESPONDANCE)),
            line("PDF sans texte", self.count(PdfStatus.PDF_SANS_TEXTE), warn_if_positive=True),
            line("PDF illisibles", self.count(PdfStatus.PDF_ILLISIBLE), warn_if_positive=True),
        ]
        if self.associated_without_content:
            result.append(
                line(
                    "Associés par le nom avec contenu PDF non exploitable",
                    self.associated_without_content,
                    warn_if_positive=True,
                )
            )
        result.append(line("Entités concernées", self.entity_count))
        return result

    def lines(self) -> List[str]:
        """Texte seul de chaque ligne du résumé."""
        return [text for _, text in self.messages()]

    def __str__(self) -> str:
        return "\n".join(self.lines())


def summarize(results: Sequence[PdfResult]) -> ReportSummary:
    """Calcule le résumé d'une liste de résultats."""
    counts = Counter(result.status for result in results)
    return ReportSummary(
        total=len(results),
        by_status={status: counts.get(status, 0) for status in PdfStatus},
        associated_without_content=sum(1 for r in results if r.associated_without_content),
        entity_count=len({oid for r in results for oid in r.oids}),
    )
