"""Vérification de la présence de PyMuPDF.

PyMuPDF n'est pas installé par défaut avec ArcGIS Pro. Ce module l'importe
une seule fois et, s'il est absent, produit un message d'erreur qui explique
comment l'installer dans le bon environnement Python.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from types import ModuleType


class DependencyError(ImportError):
    """Levée quand une dépendance obligatoire est absente."""


INSTALL_HELP = (
    "Le package Python 'pymupdf' (PyMuPDF) est introuvable.\n"
    "Il doit être installé dans l'environnement Python utilisé par ArcGIS Pro :\n"
    "  {python}\n"
    "\n"
    "Procédure :\n"
    "  1. ArcGIS Pro > Paramètres > Gestionnaire de packages : cloner l'environnement\n"
    "     par défaut 'arcgispro-py3' (il est en lecture seule), puis activer le clone.\n"
    "  2. Redémarrer ArcGIS Pro.\n"
    "  3. Ouvrir l'invite 'Python Command Prompt' d'ArcGIS Pro et exécuter :\n"
    "       pip install pymupdf\n"
    "  4. Redémarrer ArcGIS Pro."
)


def _is_pymupdf(module: ModuleType) -> bool:
    # Un package PyPI sans rapport s'appelle aussi « fitz » : on vérifie
    # qu'il s'agit bien de PyMuPDF.
    return hasattr(module, "open") and hasattr(module, "Document")


@lru_cache(maxsize=1)
def import_pymupdf() -> ModuleType:
    """Retourne le module PyMuPDF ou lève ``DependencyError``.

    ``import pymupdf`` est le nom officiel depuis PyMuPDF 1.24.3. Les versions
    plus anciennes ne fournissent que ``import fitz`` : on l'accepte en repli.
    """
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        pass

    try:
        import fitz
    except ImportError:
        fitz = None

    if fitz is not None and _is_pymupdf(fitz):
        return fitz

    raise DependencyError(INSTALL_HELP.format(python=sys.executable))


def pymupdf_version() -> str:
    """Version de PyMuPDF, par exemple ``1.28.2``."""
    module = import_pymupdf()
    return getattr(module, "VersionBind", None) or getattr(module, "__version__", "inconnue")


def check_pymupdf() -> str:
    """Vérifie PyMuPDF au démarrage de l'outil.

    Retourne un message prêt pour ``arcpy.AddMessage`` ou lève
    ``DependencyError`` (dont le texte est prêt pour ``arcpy.AddError``).
    """
    return "PyMuPDF {} détecté ({}).".format(pymupdf_version(), sys.executable)
