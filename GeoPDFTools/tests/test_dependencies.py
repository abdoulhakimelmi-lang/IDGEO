import sys
import types

import pytest

from geopdf_core import dependencies
from geopdf_core.dependencies import DependencyError, check_pymupdf, import_pymupdf


@pytest.fixture(autouse=True)
def clear_import_cache():
    """``import_pymupdf`` est mis en cache : on le vide autour de chaque test."""
    import_pymupdf.cache_clear()
    yield
    import_pymupdf.cache_clear()


def test_pymupdf_is_found():
    module = import_pymupdf()
    assert hasattr(module, "open")
    message = check_pymupdf()
    assert message.startswith("PyMuPDF ")
    assert sys.executable in message


def test_missing_pymupdf_gives_clear_error(monkeypatch):
    # Une entrée None dans sys.modules fait échouer l'import correspondant.
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)

    with pytest.raises(DependencyError) as error:
        check_pymupdf()

    message = str(error.value)
    assert "'pymupdf'" in message
    assert "pip install pymupdf" in message
    assert "arcgispro-py3" in message
    assert sys.executable in message  # indique QUEL Python est utilisé


def test_dependency_error_is_an_import_error():
    assert issubclass(DependencyError, ImportError)


def test_fallback_to_legacy_fitz_name(monkeypatch):
    import pymupdf as real_module

    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", real_module)
    assert import_pymupdf() is real_module


def test_unrelated_fitz_package_is_rejected(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", types.ModuleType("fitz"))
    with pytest.raises(DependencyError):
        import_pymupdf()


def test_import_is_cached(monkeypatch):
    first = import_pymupdf()
    # Même si le module « disparaît », le résultat mis en cache est réutilisé.
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    assert dependencies.import_pymupdf() is first
