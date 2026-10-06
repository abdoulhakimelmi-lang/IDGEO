from pathlib import Path

from geopdf_core.models import (
    DEFAULT_RESULT_FIELD,
    DEFAULT_SEARCH_MODE,
    DEFAULT_WRITE_MODE,
    LINK_SEPARATOR,
    PdfDocument,
    PdfStatus,
    SearchMode,
    WriteMode,
)


def test_defaults_match_specification():
    assert DEFAULT_RESULT_FIELD == "LIEN_PDF"
    assert LINK_SEPARATOR == " | "
    assert DEFAULT_SEARCH_MODE is SearchMode.NOM_ET_CONTENU
    assert DEFAULT_WRITE_MODE is WriteMode.COMPLETER


def test_status_values_are_report_codes():
    assert [s.value for s in PdfStatus] == [
        "ASSOCIE",
        "PLUSIEURS_CORRESPONDANCES",
        "AUCUNE_CORRESPONDANCE",
        "PDF_SANS_TEXTE",
        "PDF_ILLISIBLE",
    ]


def test_search_mode_labels_and_flags():
    assert SearchMode.labels() == ["Nom du fichier", "Contenu du PDF", "Nom + contenu"]
    assert SearchMode("Nom + contenu") is SearchMode.NOM_ET_CONTENU
    assert SearchMode.NOM.uses_filename and not SearchMode.NOM.uses_content
    assert SearchMode.CONTENU.uses_content and not SearchMode.CONTENU.uses_filename
    assert SearchMode.NOM_ET_CONTENU.uses_filename and SearchMode.NOM_ET_CONTENU.uses_content


def test_write_mode_labels():
    assert WriteMode.labels() == ["Compléter", "Remplacer"]


def test_document_with_text():
    doc = PdfDocument(path=Path("/data/rapport.pdf"), page_count=2, pages_text=["", "N57_001"])
    assert doc.name == "rapport.pdf"
    assert doc.is_readable
    assert doc.has_text
    assert doc.full_text == "\nN57_001"
    assert doc.read_status is None


def test_document_without_text():
    doc = PdfDocument(path=Path("scan.pdf"), page_count=1, pages_text=["  \n "])
    assert not doc.has_text
    assert doc.read_status is PdfStatus.PDF_SANS_TEXTE


def test_document_with_error_is_unreadable_even_with_text():
    doc = PdfDocument(path=Path("x.pdf"), pages_text=["texte"], error="Fichier vide")
    assert not doc.is_readable
    assert doc.read_status is PdfStatus.PDF_ILLISIBLE
