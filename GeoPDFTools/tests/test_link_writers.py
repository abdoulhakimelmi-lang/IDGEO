"""Tests de l'écriture des liens (étape 4) — logique pure et contrat des curseurs."""

from contextlib import contextmanager
from pathlib import Path

import pytest

from geopdf_core.link_writers import (
    FieldCapacity,
    FieldLinkWriter,
    LengthUnit,
    UpdateAction,
    WriteError,
    WriteResult,
    annotate_results,
    collect_links,
    dedupe_links,
    format_links,
    merge_links,
    parse_links,
    path_key,
    plan_updates,
)
from geopdf_core.models import (
    ContentState,
    DetectionSource,
    IdentifierMatch,
    PdfDocument,
    PdfResult,
    PdfStatus,
    WriteMode,
)
from recording_table import RecordingTable

A = r"C:\Rapports\rapport_001.pdf"
B = r"C:\Rapports\2026\inspection_N57.pdf"
C = r"\\serveur-dir\partage\Inspections\Été 2026\N57_002 – contrôle.pdf"
D = r"C:\Rapports\N57_003.pdf"

COMPLETER = WriteMode.COMPLETER
REMPLACER = WriteMode.REMPLACER
UNLIMITED = FieldCapacity(None)
GDB = FieldCapacity(4000)


# --- parse / format / dédoublonnage -----------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, []),
        ("", []),
        ("   ", []),
        (A, [A]),
        (A + " | " + B, [A, B]),
        (A + "|" + B, [A, B]),  # sans espaces
        (" | " + A + " ||  | " + B + " | ", [A, B]),  # séparateurs en trop
        ("dossier papier", ["dossier papier"]),  # saisie manuelle conservée
    ],
)
def test_parse_links(value, expected):
    assert parse_links(value) == expected


def test_format_links_uses_separator():
    assert format_links([A, B]) == A + " | " + B
    assert format_links([]) == ""


def test_path_key_follows_windows_rules():
    assert path_key(r"C:\Rapports\A.pdf") == path_key("c:/rapports/a.PDF")
    assert path_key("C:\\Rapports\\E\u0301te\u0301.pdf") == path_key("C:\\Rapports\\Été.pdf")  # Unicode NFC
    assert path_key(r"C:\Rapports\A.pdf") != path_key(r"C:\Rapports\B.pdf")


def test_dedupe_keeps_first_spelling():
    assert dedupe_links([A, A.upper(), A.replace("\\", "/"), B, "", "  "]) == [A, B]


# --- merge_links ------------------------------------------------------------


def test_merge_completer_keeps_existing_and_adds_new():
    assert merge_links([A], [B, C], COMPLETER) == [A, B, C]


def test_merge_completer_does_not_duplicate():
    assert merge_links([A, B], [B.lower(), A, C], COMPLETER) == [A, B, C]


def test_merge_completer_cleans_existing_duplicates():
    assert merge_links([A, A], [B], COMPLETER) == [A, B]


def test_merge_remplacer_keeps_only_new():
    assert merge_links([A, B], [C, C, D], REMPLACER) == [C, D]


# --- plan_updates -----------------------------------------------------------


def test_plan_completer_with_null_existing_value():
    plan = plan_updates([(1, None)], {1: [A, B]}, COMPLETER, GDB)
    (update,) = plan.updates
    assert update.action is UpdateAction.ECRIRE
    assert update.new_value == A + " | " + B
    assert update.added == [A, B]
    assert update.removed == []


def test_plan_completer_nothing_new_is_unchanged():
    plan = plan_updates([(1, A + "|" + B)], {1: [B]}, COMPLETER, GDB)
    assert plan.updates[0].action is UpdateAction.INCHANGE
    assert plan.to_write == []


def test_plan_remplacer_same_links_is_unchanged():
    plan = plan_updates([(1, A + " | " + B)], {1: [A.lower(), B]}, REMPLACER, GDB)
    assert plan.updates[0].action is UpdateAction.INCHANGE


def test_plan_remplacer_reports_removed_links():
    plan = plan_updates([(1, A + " | " + B)], {1: [C]}, REMPLACER, GDB)
    (update,) = plan.updates
    assert update.action is UpdateAction.ECRIRE
    assert update.new_value == C
    assert update.removed == [A, B]


def test_plan_ignores_entities_without_new_links():
    rows = [(1, A), (2, None), (3, B)]
    for mode in (COMPLETER, REMPLACER):
        plan = plan_updates(rows, {2: [C]}, mode, GDB)
        assert [u.oid for u in plan.updates] == [2]


def test_plan_reports_missing_entities():
    plan = plan_updates([(1, None)], {1: [A], 99: [B]}, COMPLETER, GDB)
    assert plan.missing_oids == [99]


def test_plan_too_long_is_never_truncated():
    capacity = FieldCapacity(len(A) + 5)
    plan = plan_updates([(1, A)], {1: [B]}, COMPLETER, capacity)
    (update,) = plan.updates
    assert update.action is UpdateAction.TROP_LONG
    assert update.new_value == A + " | " + B  # valeur complète conservée pour le rapport
    assert update.length == len(update.new_value)
    assert plan.to_write == []


def test_plan_exact_capacity_fits():
    value = A + " | " + B
    plan = plan_updates([(1, None)], {1: [A, B]}, COMPLETER, FieldCapacity(len(value)))
    assert plan.updates[0].action is UpdateAction.ECRIRE
    plan = plan_updates([(1, None)], {1: [A, B]}, COMPLETER, FieldCapacity(len(value) - 1))
    assert plan.updates[0].action is UpdateAction.TROP_LONG


def test_shapefile_capacity_counts_bytes():
    accented = r"C:\Été\é.pdf"  # 12 caractères, 15 octets en UTF-8
    assert FieldCapacity(12).fits(accented)
    assert not FieldCapacity(12, LengthUnit.BYTES).fits(accented)
    assert FieldCapacity(15, LengthUnit.BYTES).fits(accented)


def test_many_pdfs_for_one_entity():
    paths = [r"C:\Rapports\rapport_{:03d}.pdf".format(i) for i in range(50)]
    plan = plan_updates([(1, None)], {1: paths}, COMPLETER, GDB)
    assert plan.updates[0].new_value.count(" | ") == 49
    assert plan.updates[0].action is UpdateAction.ECRIRE


def test_unicode_and_unc_paths_are_kept_intact():
    plan = plan_updates([(1, None)], {1: [C]}, COMPLETER, GDB)
    assert plan.updates[0].new_value == C


# --- FieldLinkWriter (contrat des curseurs) ---------------------------------


def make_table():
    return RecordingTable(
        [
            {"OBJECTID": 1, "SHAPE": "LIGNE-1", "num_sectio": "N57_001", "AUTRE": "x", "LIEN_PDF": None},
            {"OBJECTID": 2, "SHAPE": "LIGNE-2", "num_sectio": "N57_002", "AUTRE": "y", "LIEN_PDF": A},
            {"OBJECTID": 3, "SHAPE": "LIGNE-3", "num_sectio": "N57_003", "AUTRE": "z", "LIEN_PDF": "manuel.pdf"},
        ]
    )


def make_writer(table, mode=COMPLETER, capacity=GDB, **kwargs):
    return FieldLinkWriter(
        "couche",
        "LIEN_PDF",
        mode,
        capacity,
        search_cursor=table.search_cursor,
        update_cursor=table.update_cursor,
        **kwargs
    )


def test_writer_completer(tmp_path):
    table = make_table()
    result = make_writer(table).write({1: [B], 2: [B]})
    assert table.value(1, "LIEN_PDF") == B
    assert table.value(2, "LIEN_PDF") == A + " | " + B
    assert table.value(3, "LIEN_PDF") == "manuel.pdf"  # non concernée : intacte
    assert [u.oid for u in result.written] == [1, 2]


def test_writer_remplacer_does_not_empty_other_entities():
    table = make_table()
    make_writer(table, REMPLACER).write({2: [C]})
    assert table.value(1, "LIEN_PDF") is None
    assert table.value(2, "LIEN_PDF") == C
    assert table.value(3, "LIEN_PDF") == "manuel.pdf"


def test_writer_requests_only_oid_and_result_field():
    table = make_table()
    make_writer(table).write({1: [B]})
    assert table.requests == [("search", ["OID@", "LIEN_PDF"]), ("update", ["OID@", "LIEN_PDF"])]


def test_writer_never_touches_geometry_or_other_attributes():
    table = make_table()
    make_writer(table, REMPLACER).write({1: [B], 2: [C], 3: [D]})
    for before, after in zip(table.initial, table.rows):
        for name in ("OBJECTID", "SHAPE", "num_sectio", "AUTRE"):
            assert after[name] == before[name]
    assert len(table.rows) == len(table.initial)  # aucune entité supprimée ni ajoutée


def test_writer_second_run_writes_nothing():
    table = make_table()
    make_writer(table).write({1: [B]})
    table.requests.clear()
    result = make_writer(table).write({1: [B]})
    assert result.written == []
    assert [u.oid for u in result.plan.unchanged] == [1]
    assert table.requests == [("search", ["OID@", "LIEN_PDF"])]  # aucun curseur d'écriture


def test_writer_dry_run_writes_nothing():
    table = make_table()
    result = make_writer(table).write({1: [B]}, dry_run=True)
    assert [u.oid for u in result.plan.to_write] == [1]
    assert table.updates == []
    assert ("update", ["OID@", "LIEN_PDF"]) not in table.requests


def test_writer_empty_mapping_opens_no_cursor():
    table = make_table()
    make_writer(table).write({})
    assert table.requests == []


def test_writer_skips_too_long_without_truncating():
    table = make_table()
    capacity = FieldCapacity(len(B))  # B tient seul, « A | B » non
    result = make_writer(table, capacity=capacity).write({1: [B], 2: [B]})
    assert table.value(1, "LIEN_PDF") == B  # tient dans la capacité
    assert table.value(2, "LIEN_PDF") == A  # inchangée, pas « A | C:\Rap… »
    assert [u.oid for u in result.plan.too_long] == [2]
    assert [u.oid for u in result.written] == [1]


def test_writer_does_not_overwrite_concurrent_change():
    table = make_table()
    writer = make_writer(table)
    original_update_cursor = table.update_cursor

    def update_cursor_after_concurrent_edit(dataset, fields):
        table.rows[1]["LIEN_PDF"] = "modifié par un collègue"
        return original_update_cursor(dataset, fields)

    writer._update_cursor = update_cursor_after_concurrent_edit
    result = writer.write({1: [B], 2: [B]})
    assert table.value(2, "LIEN_PDF") == "modifié par un collègue"
    assert [u.oid for u in result.conflicts] == [2]
    assert [u.oid for u in result.written] == [1]


def test_writer_reports_entities_that_vanished():
    table = make_table()
    writer = make_writer(table)
    original_update_cursor = table.update_cursor

    def update_cursor_after_delete(dataset, fields):
        del table.rows[0]
        return original_update_cursor(dataset, fields)

    writer._update_cursor = update_cursor_after_delete
    result = writer.write({1: [B], 2: [B]})
    assert result.vanished == [1]
    assert any("introuvables" in text for _, text in result.messages())


def test_writer_error_reports_already_written_entities():
    table = make_table()
    table.fail_after = 1
    with pytest.raises(WriteError) as error:
        make_writer(table).write({1: [B], 2: [B], 3: [B]})
    assert [u.oid for u in error.value.written] == [1]
    assert "OID 1" in str(error.value)
    assert "Erreur simulée" in str(error.value)


def test_writer_uses_edit_session_when_given():
    table = make_table()
    events = []

    @contextmanager
    def session():
        events.append("début")
        yield True
        events.append("fin")

    result = make_writer(table, edit_session=session).write({1: [B]})
    assert events == ["début", "fin"]
    assert result.edit_session


def test_writer_error_inside_edit_session_mentions_rollback():
    table = make_table()
    table.fail_after = 0

    @contextmanager
    def session():
        yield True

    with pytest.raises(WriteError, match="session de mise à jour était active"):
        make_writer(table, edit_session=session).write({1: [B]})


def test_writer_rejects_empty_field_name():
    with pytest.raises(ValueError):
        FieldLinkWriter("couche", " ", COMPLETER, GDB)


def test_writer_fields_are_fixed():
    assert make_writer(make_table()).fields == ["OID@", "LIEN_PDF"]


# --- Messages ---------------------------------------------------------------


def test_write_messages():
    table = make_table()
    result = make_writer(table, capacity=FieldCapacity(len(B))).write({1: [B], 2: [B], 99: [C]})
    messages = result.messages()
    text = "\n".join(t for _, t in messages)
    assert "Mode d'écriture : Compléter." in text
    assert "Entités mises à jour : 1" in text
    assert "NON mises à jour, valeur trop longue" in text and "OID 2" in text
    assert "introuvables" in text and "OID 99" in text
    assert [lvl for lvl, _ in messages].count("WARNING") == 2


def test_dry_run_messages():
    table = make_table()
    result = make_writer(table).write({1: [B]}, dry_run=True)
    assert ("INFO", "Entités seraient mises à jour (simulation) : 1") in result.messages()


# --- Lien avec les résultats PDF --------------------------------------------


def pdf_result(path, oids, status=PdfStatus.ASSOCIE):
    matches = [IdentifierMatch(key="K", value="K", oids=list(oids), source=DetectionSource.CONTENU)] if oids else []
    return PdfResult(
        document=PdfDocument(path=Path(path)),
        status=status,
        matches=matches,
        content_state=ContentState.EXPLOITABLE,
    )


def test_collect_links():
    results = [
        pdf_result("/r/a.pdf", [1, 2]),
        pdf_result("/r/b.pdf", [2]),
        pdf_result("/r/c.pdf", [], PdfStatus.AUCUNE_CORRESPONDANCE),
        pdf_result("/r/d.pdf", [], PdfStatus.PDF_ILLISIBLE),
    ]
    links = collect_links(results)
    assert links == {1: [str(Path("/r/a.pdf"))], 2: [str(Path("/r/a.pdf")), str(Path("/r/b.pdf"))]}


def test_annotate_results_flags_unwritten_links():
    results = [pdf_result("/r/a.pdf", [1]), pdf_result("/r/b.pdf", [2]), pdf_result("/r/c.pdf", [99])]
    links = collect_links(results)
    plan = plan_updates([(1, None), (2, None)], links, COMPLETER, FieldCapacity(3))
    plan.updates[0].action = UpdateAction.ECRIRE  # l'entité 1 « tient »
    annotate_results(results, WriteResult(plan=plan))
    assert results[0].warnings == []
    assert any("Lien NON écrit pour l'entité OID 2" in w and "capacité" in w for w in results[1].warnings)
    assert any("OID 99" in w and "introuvable" in w for w in results[2].warnings)
