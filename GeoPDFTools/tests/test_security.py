"""Garde-fous de sécurité.

1. Analyse statique du code de ``geopdf_core`` : aucune fonction de
   suppression, d'insertion, de modification de géométrie ou d'écriture de
   PDF ne doit y apparaître. Si quelqu'un en ajoute une, ces tests échouent.
2. Mode « Nom du fichier » : le PDF n'est jamais ouvert.
"""

import ast
from pathlib import Path

import pytest

from conftest import make_text_pdf, pymupdf
from geopdf_core.matcher import IdentifierIndex, match_document
from geopdf_core.models import ContentState, PdfDocument, PdfStatus, SearchMode
from geopdf_core.report import result_to_row

PACKAGE = Path(__file__).resolve().parents[1] / "geopdf_core"
SOURCES = sorted(PACKAGE.glob("*.py"))

FORBIDDEN_ATTRIBUTES = {
    # Suppression / insertion d'entités ou de champs
    "deleteRow",
    "insertRow",
    "InsertCursor",
    "DeleteRows",
    "DeleteFeatures",
    "DeleteField",
    "Delete",
    "TruncateTable",
    # Modification d'autres champs ou du schéma
    "CalculateField",
    "AlterField",
    # Écriture / déplacement de fichiers (dont les PDF)
    "save",
    "saveIncr",
    "ez_save",
    "rmtree",
    "unlink",
    "rename",
    "move",
    "copyfile",
}


def attribute_calls(path):
    """Couples (objet, attribut) de toutes les expressions ``objet.attribut`` du fichier."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            owner = node.value.id if isinstance(node.value, ast.Name) else None
            yield owner, node.attr, node.lineno


def string_constants(path):
    """Chaînes de caractères du code, hors documentation (docstrings)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value, node.lineno


def test_geometry_detector_works(tmp_path):
    """Le détecteur doit repérer « SHAPE@ » dans du code (et pas seulement l'ignorer)."""
    sample = tmp_path / "exemple.py"
    sample.write_text('"""Doc : SHAPE@ autorisé ici."""\nfields = ["OID@", "SHAPE@XY"]\n', encoding="utf-8")
    assert [v for v, _ in string_constants(sample)] == ["OID@", "SHAPE@XY"]


def test_sources_found():
    names = {p.name for p in SOURCES}
    assert {"pdf_reader.py", "matcher.py", "report.py", "arcgis_utils.py", "link_writers.py"} <= names


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_destructive_call(source):
    found = [(attr, line) for _, attr, line in attribute_calls(source) if attr in FORBIDDEN_ATTRIBUTES]
    assert found == [], "Appel interdit dans {} : {}".format(source.name, found)


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_geometry_token(source):
    tokens = [(value, line) for value, line in string_constants(source) if "SHAPE@" in value.upper()]
    assert tokens == [], "Accès à la géométrie dans {} : {}".format(source.name, tokens)


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_file_removal_only_for_report_temp_file(source):
    calls = [(attr, line) for owner, attr, line in attribute_calls(source) if owner == "os" and attr in ("remove", "replace")]
    if source.name == "report.py":
        # Seulement : renommage du fichier temporaire vers le rapport, suppression du temporaire.
        assert sorted(attr for attr, _ in calls) == ["remove", "replace"]
    else:
        assert calls == []


def test_update_cursor_only_in_link_writers():
    users = sorted({p.name for p in SOURCES for _, attr, _ in attribute_calls(p) if attr == "UpdateCursor"})
    assert users == ["link_writers.py"]


def test_add_field_only_in_arcgis_utils():
    users = sorted({p.name for p in SOURCES for _, attr, _ in attribute_calls(p) if attr == "AddField"})
    assert users == ["arcgis_utils.py"]


def test_arcpy_only_imported_by_arcgis_utils():
    importers = set()
    for source in SOURCES:
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import) and any(a.name == "arcpy" for a in node.names):
                importers.add(source.name)
            if isinstance(node, ast.ImportFrom) and node.module == "arcpy":
                importers.add(source.name)
    assert importers == {"arcgis_utils.py"}


# --- Mode « Nom du fichier » : le PDF n'est pas ouvert ----------------------


def test_name_mode_never_opens_the_pdf(tmp_path, monkeypatch):
    path = make_text_pdf(tmp_path / "N57_001_rapport.pdf", ["contenu"])

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("Le PDF ne doit pas être ouvert en mode « Nom du fichier »")

    monkeypatch.setattr(pymupdf, "open", forbidden_open)
    index = IdentifierIndex([(1, "N57_001")])
    document = PdfDocument(path=path)  # document non lu : seul le chemin est connu
    result = match_document(document, index, SearchMode.NOM)

    assert result.status is PdfStatus.ASSOCIE
    assert result.content_state is ContentState.NON_ANALYSE
    row = result_to_row(result)
    assert row["contenu_pdf"] == "NON_ANALYSE"
    assert row["nombre_pages"] == ""  # inconnu, pas « 0 »
