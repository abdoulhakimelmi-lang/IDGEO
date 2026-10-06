"""Fixtures communes : génération de PDF de test avec PyMuPDF.

Les PDF sont créés à la volée dans un dossier temporaire : aucun fichier
binaire n'est stocké dans le dépôt.
"""

from pathlib import Path
from typing import List

import pytest

pymupdf = pytest.importorskip("pymupdf")


def make_text_pdf(path: Path, pages: List[str]) -> Path:
    """Crée un PDF dont chaque élément de ``pages`` est le texte d'une page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        for text in pages:
            page = doc.new_page()
            # insert_textbox gère le retour à la ligne et les accents (police Helvetica).
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=11)
        doc.save(str(path))
    return path


def make_image_only_pdf(path: Path) -> Path:
    """Simule un PDF scanné : une page contenant une image et aucun texte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        page = doc.new_page()
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), False)
        pixmap.clear_with(180)
        page.insert_image(pymupdf.Rect(50, 50, 300, 300), pixmap=pixmap)
        doc.save(str(path))
    return path


def make_encrypted_pdf(path: Path, text: str = "Section N57_001") -> Path:
    """PDF protégé par un mot de passe d'ouverture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), text)
        doc.save(
            str(path),
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw="secret",
            owner_pw="owner-secret",
        )
    return path


@pytest.fixture
def pdf_folder(tmp_path: Path) -> Path:
    """Dossier réaliste : PDF valides, sous-dossiers, extensions variées, intrus."""
    root = tmp_path / "Rapports"
    make_text_pdf(
        root / "rapport_001.pdf",
        ["Inspection réalisée sur la section N57_002 le 15/09/2026."],
    )
    make_text_pdf(root / "2026" / "octobre" / "inspection_N57.PDF", ["Section N57_003"])
    make_text_pdf(root / "2026" / "Été été.pdf", ["Accents dans le nom du fichier"])
    (root / "notes.txt").write_text("pas un PDF", encoding="utf-8")
    (root / "ancien.pdf.bak").write_bytes(b"sauvegarde")
    (root / "dossier.pdf").mkdir()  # un dossier nommé .pdf n'est pas un fichier
    return root
