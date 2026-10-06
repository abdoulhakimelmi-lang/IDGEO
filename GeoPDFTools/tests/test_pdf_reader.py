import hashlib
from pathlib import Path

import pytest

from conftest import make_encrypted_pdf, make_image_only_pdf, make_text_pdf, pymupdf
from geopdf_core.models import PdfStatus
from geopdf_core.pdf_reader import _extract_pages_text, describe_error, find_pdf_files, read_pdf

# --- find_pdf_files ---------------------------------------------------------


def test_find_pdf_files_is_recursive_and_case_insensitive(pdf_folder):
    names = [p.name for p in find_pdf_files(pdf_folder)]
    assert sorted(names) == sorted(["rapport_001.pdf", "inspection_N57.PDF", "Été été.pdf"])


def test_find_pdf_files_ignores_other_files_and_folders(pdf_folder):
    names = {p.name for p in find_pdf_files(pdf_folder)}
    assert "notes.txt" not in names
    assert "ancien.pdf.bak" not in names
    assert "dossier.pdf" not in names


def test_find_pdf_files_returns_sorted_absolute_paths(pdf_folder):
    files = find_pdf_files(pdf_folder)
    assert all(p.is_absolute() for p in files)
    assert files == sorted(files, key=lambda p: str(p).lower())


def test_find_pdf_files_empty_folder(tmp_path):
    assert find_pdf_files(tmp_path) == []


def test_find_pdf_files_rejects_missing_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_pdf_files(tmp_path / "absent")


def test_find_pdf_files_rejects_a_file(tmp_path):
    file_path = make_text_pdf(tmp_path / "a.pdf", ["x"])
    with pytest.raises(NotADirectoryError):
        find_pdf_files(file_path)


# --- read_pdf : cas valides -------------------------------------------------


def test_read_single_page_pdf(tmp_path):
    path = make_text_pdf(
        tmp_path / "rapport_001.pdf",
        ["Inspection réalisée sur la section N57_002 le 15/09/2026."],
    )
    doc = read_pdf(path)

    assert doc.error is None
    assert doc.name == "rapport_001.pdf"
    assert doc.page_count == 1
    assert "N57_002" in doc.full_text
    assert "réalisée" in doc.full_text  # les accents sont conservés
    assert doc.read_status is None


def test_read_multi_page_pdf_keeps_page_order(tmp_path):
    path = make_text_pdf(tmp_path / "multi.pdf", ["Page A N57_001", "", "Page C N57_003"])
    doc = read_pdf(path)

    assert doc.page_count == 3
    assert len(doc.pages_text) == 3
    assert "N57_001" in doc.pages_text[0]
    assert doc.pages_text[1].strip() == ""
    assert "N57_003" in doc.pages_text[2]


def test_read_pdf_max_pages(tmp_path):
    path = make_text_pdf(tmp_path / "long.pdf", ["p1", "p2", "p3", "p4"])
    doc = read_pdf(path, max_pages=2)

    assert doc.page_count == 4  # nombre réel de pages
    assert len(doc.pages_text) == 2  # pages effectivement lues


def test_read_pdf_accepts_str_path_and_accents(pdf_folder):
    path = pdf_folder / "2026" / "Été été.pdf"
    doc = read_pdf(str(path))
    assert doc.error is None
    assert doc.name == "Été été.pdf"


# --- read_pdf : cas problématiques -------------------------------------------


def test_image_only_pdf_has_no_text(tmp_path):
    doc = read_pdf(make_image_only_pdf(tmp_path / "scan.pdf"))
    assert doc.error is None
    assert doc.page_count == 1
    assert not doc.has_text
    assert doc.read_status is PdfStatus.PDF_SANS_TEXTE


def test_encrypted_pdf_is_unreadable(tmp_path):
    doc = read_pdf(make_encrypted_pdf(tmp_path / "secret.pdf"))
    assert doc.error == "PDF protégé par mot de passe"
    assert doc.page_count == 1
    assert doc.pages_text == []
    assert doc.read_status is PdfStatus.PDF_ILLISIBLE


def test_corrupted_pdf_is_unreadable(tmp_path):
    path = tmp_path / "corrompu.pdf"
    path.write_bytes(b"Ceci n'est pas un PDF")
    doc = read_pdf(path)
    assert doc.error.startswith("Fichier corrompu ou non PDF")
    assert doc.read_status is PdfStatus.PDF_ILLISIBLE


def test_truncated_pdf_does_not_crash(tmp_path):
    good = make_text_pdf(tmp_path / "ok.pdf", ["Section N57_001"])
    truncated = tmp_path / "tronque.pdf"
    truncated.write_bytes(good.read_bytes()[:200])

    doc = read_pdf(truncated)  # ne doit pas lever d'exception
    assert doc.read_status in (PdfStatus.PDF_ILLISIBLE, PdfStatus.PDF_SANS_TEXTE)


def test_empty_file_is_unreadable(tmp_path):
    path = tmp_path / "vide.pdf"
    path.write_bytes(b"")
    doc = read_pdf(path)
    assert doc.error == "Fichier vide"
    assert doc.read_status is PdfStatus.PDF_ILLISIBLE


def test_missing_file_is_unreadable(tmp_path):
    doc = read_pdf(tmp_path / "absent.pdf")
    assert doc.read_status is PdfStatus.PDF_ILLISIBLE
    assert doc.error.startswith("Fichier introuvable")


def test_batch_continues_after_bad_pdf(tmp_path):
    bad = tmp_path / "a_corrompu.pdf"
    bad.write_bytes(b"%PDF-1.7 garbage")
    good = make_text_pdf(tmp_path / "b_ok.pdf", ["Section N57_001"])

    results = [read_pdf(p) for p in find_pdf_files(tmp_path)]
    assert [r.path.name for r in results] == ["a_corrompu.pdf", "b_ok.pdf"]
    assert results[0].read_status is PdfStatus.PDF_ILLISIBLE
    assert "N57_001" in results[1].full_text
    assert good.exists()


# --- Sécurité : les PDF ne sont jamais modifiés -----------------------------


def test_reading_never_modifies_pdf_files(pdf_folder, tmp_path):
    corrupted = pdf_folder / "corrompu.pdf"
    corrupted.write_bytes(b"pas un pdf")
    encrypted = make_encrypted_pdf(pdf_folder / "secret.pdf")
    files = find_pdf_files(pdf_folder)
    assert corrupted in files and encrypted in files

    def snapshot():
        return {
            p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in files
        }

    before = snapshot()
    for path in files:
        read_pdf(path)
    assert snapshot() == before
    assert sorted(find_pdf_files(pdf_folder)) == sorted(files)  # rien ajouté ni supprimé


# --- Détails internes -------------------------------------------------------


class _BrokenPage:
    def get_text(self, _mode):
        raise RuntimeError("contenu de page invalide")


class _TextPage:
    def __init__(self, text):
        self._text = text

    def get_text(self, _mode):
        return self._text


class _FakeDoc:
    def __init__(self, pages):
        self._pages = pages
        self.page_count = len(pages)

    def load_page(self, index):
        return self._pages[index]


def test_broken_page_does_not_lose_other_pages():
    warnings = []
    texts, failed = _extract_pages_text(
        _FakeDoc([_TextPage("N57_001"), _BrokenPage(), _TextPage("N57_003")]), None, warnings
    )
    assert texts == ["N57_001", "", "N57_003"]
    assert failed == 1
    assert warnings == ["Page 2 illisible : contenu de page invalide"]


def test_all_pages_broken_is_detected(tmp_path, monkeypatch):
    path = make_text_pdf(tmp_path / "x.pdf", ["a", "b"])
    monkeypatch.setattr(pymupdf.Page, "get_text", lambda self, *a, **k: 1 / 0)
    doc = read_pdf(path)
    assert doc.error == "Aucune page lisible"
    assert doc.read_status is PdfStatus.PDF_ILLISIBLE
    assert len([w for w in doc.warnings if w.startswith("Page ")]) == 2


def test_describe_error_messages():
    assert describe_error(pymupdf.EmptyFileError("x"), pymupdf) == "Fichier vide"
    assert describe_error(pymupdf.FileNotFoundError("x"), pymupdf).startswith("Fichier introuvable")
    assert describe_error(FileNotFoundError("x"), None).startswith("Fichier introuvable")
    assert describe_error(PermissionError("verrou"), pymupdf).startswith("Accès refusé")
    assert describe_error(ValueError("oups"), pymupdf) == "ValueError : oups"
    assert describe_error(ValueError(), None) == "ValueError : ValueError"
