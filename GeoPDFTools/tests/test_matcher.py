"""Tests du moteur de correspondance (étape 2).

Les identifiants d'exemple imitent des données SIG réelles :
sections de route, PR (points de repère), codes de dossier.
"""

import hashlib
import time
from pathlib import Path

import pytest

from conftest import make_encrypted_pdf, make_text_pdf
from geopdf_core.matcher import (
    RISK_NO_DIGIT,
    RISK_NUMERIC,
    RISK_SHORT,
    IdentifierIndex,
    canonical_key,
    classify_risk,
    match_document,
    normalize_identifier,
    normalize_text,
)
from geopdf_core.models import DetectionSource, PdfDocument, PdfStatus, SearchMode
from geopdf_core.pdf_reader import find_pdf_files, read_pdf

SIG_IDS = ["N57_001", "N57_0010", "RN57-025", "A31_0042", "PR12+350", "SEC-2026-001"]


@pytest.fixture
def index():
    """Index strict construit à partir des identifiants d'exemple (OID = 1, 2, 3…)."""
    return IdentifierIndex(enumerate(SIG_IDS, start=1))


@pytest.fixture
def tolerant_index():
    return IdentifierIndex(enumerate(SIG_IDS, start=1), tolerant_separators=True)


def doc(name="sans_id.pdf", text="", error=None, pages=1):
    """PdfDocument construit directement, sans fichier sur disque."""
    return PdfDocument(path=Path("C:/Rapports") / name, page_count=pages, pages_text=[text], error=error)


# --- Normalisation ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("n57_001", "N57_001"),  # casse
        ("N57\u00a0001", "N57 001"),  # espace insécable
        ("N57\u202f001", "N57 001"),  # espace fine insécable
        ("RN57\u2013025", "RN57-025"),  # tiret demi-cadratin
        ("RN57\u2011025", "RN57-025"),  # trait d'union insécable
        ("RN57\u2212025", "RN57-025"),  # signe moins
        ("SEC-2026-\u00ad001", "SEC-2026-001"),  # trait d'union conditionnel
        ("N57_\u200b001", "N57_001"),  # espace de largeur nulle
        ("\ufeffN57_001", "N57_001"),  # BOM
        ("\uff2e\uff15\uff17\uff3f\uff10\uff10\uff11", "N57_001"),  # pleine chasse
        ("\ufb01chier", "FICHIER"),  # ligature « fi »
        ("re\u0301alise\u0301e", "RÉALISÉE"),  # accent décomposé
        ("ligne 1\n\t  ligne 2", "LIGNE 1 LIGNE 2"),  # espaces multiples
        ("", ""),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        (True, ""),
        (float("nan"), ""),
        (" n57_001 ", "N57_001"),
        (12, "12"),
        (12.0, "12"),  # entier stocké dans un champ réel
        (12.5, "12.5"),
    ],
)
def test_normalize_identifier(value, expected):
    assert normalize_identifier(value) == expected


def test_canonical_key():
    assert canonical_key("N57 _002") == "N57 _002"  # mode strict : inchangé
    assert canonical_key("N57 _002", tolerant_separators=True) == "N57_002"
    assert canonical_key("PR12+350", tolerant_separators=True) == "PR12_350"
    assert canonical_key("-N57-", tolerant_separators=True) == "N57"


@pytest.mark.parametrize(
    "key, expected",
    [
        ("N57_001", []),
        ("SEC-2026-001", []),
        ("123456", []),  # numérique mais assez long
        ("12", [RISK_SHORT, RISK_NUMERIC]),
        ("001", [RISK_SHORT, RISK_NUMERIC]),
        ("2026", [RISK_NUMERIC]),
        ("N57", [RISK_SHORT]),
        ("A-1", [RISK_SHORT]),
        ("NORD", [RISK_NO_DIGIT]),
    ],
)
def test_classify_risk(key, expected):
    assert classify_risk(key) == expected


# --- Exemples SIG : acceptés / refusés ---------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        # Exemple du cahier des charges
        ("Inspection réalisée sur la section N57_001 le 15/09/2026.", ["N57_001"]),
        # N57_001 ne doit jamais être trouvé dans N57_0010
        ("Section N57_0010", ["N57_0010"]),
        ("Section N57_00100", []),
        ("Sections N57_001 et N57_0010", ["N57_001", "N57_0010"]),
        ("N57_001N57_0010", []),  # collés : aucune borne
        ("section n57_001", ["N57_001"]),  # casse
        # RN57-025
        ("Route RN57-025", ["RN57-025"]),
        ("Route RN57\u2013025", ["RN57-025"]),  # tiret typographique
        ("Route RN57-0250", []),
        ("Route XRN57-025", []),
        ("RN57-025,5 km", []),  # nombre décimal : autre valeur
        ("RN57-025, puis A31_0042", ["RN57-025", "A31_0042"]),
        # A31_0042
        ("A31_0042.", ["A31_0042"]),
        ("A31_00420", []),
        ("BA31_0042", []),
        # PR12+350
        ("du PR12+350 au PR12+800", ["PR12+350"]),
        ("PR12+3500", []),
        ("PR12+350,5", []),
        ("PR12+350.5", []),
        # SEC-2026-001
        ("Dossier SEC-2026-001 (clos)", ["SEC-2026-001"]),
        ("Dossier SEC-2026-0012", []),
        ("Dossier sec-2026-001", ["SEC-2026-001"]),
        # Unicode
        ("\uff2e\uff15\uff17\uff3f\uff10\uff10\uff11", ["N57_001"]),
        ("SEC-2026-\u00ad001", ["SEC-2026-001"]),
        ("N57_\u200b001", ["N57_001"]),
        # Contexte tabulaire, dates, chemins
        ("N57_001\t12,5 km\t15/09/2026", ["N57_001"]),
        ("N57_001 15/09/2026", ["N57_001"]),
        ("\\\\serveur\\rapports\\N57_001.pdf", ["N57_001"]),
        # Aucun identifiant
        ("Rapport d'octobre sans référence", []),
        ("", []),
    ],
)
def test_find_sig_examples(index, text, expected):
    assert index.find(text) == expected


@pytest.mark.parametrize(
    "stem, expected",
    [
        ("inspection_N57_001_v2", ["N57_001"]),  # « _ » autour : accepté
        ("N57_001", ["N57_001"]),
        ("rapport_001", []),  # « 001 » seul n'est pas N57_001
        ("inspection_N57", []),
        ("2026-09-15_RN57-025_inspection", ["RN57-025"]),
        ("PR12+350_PR12+800", ["PR12+350"]),
        ("SEC-2026-001_SEC-2026-0010", ["SEC-2026-001"]),
    ],
)
def test_find_in_filenames(index, stem, expected):
    assert index.find(stem, in_content=False) == expected


def test_order_of_appearance_and_no_duplicates(index):
    text = "N57_0010 ... N57_001 ... N57_0010 ... n57_001"
    assert index.find(text) == ["N57_0010", "N57_001"]


def test_longest_identifier_wins():
    ix = IdentifierIndex(enumerate(["N57_001", "N57_001_A", "RN57", "RN57-025"]))
    assert ix.find("N57_001_A") == ["N57_001_A"]
    assert ix.find("N57_001_B") == ["N57_001"]
    assert ix.find("RN57-025") == ["RN57-025"]
    assert ix.find("RN57 et RN57-025") == ["RN57", "RN57-025"]
    # « RN57-02 » n'est ni RN57-025, ni RN57 (suite numérique) :
    assert ix.find("RN57-02") == []


@pytest.mark.parametrize(
    "identifier, accepted, refused",
    [
        ("ZONE 4 NORD", ["zone  4\nnord", "Zone 4 Nord."], ["ZONE 4 NORDEST", "ZONE 45 NORD"]),
        ("D/2026/15", ["Arrêté D/2026/15 du"], ["D/2026/150", "D/2026/15/3"]),
        ("A.B.1234", ["réf A.B.1234"], ["AXB.1234", "A.B.12345"]),
        ("LOT(3)*", ["le LOT(3)* est"], ["LOT(3)"]),  # caractères spéciaux d'expressions régulières
        ("{0A1B-77}", ["guid {0a1b-77}"], ["{0A1B-7}"]),
        ("123456", ["parcelle 123456"], ["tél 01.123456", "1234567", "123456,78"]),
    ],
)
def test_identifiers_with_separators_spaces_and_special_chars(identifier, accepted, refused):
    ix = IdentifierIndex([(1, identifier)])
    key = normalize_identifier(identifier)
    for text in accepted:
        assert ix.find(text) == [key], text
    for text in refused:
        assert ix.find(text) == [], text


# --- Mode tolérant aux séparateurs -----------------------------------------


@pytest.mark.parametrize(
    "text, strict, tolerant",
    [
        ("N57 _001", [], ["N57_001"]),  # espace parasite
        ("N57-\n001", [], ["N57_001"]),  # retour à la ligne
        ("N57\u00a0001", [], ["N57_001"]),  # espace insécable à la place de « _ »
        ("N57/001", [], ["N57_001"]),
        ("N57001", [], []),  # séparateur absent : jamais accepté
        ("SEC 2026 001", [], ["SEC_2026_001"]),
        ("N57_001", ["N57_001"], ["N57_001"]),
    ],
)
def test_strict_vs_tolerant(index, tolerant_index, text, strict, tolerant):
    assert index.find(text) == strict
    assert tolerant_index.find(text) == tolerant


def test_tolerant_keeps_layer_value_for_display(tolerant_index):
    result = match_document(doc(text="SEC 2026 001"), tolerant_index, SearchMode.CONTENU)
    assert result.identifiers == ["SEC-2026-001"]


# --- Identifiants courts ou numériques ---------------------------------------


@pytest.fixture
def risky_index():
    return IdentifierIndex([(1, "12"), (2, "001"), (3, "2026"), (4, "N57_001")])


def test_risky_ids_are_ignored_in_content(risky_index):
    text = "Page 12 — année 2026 — lot 001 — section N57_001"
    assert risky_index.find(text) == ["N57_001"]


def test_risky_ids_are_searched_in_filenames(risky_index):
    assert risky_index.find("12", in_content=False) == ["12"]
    assert risky_index.find("2026", in_content=False) == ["2026"]
    assert risky_index.find("dossier_12", in_content=False) == ["12"]


def test_numeric_ids_do_not_match_inside_number_chains(risky_index):
    # « 2026 » et « 001 » font partie d'une suite numérique : refusés.
    assert risky_index.find("rapport_2026_001", in_content=False) == []
    assert risky_index.find("15-12-2026", in_content=False) == []
    assert risky_index.find("N57_001", in_content=False) == ["N57_001"]  # pas « 001 »


def test_risky_ids_can_be_allowed_in_content():
    ix = IdentifierIndex([(1, "12"), (2, "N57_001")], allow_risky_in_content=True)
    assert ix.find("Page 12") == ["12"]


# --- Index : doublons, valeurs vides, messages -------------------------------


def test_index_groups_duplicates_and_case_variants():
    ix = IdentifierIndex([(10, "N57_001"), (11, "N57_001"), (12, "n57_001 "), (13, "A31_0042")])
    assert len(ix) == 2
    entry = ix.get("N57_001")
    assert entry.oids == [10, 11, 12]
    assert entry.value == "N57_001"  # première valeur rencontrée
    assert entry.variants == ["N57_001", "n57_001"]
    assert [e.key for e in ix.duplicates] == ["N57_001"]


def test_index_ignores_empty_values():
    ix = IdentifierIndex([(1, None), (2, ""), (3, "  "), (4, "N57_001")])
    assert len(ix) == 1
    assert ix.ignored_count == 3


def test_index_accepts_numeric_field_values():
    ix = IdentifierIndex([(1, 570012), (2, 570013.0)])
    assert ix.find("parcelles 570012 et 570013") == ["570012", "570013"]


def test_empty_index_finds_nothing():
    ix = IdentifierIndex([])
    assert len(ix) == 0
    assert ix.find("N57_001") == []
    assert ix.find("N57_001", in_content=False) == []


def test_summary_messages():
    ix = IdentifierIndex([(1, "N57_001"), (2, "N57_001"), (3, None), (4, "12"), (5, "NORD")])
    messages = ix.summary_messages()
    levels = [level for level, _ in messages]
    text = "\n".join(message for _, message in messages)
    assert messages[0] == ("INFO", "3 identifiant(s) distinct(s) chargé(s).")
    assert levels.count("WARNING") == 4
    assert "1 valeur(s) vide(s)" in text
    assert "N57_001" in text and "plusieurs entités" in text
    assert "uniquement dans le nom" in text and "12" in text
    assert "sans chiffre" in text and "NORD" in text


# --- match_document : modes, sources, statuts --------------------------------


def test_mode_nom_ignores_content(index):
    result = match_document(doc("N57_001.pdf", "A31_0042"), index, SearchMode.NOM)
    assert result.identifiers == ["N57_001"]
    assert result.matches[0].source is DetectionSource.NOM
    assert result.status is PdfStatus.ASSOCIE


def test_mode_contenu_ignores_filename(index):
    result = match_document(doc("N57_001.pdf", "A31_0042"), index, SearchMode.CONTENU)
    assert result.identifiers == ["A31_0042"]
    assert result.matches[0].source is DetectionSource.CONTENU


def test_same_id_in_name_and_content_counts_once(index):
    result = match_document(doc("N57_001.pdf", "Section N57_001"), index, SearchMode.NOM_ET_CONTENU)
    assert result.match_count == 1
    assert result.matches[0].source is DetectionSource.NOM_ET_CONTENU
    assert result.detection_source is DetectionSource.NOM_ET_CONTENU
    assert result.status is PdfStatus.ASSOCIE


def test_several_ids_with_mixed_sources(index):
    result = match_document(
        doc("N57_001.pdf", "Sections N57_001, A31_0042 et PR12+350"), index, SearchMode.NOM_ET_CONTENU
    )
    assert result.identifiers == ["N57_001", "A31_0042", "PR12+350"]
    assert [m.source for m in result.matches] == [
        DetectionSource.NOM_ET_CONTENU,
        DetectionSource.CONTENU,
        DetectionSource.CONTENU,
    ]
    assert result.status is PdfStatus.PLUSIEURS_CORRESPONDANCES
    assert result.oids == [1, 4, 5]
    assert any("Plusieurs identifiants" in w for w in result.warnings)


def test_no_match(index):
    result = match_document(doc("rapport_octobre.pdf", "Rien ici"), index, SearchMode.NOM_ET_CONTENU)
    assert result.matches == []
    assert result.status is PdfStatus.AUCUNE_CORRESPONDANCE
    assert result.detection_source is None


def test_duplicate_id_in_layer_associates_all_entities():
    ix = IdentifierIndex([(7, "N57_001"), (9, "N57_001")])
    result = match_document(doc(text="Section N57_001"), ix, SearchMode.CONTENU)
    assert result.status is PdfStatus.ASSOCIE  # un seul identifiant…
    assert result.oids == [7, 9]  # … mais deux entités
    assert result.entity_count == 2
    assert any("2 entités" in w for w in result.warnings)


@pytest.mark.parametrize(
    "mode, name, expected",
    [
        (SearchMode.CONTENU, "N57_001.pdf", PdfStatus.PDF_ILLISIBLE),
        (SearchMode.NOM_ET_CONTENU, "rapport.pdf", PdfStatus.PDF_ILLISIBLE),
        (SearchMode.NOM_ET_CONTENU, "N57_001.pdf", PdfStatus.ASSOCIE),  # sauvé par le nom
        (SearchMode.NOM, "rapport.pdf", PdfStatus.AUCUNE_CORRESPONDANCE),  # contenu non demandé
    ],
)
def test_status_of_unreadable_pdf(index, mode, name, expected):
    result = match_document(doc(name, error="PDF protégé par mot de passe", pages=0), index, mode)
    assert result.status is expected
    assert result.document.error == "PDF protégé par mot de passe"  # l'erreur reste visible


def test_status_of_pdf_without_text(index):
    scanned = doc("scan.pdf", "   ")
    assert match_document(scanned, index, SearchMode.CONTENU).status is PdfStatus.PDF_SANS_TEXTE
    named = doc("scan_RN57-025.pdf", "   ")
    result = match_document(named, index, SearchMode.NOM_ET_CONTENU)
    assert result.status is PdfStatus.ASSOCIE
    assert result.matches[0].source is DetectionSource.NOM


def test_risky_id_found_in_filename_only(risky_index):
    result = match_document(doc("12.pdf", "page 12"), risky_index, SearchMode.NOM_ET_CONTENU)
    assert result.identifiers == ["12"]
    assert result.matches[0].source is DetectionSource.NOM


# --- Intégration avec de vrais PDF (étape 1 + étape 2) -----------------------


def test_end_to_end_with_real_pdfs(tmp_path, index):
    root = tmp_path / "Rapports"
    make_text_pdf(root / "rapport_001.pdf", ["Inspection réalisée sur la section N57_001 le 15/09/2026."])
    make_text_pdf(root / "inspection_N57.pdf", ["Sections N57_0010 et RN57-025", "Page 2 : PR12+350"])
    make_text_pdf(root / "2026" / "rapport_octobre.pdf", ["Aucune référence."])
    make_text_pdf(root / "2026" / "SEC-2026-001.pdf", ["Dossier SEC-2026-001."])
    make_encrypted_pdf(root / "secret.pdf")
    files = find_pdf_files(root)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

    results = {p.name: match_document(read_pdf(p), index, SearchMode.NOM_ET_CONTENU) for p in files}

    assert results["rapport_001.pdf"].identifiers == ["N57_001"]
    assert results["rapport_001.pdf"].status is PdfStatus.ASSOCIE
    assert results["inspection_N57.pdf"].identifiers == ["N57_0010", "RN57-025", "PR12+350"]
    assert results["inspection_N57.pdf"].status is PdfStatus.PLUSIEURS_CORRESPONDANCES
    assert results["rapport_octobre.pdf"].status is PdfStatus.AUCUNE_CORRESPONDANCE
    assert results["SEC-2026-001.pdf"].matches[0].source is DetectionSource.NOM_ET_CONTENU
    assert results["secret.pdf"].status is PdfStatus.PDF_ILLISIBLE
    # Les PDF n'ont pas été modifiés.
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files} == before


# --- Performance ------------------------------------------------------------


def test_many_identifiers_and_long_text():
    ids = ["SEC-{:05d}".format(i) for i in range(20000)]
    start = time.perf_counter()
    ix = IdentifierIndex(enumerate(ids))
    build_seconds = time.perf_counter() - start

    filler = "Lorem ipsum dolor sit amet 15/09/2026 PR 12,5 km. " * 20000  # ~1 Mo
    text = filler + " SEC-00042 " + filler + " SEC-19999 SEC-200000"
    start = time.perf_counter()
    found = ix.find(text)
    search_seconds = time.perf_counter() - start

    assert found == ["SEC-00042", "SEC-19999"]
    # Marges larges pour ne pas dépendre de la vitesse de la machine.
    assert build_seconds < 10
    assert search_seconds < 10
