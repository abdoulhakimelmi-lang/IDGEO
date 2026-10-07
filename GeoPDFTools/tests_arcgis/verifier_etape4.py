"""Vérification de l'étape 4 dans ArcGIS Pro (À EXÉCUTER SUR UN POSTE ARCGIS PRO).

Ce script n'a PAS pu être exécuté pendant le développement (pas d'ArcGIS Pro
disponible). Il travaille uniquement sur des données de test créées dans un
dossier temporaire : vos données réelles ne sont jamais touchées.

Lancement (invite « Python Command Prompt » d'ArcGIS Pro) :

    cd C:\\chemin\\vers\\IDGEO\\GeoPDFTools
    python tests_arcgis\\verifier_etape4.py

Chaque contrôle affiche OK ou ÉCHEC ; le script se termine par un bilan.
"""

import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import arcpy  # noqa: E402

from geopdf_core.arcgis_utils import (  # noqa: E402
    DataFormat,
    FieldAction,
    FieldError,
    check_identifier_field,
    describe_layer,
    ensure_result_field,
    list_fields,
    make_field_link_writer,
    read_identifier_records,
    scope_messages,
)
from geopdf_core.models import WriteMode  # noqa: E402

RESULTS = []


def check(label, condition, detail=""):
    RESULTS.append((label, bool(condition)))
    print("{} {}{}".format("OK    " if condition else "ÉCHEC ", label, " — " + str(detail) if detail else ""))


def section(title):
    print("\n=== {} ===".format(title))


def create_test_data(folder, workspace, name):
    """Classe d'entités polyligne avec num_sectio, AUTRE ; 5 entités dont 2 identifiants vides."""
    sr = arcpy.SpatialReference(2154)
    fc = arcpy.management.CreateFeatureclass(workspace, name, "POLYLINE", spatial_reference=sr)[0]
    arcpy.management.AddField(fc, "num_sectio", "TEXT", field_length=20)
    arcpy.management.AddField(fc, "AUTRE", "TEXT", field_length=50)
    values = ["N57_001", "N57_002", None, "   ", "N57_003"]
    # InsertCursor utilisé UNIQUEMENT ici, pour fabriquer les données de test.
    with arcpy.da.InsertCursor(fc, ["SHAPE@", "num_sectio", "AUTRE"]) as cursor:
        for i, value in enumerate(values):
            line = arcpy.Polyline(
                arcpy.Array([arcpy.Point(900000 + i * 100, 6900000), arcpy.Point(900050 + i * 100, 6900080)]), sr
            )
            cursor.insertRow([line, value, "autre-{}".format(i)])
    return fc


def snapshot(fc, extra_fields=()):
    """Géométrie (WKB), identifiant et autres attributs de chaque entité, en LECTURE seule."""
    fields = ["OID@", "SHAPE@WKB", "num_sectio", "AUTRE"] + list(extra_fields)
    with arcpy.da.SearchCursor(fc, fields) as cursor:
        return {row[0]: tuple(bytes(v) if isinstance(v, (bytearray, memoryview)) else v for v in row[1:]) for row in cursor}


def field_value(fc, oid, field):
    with arcpy.da.SearchCursor(fc, ["OID@", field]) as cursor:
        for row in cursor:
            if row[0] == oid:
                return row[1]
    return "<entité absente>"


def verify_file_gdb(folder):
    section("Géodatabase fichier")
    gdb = arcpy.management.CreateFileGDB(folder, "test_geopdf.gdb")[0]
    fc = create_test_data(folder, gdb, "sections_routes")
    before = snapshot(fc)
    fields_before = [f.name for f in arcpy.ListFields(fc)]
    oids = sorted(before)

    layer = arcpy.management.MakeFeatureLayer(fc, "couche_test")[0]
    info = describe_layer(layer)
    check("Format détecté : géodatabase fichier", info.data_format is DataFormat.FILE_GDB, info.data_format)
    check("Couche reconnue comme couche", info.is_layer)
    check("Aucune sélection détectée", not info.has_selection, info.selection_count)
    check("Nombre d'entités = 5", info.scope_count == 5, info.scope_count)
    check("Pas de session de mise à jour nécessaire", not info.needs_edit_session)
    for level, text in scope_messages(info):
        print("    [{}] {}".format(level, text))

    check_identifier_field(list_fields(layer).get("NUM_SECTIO"), "num_sectio")
    records = read_identifier_records(layer, "num_sectio")
    check("3 identifiants lus, 2 ignorés (NULL et espaces)", (len(records.records), records.ignored_count) == (3, 2),
          (len(records.records), records.ignored_count))

    # Champ résultat
    try:
        ensure_result_field(layer, info, "LIEN_PDF", False, "num_sectio")
        check("Refus de créer le champ sans autorisation", False)
    except FieldError:
        check("Refus de créer le champ sans autorisation", True)
    plan = ensure_result_field(layer, info, "LIEN_PDF", True, "num_sectio")
    created = list_fields(layer).get("LIEN_PDF")
    check("Champ LIEN_PDF créé", plan.action is FieldAction.CREER and created is not None)
    check("Champ texte de 4000 caractères", created and created.type == "String" and created.length == 4000,
          created and (created.type, created.length))
    plan2 = ensure_result_field(layer, info, "LIEN_PDF", True, "num_sectio")
    check("Second appel : champ existant réutilisé", plan2.action is FieldAction.UTILISER)
    try:
        ensure_result_field(layer, info, "num_sectio", True, "num_sectio")
        check("Refus d'écrire dans le champ identifiant", False)
    except FieldError:
        check("Refus d'écrire dans le champ identifiant", True)

    a, b, c = r"C:\Rapports\a.pdf", r"\\serveur\partage\Été 2026\b – contrôle.pdf", r"C:\Rapports\c.pdf"
    o1, o2, o3 = oids[0], oids[1], oids[4]

    # Compléter
    writer = make_field_link_writer(layer, info, plan2, WriteMode.COMPLETER)
    result = writer.write({o1: [a, b], o2: [a]})
    check("Compléter : 2 entités écrites", len(result.written) == 2, [u.oid for u in result.written])
    check("Compléter : valeur OID {}".format(o1), field_value(fc, o1, "LIEN_PDF") == a + " | " + b,
          field_value(fc, o1, "LIEN_PDF"))
    result = writer.write({o1: [b, c]})
    check("Compléter : ajout sans doublon", field_value(fc, o1, "LIEN_PDF") == " | ".join([a, b, c]),
          field_value(fc, o1, "LIEN_PDF"))
    result = writer.write({o1: [a]})
    check("Compléter : relance sans nouveauté → rien écrit", result.written == [] and len(result.plan.unchanged) == 1)

    # Remplacer
    writer = make_field_link_writer(layer, info, plan2, WriteMode.REMPLACER)
    writer.write({o1: [c]})
    check("Remplacer : valeur remplacée", field_value(fc, o1, "LIEN_PDF") == c, field_value(fc, o1, "LIEN_PDF"))
    check("Remplacer : autre entité intacte", field_value(fc, o2, "LIEN_PDF") == a, field_value(fc, o2, "LIEN_PDF"))
    check("Remplacer : entité sans correspondance non vidée", field_value(fc, o3, "LIEN_PDF") is None)

    # Trop long
    long_paths = [r"C:\Rapports\{}\{}.pdf".format("x" * 200, i) for i in range(30)]
    result = writer.write({o3: long_paths})
    check("Valeur trop longue : non écrite, signalée", len(result.plan.too_long) == 1 and field_value(fc, o3, "LIEN_PDF") is None)

    # Sélection
    arcpy.management.SelectLayerByAttribute(layer, "NEW_SELECTION", "num_sectio = 'N57_002'")
    info_sel = describe_layer(layer)
    check("Sélection détectée (1 entité)", info_sel.selection_count == 1, info_sel.selection_count)
    records = read_identifier_records(layer, "num_sectio")
    check("Lecture limitée à la sélection", [v for _, v in records.records] == ["N57_002"], records.records)
    writer = make_field_link_writer(layer, info_sel, plan2, WriteMode.REMPLACER)
    result = writer.write({o1: [b]})  # o1 hors sélection
    check("Entité hors sélection jamais modifiée", field_value(fc, o1, "LIEN_PDF") == c and result.plan.missing_oids == [o1])
    arcpy.management.SelectLayerByAttribute(layer, "CLEAR_SELECTION")

    # Requête de définition (couche filtrée)
    filtered = arcpy.management.MakeFeatureLayer(fc, "couche_filtree", "num_sectio <> 'N57_001'")[0]
    info_dq = describe_layer(filtered)
    check("Requête de définition détectée", bool(info_dq.definition_query), info_dq.definition_query)
    records = read_identifier_records(filtered, "num_sectio")
    check("Lecture limitée par le filtre", sorted(v for _, v in records.records) == ["N57_002", "N57_003"], records.records)

    # Sécurité
    after = snapshot(fc)
    check("Aucune entité supprimée ni ajoutée", sorted(after) == oids)
    check("Géométries, identifiants et autres attributs identiques", after == before)
    fields_after = [f.name for f in arcpy.ListFields(fc)]
    check("Seul le champ LIEN_PDF a été ajouté", fields_after == fields_before + ["LIEN_PDF"], fields_after)


def verify_shapefile(folder):
    section("Shapefile")
    fc = create_test_data(folder, folder, "sections_shp.shp")
    layer = arcpy.management.MakeFeatureLayer(fc, "couche_shp")[0]
    info = describe_layer(layer)
    check("Format détecté : shapefile", info.data_format is DataFormat.SHAPEFILE, info.data_format)
    try:
        ensure_result_field(layer, info, "LIEN_PDF_RAPPORT", True, "num_sectio")
        check("Nom > 10 caractères refusé", False)
    except FieldError:
        check("Nom > 10 caractères refusé", True)
    plan = ensure_result_field(layer, info, "LIEN_PDF", True, "num_sectio")
    created = list_fields(layer).get("LIEN_PDF")
    check("Champ de 254 créé avec avertissement",
          created is not None and created.length == 254 and any(lvl == "WARNING" for lvl, _ in plan.messages),
          created and created.length)
    oid = sorted(snapshot(fc))[0]
    writer = make_field_link_writer(layer, info, plan, WriteMode.COMPLETER)
    paths = [r"C:\Rapports\Été\rapport_{:02d}_inspection_section.pdf".format(i) for i in range(10)]
    result = writer.write({oid: paths})
    check("Shapefile : liste trop longue non écrite (pas de troncature)",
          len(result.plan.too_long) == 1 and field_value(fc, oid, "LIEN_PDF") in (None, "", " "),
          repr(field_value(fc, oid, "LIEN_PDF")))


def main():
    folder = tempfile.mkdtemp(prefix="geopdf_test_")
    print("Données de test : {}".format(folder))
    print("ArcGIS Pro {} — licence {}".format(arcpy.GetInstallInfo().get("Version"), arcpy.ProductInfo()))
    for verify in (verify_file_gdb, verify_shapefile):
        try:
            verify(folder)
        except Exception:
            traceback.print_exc()
            check("{} : exécution sans erreur".format(verify.__name__), False)
    failed = [label for label, ok in RESULTS if not ok]
    print("\nBilan : {} contrôle(s), {} échec(s).".format(len(RESULTS), len(failed)))
    for label in failed:
        print("  - " + label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
