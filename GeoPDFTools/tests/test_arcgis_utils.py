"""Tests de la partie pure d'``arcgis_utils`` (aucun appel ArcPy réel).

Les fonctions qui appellent réellement ArcPy (``describe_layer``,
``list_fields``, ``ensure_result_field``, ``edit_session``) ne sont PAS
testées ici : elles doivent l'être dans ArcGIS Pro avec
``tests_arcgis/verifier_etape4.py``.
"""

import pytest

from geopdf_core import arcgis_utils
from geopdf_core.arcgis_utils import (
    ArcPyUnavailableError,
    DataFormat,
    FieldAction,
    FieldError,
    FieldInfo,
    LayerInfo,
    check_identifier_field,
    classify_data_source,
    filter_identifier_rows,
    guess_workspace,
    parse_fid_set,
    plan_result_field,
    read_identifier_records,
    scope_messages,
)
from geopdf_core.link_writers import LengthUnit
from recording_table import RecordingTable

# --- Format des données -----------------------------------------------------


@pytest.mark.parametrize(
    "path, workspace_type, expected",
    [
        (r"C:\SIG\routes.gdb\sections_routes", None, DataFormat.FILE_GDB),
        (r"C:\SIG\routes.gdb\Reseau\sections_routes", None, DataFormat.FILE_GDB),  # jeu de classes
        (r"\\serveur\SIG\Routes.GDB\sections", None, DataFormat.FILE_GDB),
        (r"C:\SIG\sections.shp", None, DataFormat.SHAPEFILE),
        (r"C:\Conn\dir_est.sde\SIG.DIREST.SECTIONS", None, DataFormat.ENTERPRISE_GDB),
        (r"C:\SIG\x.gdb\sections", "RemoteDatabase", DataFormat.ENTERPRISE_GDB),
        ("https://services.arcgis.com/x/FeatureServer/0", None, DataFormat.FEATURE_SERVICE),
        (r"memory\sections", None, DataFormat.AUTRE),
        ("", None, DataFormat.AUTRE),
    ],
)
def test_classify_data_source(path, workspace_type, expected):
    assert classify_data_source(path, workspace_type) is expected


@pytest.mark.parametrize(
    "path, data_format, expected",
    [
        (r"C:\SIG\routes.gdb\sections", DataFormat.FILE_GDB, r"C:\SIG\routes.gdb"),
        (r"C:\SIG\routes.gdb\Reseau\sections", DataFormat.FILE_GDB, r"C:\SIG\routes.gdb"),
        (r"C:\Conn\dir_est.sde\SIG.DS\SIG.SECTIONS", DataFormat.ENTERPRISE_GDB, r"C:\Conn\dir_est.sde"),
        (r"C:\SIG\sections.shp", DataFormat.SHAPEFILE, r"C:\SIG"),
    ],
)
def test_guess_workspace(path, data_format, expected):
    assert guess_workspace(path, data_format) == expected


# --- Sélection --------------------------------------------------------------


@pytest.mark.parametrize(
    "fid_set, expected",
    [(None, []), ("", []), ("5", [5]), ("1; 5; 8", [1, 5, 8]), ("1;5;8", [1, 5, 8])],
)
def test_parse_fid_set(fid_set, expected):
    assert parse_fid_set(fid_set) == expected


def info(**kwargs):
    values = dict(
        name="sections_routes",
        catalog_path=r"C:\SIG\routes.gdb\sections_routes",
        workspace=r"C:\SIG\routes.gdb",
        data_format=DataFormat.FILE_GDB,
        oid_field="OBJECTID",
        shape_field="Shape",
        scope_count=120,
    )
    values.update(kwargs)
    return LayerInfo(**values)


def test_scope_without_selection():
    messages = scope_messages(info())
    assert messages[0] == (
        "INFO",
        "Aucune sélection : toutes les entités de la couche « sections_routes » (120 entité(s)) seront traitées.",
    )
    assert all(level == "INFO" for level, _ in messages)


def test_scope_with_selection_is_a_warning():
    messages = scope_messages(info(selection_count=12, scope_count=12))
    level, text = messages[0]
    assert level == "WARNING"
    assert "seules les 12 entité(s) sélectionnée(s)" in text
    assert "Effacez la sélection" in text


def test_scope_mentions_definition_query():
    messages = scope_messages(info(definition_query="route = 'N57'"))
    assert ("WARNING", "Requête de définition active : « route = 'N57' ». Les entités exclues par ce filtre "
            "ne sont ni lues ni modifiées.") in messages


def test_scope_warns_for_untested_formats():
    for data_format in (DataFormat.SHAPEFILE, DataFormat.ENTERPRISE_GDB, DataFormat.FEATURE_SERVICE):
        text = "\n".join(t for _, t in scope_messages(info(data_format=data_format)))
        assert "non encore validé" in text
    assert "non encore validé" not in "\n".join(t for _, t in scope_messages(info()))


def test_edit_session_needed_for_enterprise_or_versioned():
    assert not info().needs_edit_session
    assert not info(data_format=DataFormat.SHAPEFILE).needs_edit_session
    assert info(data_format=DataFormat.ENTERPRISE_GDB).needs_edit_session
    assert info(is_versioned=True).needs_edit_session


# --- Lecture des identifiants -----------------------------------------------


def test_filter_identifier_rows():
    rows = [(1, "N57_001"), (2, None), (3, ""), (4, "   "), (5, "\u00a0"), (6, 12), (7, 0), (8, " N57_002 ")]
    records = filter_identifier_rows(rows)
    assert records.records == [(1, "N57_001"), (6, 12), (7, 0), (8, " N57_002 ")]
    assert records.ignored_count == 4
    assert records.total == 8
    messages = records.messages("num_sectio")
    assert ("INFO", "8 entité(s) lue(s) dans le champ « num_sectio ».") in messages
    assert ("WARNING", "4 entité(s) ignorée(s) : identifiant NULL, vide ou composé d'espaces.") in messages


def test_read_identifier_records_requests_only_oid_and_id_field():
    table = RecordingTable(
        [
            {"OBJECTID": 1, "SHAPE": "L1", "num_sectio": "N57_001"},
            {"OBJECTID": 2, "SHAPE": "L2", "num_sectio": None},
        ]
    )
    records = read_identifier_records("couche", "num_sectio", search_cursor=table.search_cursor)
    assert table.requests == [("search", ["OID@", "num_sectio"])]
    assert records.records == [(1, "N57_001")]
    assert records.ignored_count == 1


@pytest.mark.parametrize("field_type", ["String", "Integer", "SmallInteger", "BigInteger", "GUID", "GlobalID", "OID"])
def test_identifier_field_types_accepted(field_type):
    assert check_identifier_field(FieldInfo("ID", field_type), "ID") == []


def test_identifier_field_double_gives_warning():
    (message,) = check_identifier_field(FieldInfo("ID", "Double"), "ID")
    assert message[0] == "WARNING"


@pytest.mark.parametrize("field_type", ["Date", "Geometry", "Blob", "Raster"])
def test_identifier_field_types_refused(field_type):
    with pytest.raises(FieldError):
        check_identifier_field(FieldInfo("ID", field_type), "ID")


def test_identifier_field_missing():
    with pytest.raises(FieldError, match="n'existe pas"):
        check_identifier_field(None, "num_sectio")


# --- Champ résultat ---------------------------------------------------------

PROTECTED = ["num_sectio", "OBJECTID", "Shape"]


def test_create_field_in_file_gdb():
    plan = plan_result_field("LIEN_PDF", None, True, DataFormat.FILE_GDB, PROTECTED)
    assert plan.action is FieldAction.CREER
    assert plan.length == 4000
    assert plan.capacity.unit is LengthUnit.CHARACTERS


def test_create_field_in_enterprise_gdb_is_prudent():
    plan = plan_result_field("LIEN_PDF", None, True, DataFormat.ENTERPRISE_GDB, PROTECTED)
    assert plan.length == 2000


def test_create_field_in_shapefile_warns_and_counts_bytes():
    plan = plan_result_field("LIEN_PDF", None, True, DataFormat.SHAPEFILE, PROTECTED)
    assert plan.length == 254
    assert plan.capacity.unit is LengthUnit.BYTES
    assert any(level == "WARNING" and "254 octets" in text for level, text in plan.messages)


def test_shapefile_field_name_longer_than_10_is_refused():
    with pytest.raises(FieldError, match="tronquerait"):
        plan_result_field("LIEN_PDF_RAPPORT", None, True, DataFormat.SHAPEFILE, PROTECTED)


def test_missing_field_without_permission_is_an_error():
    with pytest.raises(FieldError, match="création n'est pas autorisée"):
        plan_result_field("LIEN_PDF", None, False, DataFormat.FILE_GDB, PROTECTED)


@pytest.mark.parametrize("name", ["", "  ", "1LIEN", "LIEN PDF", "LIEN-PDF", "LIÉN"])
def test_invalid_field_names(name):
    with pytest.raises(FieldError):
        plan_result_field(name, None, True, DataFormat.FILE_GDB, PROTECTED)


@pytest.mark.parametrize("name", ["num_sectio", "NUM_SECTIO", "objectid", "Shape"])
def test_protected_fields_are_refused(name):
    with pytest.raises(FieldError, match="protégé"):
        plan_result_field(name, FieldInfo(name, "String", 50), True, DataFormat.FILE_GDB, PROTECTED)


def test_existing_text_field_is_used_with_its_capacity():
    plan = plan_result_field("lien_pdf", FieldInfo("LIEN_PDF", "String", 2000), True, DataFormat.FILE_GDB, PROTECTED)
    assert plan.action is FieldAction.UTILISER
    assert plan.name == "LIEN_PDF"  # nom réel du champ
    assert plan.length == 2000
    assert all(level == "INFO" for level, _ in plan.messages)


def test_existing_small_text_field_gives_warning():
    plan = plan_result_field("LIEN_PDF", FieldInfo("LIEN_PDF", "String", 100), True, DataFormat.FILE_GDB, PROTECTED)
    assert any(level == "WARNING" and "aucune troncature" in text for level, text in plan.messages)


def test_existing_non_text_field_is_refused():
    with pytest.raises(FieldError, match="pas de type texte"):
        plan_result_field("LIEN_PDF", FieldInfo("LIEN_PDF", "Integer"), True, DataFormat.FILE_GDB, PROTECTED)


def test_existing_read_only_field_is_refused():
    with pytest.raises(FieldError, match="pas modifiable"):
        plan_result_field(
            "LIEN_PDF", FieldInfo("LIEN_PDF", "String", 4000, editable=False), True, DataFormat.FILE_GDB, PROTECTED
        )


# --- Sans ArcGIS Pro : erreurs claires --------------------------------------


@pytest.fixture
def no_arcpy(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "arcpy", None)


def test_import_arcpy_without_arcgis(no_arcpy):
    with pytest.raises(ArcPyUnavailableError, match="ArcGIS Pro"):
        arcgis_utils.import_arcpy()


def test_arcpy_functions_fail_clearly_without_arcgis(no_arcpy):
    with pytest.raises(ArcPyUnavailableError):
        arcgis_utils.describe_layer("couche")
    with pytest.raises(ArcPyUnavailableError):
        arcgis_utils.list_fields("couche")
    with pytest.raises(ArcPyUnavailableError):
        read_identifier_records("couche", "num_sectio")


def test_edit_session_without_need_does_not_require_arcpy(no_arcpy):
    with arcgis_utils.edit_session(info()) as active:
        assert active is False


def test_modules_import_without_arcpy():
    """Le code doit se charger hors d'ArcGIS Pro (processus séparé, sans arcpy)."""
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['arcpy'] = None; "
        "import geopdf_core.arcgis_utils, geopdf_core.link_writers, geopdf_core.report"
    )
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
