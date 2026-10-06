"""Recherche des fichiers PDF et extraction de leur texte avec PyMuPDF.

Règles de sécurité :
- les PDF sont uniquement **lus** : aucune sauvegarde, aucun déplacement,
  aucune suppression ;
- un PDF défectueux ne doit jamais interrompre le traitement des autres :
  ``read_pdf`` ne lève pas d'exception, il décrit le problème dans
  ``PdfDocument.error``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType
from typing import List, Optional, Tuple, Union

from .dependencies import import_pymupdf
from .models import PDF_EXTENSION, PdfDocument

PathLike = Union[str, "os.PathLike[str]"]

MAX_WARNINGS_PER_PDF = 10
"""Nombre maximal d'avertissements MuPDF conservés par PDF (évite les rapports illisibles)."""


# --- Recherche des fichiers -------------------------------------------------


def find_pdf_files(folder: PathLike, errors: Optional[List[str]] = None) -> List[Path]:
    """Liste récursivement les fichiers ``.pdf`` d'un dossier.

    - l'extension est comparée sans tenir compte de la casse (``.PDF`` accepté) ;
    - les chemins retournés sont absolus et triés (ordre reproductible) ;
    - un sous-dossier inaccessible n'arrête pas la recherche : le problème est
      ajouté à ``errors`` si une liste est fournie.

    Lève ``FileNotFoundError`` / ``NotADirectoryError`` si ``folder`` n'est pas
    un dossier existant.
    """
    root = Path(folder).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError("Dossier introuvable : {}".format(root))
    if not root.is_dir():
        raise NotADirectoryError("Ce chemin n'est pas un dossier : {}".format(root))

    def on_walk_error(exc: OSError) -> None:
        if errors is not None:
            errors.append("Dossier inaccessible : {} ({})".format(exc.filename, exc.strerror))

    pdf_files = []
    for dirpath, _dirnames, filenames in os.walk(root, onerror=on_walk_error):
        for filename in filenames:
            if filename.lower().endswith(PDF_EXTENSION):
                pdf_files.append(Path(dirpath) / filename)

    return sorted(pdf_files, key=lambda p: str(p).lower())


# --- Lecture d'un PDF -------------------------------------------------------


def read_pdf(path: PathLike, max_pages: Optional[int] = None) -> PdfDocument:
    """Ouvre un PDF en lecture et extrait son texte page par page.

    ``max_pages`` limite le nombre de pages lues (``None`` = toutes).
    ``page_count`` contient toujours le nombre réel de pages du document.

    Ne lève jamais d'exception pour un PDF défectueux (seule l'absence de
    PyMuPDF lève ``DependencyError``).
    """
    pymupdf = import_pymupdf()
    document = PdfDocument(path=Path(path))
    _silence_mupdf_console(pymupdf)
    _pop_mupdf_warnings(pymupdf)  # vide les messages laissés par un PDF précédent

    try:
        # filetype="pdf" : un fichier « .pdf » qui n'est pas un PDF est refusé
        # au lieu d'être interprété comme une image ou un autre format.
        with pymupdf.open(str(document.path), filetype="pdf") as doc:
            document.page_count = doc.page_count

            if doc.needs_pass:
                document.error = "PDF protégé par mot de passe"
                return document

            document.pages_text, failed_pages = _extract_pages_text(
                doc, max_pages, document.warnings
            )
            if document.pages_text and failed_pages == len(document.pages_text):
                document.error = "Aucune page lisible"
    except Exception as exc:  # un PDF défectueux ne doit pas arrêter le lot
        document.error = describe_error(exc, pymupdf)
    finally:
        document.warnings.extend(_pop_mupdf_warnings(pymupdf))

    return document


def _extract_pages_text(
    doc, max_pages: Optional[int], warnings: List[str]
) -> Tuple[List[str], int]:
    """Texte de chaque page, et nombre de pages en erreur.

    Une page en erreur donne un texte vide et un avertissement : les autres
    pages du document restent exploitables.
    """
    limit = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
    texts = []
    failed = 0
    for index in range(limit):
        try:
            texts.append(doc.load_page(index).get_text("text"))
        except Exception as exc:
            warnings.append("Page {} illisible : {}".format(index + 1, exc))
            texts.append("")
            failed += 1
    return texts, failed


def describe_error(exc: Exception, pymupdf: Optional[ModuleType] = None) -> str:
    """Traduit une exception d'ouverture en message compréhensible pour le rapport."""
    detail = str(exc).strip() or type(exc).__name__

    empty_error = getattr(pymupdf, "EmptyFileError", None)
    data_error = getattr(pymupdf, "FileDataError", None)
    # PyMuPDF définit sa propre classe FileNotFoundError, distincte de celle de Python.
    not_found_errors = tuple(
        cls for cls in (FileNotFoundError, getattr(pymupdf, "FileNotFoundError", None))
        if isinstance(cls, type)
    )

    # EmptyFileError hérite de FileDataError : il doit être testé en premier.
    if empty_error is not None and isinstance(exc, empty_error):
        return "Fichier vide"
    if data_error is not None and isinstance(exc, data_error):
        return "Fichier corrompu ou non PDF ({})".format(detail)
    if isinstance(exc, not_found_errors):
        return "Fichier introuvable ({})".format(detail)
    if isinstance(exc, PermissionError):
        return "Accès refusé : fichier verrouillé ou droits insuffisants ({})".format(detail)
    return "{} : {}".format(type(exc).__name__, detail)


# --- Messages internes de MuPDF ---------------------------------------------


def _silence_mupdf_console(pymupdf: ModuleType) -> None:
    """Empêche MuPDF d'écrire ses messages sur la console : on les collecte nous-mêmes."""
    tools = getattr(pymupdf, "TOOLS", None)
    for name in ("mupdf_display_errors", "mupdf_display_warnings"):
        toggle = getattr(tools, name, None)
        if toggle is not None:
            toggle(False)


def _pop_mupdf_warnings(pymupdf: ModuleType) -> List[str]:
    """Récupère et efface les avertissements accumulés par MuPDF (dédoublonnés)."""
    tools = getattr(pymupdf, "TOOLS", None)
    getter = getattr(tools, "mupdf_warnings", None)
    if getter is None:
        return []
    raw = getter(reset=True) or ""
    unique = list(dict.fromkeys(line.strip() for line in raw.splitlines() if line.strip()))
    return ["MuPDF : " + line for line in unique[:MAX_WARNINGS_PER_PDF]]
