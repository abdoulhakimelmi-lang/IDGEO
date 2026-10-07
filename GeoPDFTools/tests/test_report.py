"""Tests du rapport CSV et du résumé (étape 3)."""

import csv
import hashlib
import os
from pathlib import Path

import pytest

from conftest import make_encrypted_pdf, make_image_only_pdf, make_text_pdf
from geopdf_core import report
from geopdf_core.matcher import IdentifierIndex, match_document
from geopdf_core.models import (
    ContentState,
    DetectionSource,
    IdentifierMatch,
    PdfDocument,
    PdfResult,
    PdfStatus,
    SearchMode,
)
from geopdf_core.pdf_reader import find_pdf_files, read_pdf
from geopdf_core.report import (
    CSV_DELIMITER,
    REPORT_COLUMNS,
    ReportError,
    result_to_row,
    summarize,
    write_csv_report,
)

SIG_IDS = ["N57_001", "N57_0010", "RN57-025", "A31_0042", "PR12+350", "SEC-2026-001"]


@pytest.fixture
def index():
    return IdentifierIndex(enumerate(SIG_IDS, start=1))


def doc(name, text="", error=None, pages=1, warnings=None):
    return PdfDocument(
        path=Path("C:/Rapports") / name,
        page_count=pages,
        pages_text=[text] if error is None else [],
        error=error,
        warnings=list(warnings or []),
    )


def read_back(path):
    """Relit le CSV comme le ferait Excel : UTF-8 avec BOM, séparateur « ; »."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


# --- result_to_row ----------------------------------------------------------


def test_row_for_associated_pdf(index):
    result = match_document(
        doc("rapport_001.pdf", "Section N57_001 le 15/09/2026", pages=3), index, SearchMode.NOM_ET_CONTENU
    )
    row = result_to_row(result)
    assert list(row) == list(REPORT_COLUMNS)
    assert row["nom_pdf"] == "rapport_001.pdf"
    assert row["chemin_pdf"] == str(Path("C:/Rapports/rapport_001.pdf"))
    assert row["statut"] == "ASSOCIE"
    assert row["identifiant_trouve"] == "N57_001"
    assert row["source_detection"] == "CONTENU"
    assert row["nombre_correspondances"] == 1
    assert row["nombre_entites"] == 1
    assert row["nombre_pages"] == 3
    assert row["contenu_pdf"] == "EXPLOITABLE"
    assert row["avertissement"] == ""
    assert row["erreur"] == ""
    assert row["detail_sources"] == "N57_001 [CONTENU]"
    assert row["oid_entites"] == "1"


def test_row_for_several_ids_with_mixed_sources(index):
    result = match_document(
        doc("N57_001.pdf", "Sections N57_001 et A31_0042"), index, SearchMode.NOM_ET_CONTENU
    )
    row = result_to_row(result)
    assert row["statut"] == "PLUSIEURS_CORRESPONDANCES"
    assert row["identifiant_trouve"] == "N57_001 | A31_0042"
    assert row["source_detection"] == "NOM+CONTENU"
    assert row["detail_sources"] == "N57_001 [NOM+CONTENU] | A31_0042 [CONTENU]"
    assert row["nombre_correspondances"] == 2
    assert "Plusieurs identifiants trouvés" in row["avertissement"]


def test_row_for_unreadable_pdf_associated_by_name(index):
    result = match_document(
        doc("N57_001_rapport.pdf", error="PDF protégé par mot de passe"), index, SearchMode.NOM_ET_CONTENU
    )
    row = result_to_row(result)
    assert row["statut"] == "ASSOCIE"
    assert row["source_detection"] == "NOM"
    assert row["contenu_pdf"] == "ILLISIBLE"
    assert row["erreur"] == "PDF protégé par mot de passe"
    assert "Contenu PDF non exploitable" in row["avertissement"]


def test_row_for_unreadable_pdf_without_match(index):
    row = result_to_row(match_document(doc("x.pdf", error="Fichier vide", pages=0), index, SearchMode.CONTENU))
    assert row["statut"] == "PDF_ILLISIBLE"
    assert row["identifiant_trouve"] == ""
    assert row["source_detection"] == ""
    assert row["nombre_correspondances"] == 0
    assert row["erreur"] == "Fichier vide"


def test_row_merges_reader_and_matcher_warnings_without_duplicates():
    document = doc("a.pdf", "N57_001", warnings=["MuPDF : xref réparée", "MuPDF : xref réparée"])
    result = PdfResult(document=document, status=PdfStatus.ASSOCIE, warnings=["Avertissement A"])
    assert result_to_row(result)["avertissement"] == "Avertissement A | MuPDF : xref réparée"


def test_row_lists_all_entities_of_duplicate_identifier():
    ix = IdentifierIndex([(7, "N57_001"), (9, "N57_001")])
    row = result_to_row(match_document(doc("a.pdf", "N57_001"), ix, SearchMode.CONTENU))
    assert row["nombre_correspondances"] == 1
    assert row["nombre_entites"] == 2
    assert row["oid_entites"] == "7 | 9"


# --- write_csv_report -------------------------------------------------------


def test_csv_uses_bom_and_semicolon(tmp_path, index):
    results = [match_document(doc("Été N57_001.pdf", "réalisée"), index, SearchMode.NOM_ET_CONTENU)]
    path = write_csv_report(results, tmp_path / "rapport.csv")

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM UTF-8
    first_line = raw[3:].split(b"\r\n")[0].decode("utf-8")
    assert first_line == CSV_DELIMITER.join(REPORT_COLUMNS)
    rows = read_back(path)
    assert len(rows) == 1
    assert rows[0]["nom_pdf"] == "Été N57_001.pdf"


def test_csv_returns_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = write_csv_report([], "rapport.csv")
    assert path.is_absolute()
    assert path == (tmp_path / "rapport.csv").resolve()


def test_csv_with_no_results_contains_header_only(tmp_path):
    path = write_csv_report([], tmp_path / "vide.csv")
    assert read_back(path) == []
    assert path.read_text(encoding="utf-8-sig").strip() == ";".join(REPORT_COLUMNS)


def test_csv_quotes_semicolons_and_flattens_newlines(tmp_path):
    document = doc("a;b.pdf", "x", warnings=["ligne 1\nligne 2"])
    result = PdfResult(document=document, status=PdfStatus.AUCUNE_CORRESPONDANCE)
    rows = read_back(write_csv_report([result], tmp_path / "r.csv"))
    assert rows[0]["nom_pdf"] == "a;b.pdf"  # le « ; » du nom ne décale pas les colonnes
    assert rows[0]["avertissement"] == "ligne 1 ligne 2"


def test_csv_neutralizes_excel_formulas(tmp_path):
    result = PdfResult(document=doc('=HYPERLINK("x").pdf', ""), status=PdfStatus.PDF_SANS_TEXTE)
    rows = read_back(write_csv_report([result], tmp_path / "r.csv"))
    assert rows[0]["nom_pdf"] == '\'=HYPERLINK("x").pdf'
    assert rows[0]["statut"] == "PDF_SANS_TEXTE"


def test_csv_replaces_existing_report(tmp_path):
    path = tmp_path / "rapport.csv"
    path.write_text("ancien contenu", encoding="utf-8")
    write_csv_report([], path)
    assert "ancien contenu" not in path.read_text(encoding="utf-8-sig")


def test_missing_folder_raises_clear_error(tmp_path):
    with pytest.raises(ReportError, match="n'existe pas"):
        write_csv_report([], tmp_path / "absent" / "rapport.csv")


def test_locked_report_keeps_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "rapport.csv"
    path.write_text("ancien rapport", encoding="utf-8")

    def locked(*_args):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(report.os, "replace", locked)
    with pytest.raises(ReportError, match="ouvert dans Excel"):
        write_csv_report([], path)

    assert path.read_text(encoding="utf-8") == "ancien rapport"
    assert sorted(os.listdir(tmp_path)) == ["rapport.csv"]  # aucun fichier temporaire oublié


def test_report_error_is_an_oserror():
    assert issubclass(ReportError, OSError)


# --- Résumé -----------------------------------------------------------------


def fake_results(counts, start_oid=1):
    """Fabrique des résultats ayant les statuts demandés (un OID distinct par association)."""
    results = []
    oid = start_oid
    for status, number in counts.items():
        for _ in range(number):
            matches = []
            if status in (PdfStatus.ASSOCIE, PdfStatus.PLUSIEURS_CORRESPONDANCES):
                matches = [IdentifierMatch(key="K{}".format(oid), value="K{}".format(oid), oids=[oid], source=DetectionSource.CONTENU)]
                oid += 1
            results.append(
                PdfResult(document=doc("f.pdf"), status=status, matches=matches, content_state=ContentState.EXPLOITABLE)
            )
    return results


def test_summary_matches_expected_format():
    results = fake_results(
        {
            PdfStatus.ASSOCIE: 221,
            PdfStatus.PLUSIEURS_CORRESPONDANCES: 7,
            PdfStatus.AUCUNE_CORRESPONDANCE: 19,
            PdfStatus.PDF_SANS_TEXTE: 2,
            PdfStatus.PDF_ILLISIBLE: 1,
        }
    )
    summary = summarize(results)
    assert summary.lines() == [
        "PDF analysés : 250",
        "Associés : 221",
        "Plusieurs correspondances : 7",
        "Sans correspondance : 19",
        "PDF sans texte : 2",
        "PDF illisibles : 1",
        "Entités concernées : 228",
    ]
    assert str(summary).startswith("PDF analysés : 250\nAssociés : 221")
    # Les statuts forment une partition : aucun PDF compté deux fois.
    assert sum(summary.by_status.values()) == summary.total


def test_summary_levels():
    summary = summarize(fake_results({PdfStatus.ASSOCIE: 2, PdfStatus.PDF_ILLISIBLE: 1}))
    levels = dict((text.split(" : ")[0], level) for level, text in summary.messages())
    assert levels["Associés"] == "INFO"
    assert levels["PDF illisibles"] == "WARNING"
    assert levels["PDF sans texte"] == "INFO"  # 0 : pas d'avertissement


def test_summary_of_empty_run():
    summary = summarize([])
    assert summary.total == 0
    assert summary.lines()[0] == "PDF analysés : 0"
    assert all(level == "INFO" for level, _ in summary.messages())


def test_summary_counts_associations_without_content(index):
    results = [
        match_document(doc("N57_001_rapport.pdf", error="PDF protégé par mot de passe"), index, SearchMode.NOM_ET_CONTENU),
        match_document(doc("scan_A31_0042.pdf", "  "), index, SearchMode.NOM_ET_CONTENU),
        match_document(doc("RN57-025.pdf", "RN57-025"), index, SearchMode.NOM_ET_CONTENU),
    ]
    summary = summarize(results)
    assert summary.count(PdfStatus.ASSOCIE) == 3
    assert summary.associated_without_content == 2
    assert ("WARNING", "Associés par le nom avec contenu PDF non exploitable : 2") in summary.messages()


def test_summary_counts_distinct_entities():
    ix = IdentifierIndex([(1, "N57_001"), (2, "A31_0042")])
    results = [
        match_document(doc("a.pdf", "N57_001"), ix, SearchMode.CONTENU),
        match_document(doc("b.pdf", "N57_001 et A31_0042"), ix, SearchMode.CONTENU),
    ]
    assert summarize(results).entity_count == 2  # l'entité 1 n'est comptée qu'une fois


# --- Intégration : étapes 1 + 2 + 3 -----------------------------------------


def test_end_to_end_report(tmp_path, index):
    root = tmp_path / "Rapports"
    make_text_pdf(root / "rapport_001.pdf", ["Inspection réalisée sur la section N57_001 le 15/09/2026."])
    make_text_pdf(root / "inspection_N57.pdf", ["Sections N57_0010 et RN57-025", "Page 2 : PR12+350"])
    make_text_pdf(root / "rapport_octobre.pdf", ["Aucune référence."])
    make_image_only_pdf(root / "scan.pdf")
    make_image_only_pdf(root / "scan_A31_0042.pdf")
    make_encrypted_pdf(root / "N57_001_secret.pdf")
    (root / "corrompu.pdf").write_bytes(b"pas un pdf")
    files = find_pdf_files(root)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

    results = [match_document(read_pdf(p), index, SearchMode.NOM_ET_CONTENU) for p in files]
    rows = {row["nom_pdf"]: row for row in read_back(write_csv_report(results, tmp_path / "rapport.csv"))}

    assert rows["rapport_001.pdf"]["statut"] == "ASSOCIE"
    assert rows["inspection_N57.pdf"]["statut"] == "PLUSIEURS_CORRESPONDANCES"
    assert rows["inspection_N57.pdf"]["nombre_pages"] == "2"
    assert rows["rapport_octobre.pdf"]["statut"] == "AUCUNE_CORRESPONDANCE"
    assert rows["scan.pdf"]["statut"] == "PDF_SANS_TEXTE"
    assert rows["scan_A31_0042.pdf"]["statut"] == "ASSOCIE"
    assert rows["scan_A31_0042.pdf"]["contenu_pdf"] == "SANS_TEXTE"
    assert rows["N57_001_secret.pdf"]["statut"] == "ASSOCIE"
    assert rows["N57_001_secret.pdf"]["contenu_pdf"] == "ILLISIBLE"
    assert rows["N57_001_secret.pdf"]["erreur"] == "PDF protégé par mot de passe"
    assert rows["corrompu.pdf"]["statut"] == "PDF_ILLISIBLE"

    assert summarize(results).lines()[:6] == [
        "PDF analysés : 7",
        "Associés : 3",
        "Plusieurs correspondances : 1",
        "Sans correspondance : 1",
        "PDF sans texte : 1",
        "PDF illisibles : 1",
    ]
    # Aucun PDF modifié, ajouté ou supprimé.
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in find_pdf_files(root)} == before
