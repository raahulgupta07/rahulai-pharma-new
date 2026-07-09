"""Smoke tests verifying the Phase 0 scaffold imports cleanly.

These do not exercise behavior; they only confirm that the core modules
can be imported, catching packaging or syntax errors early.
"""


def test_health_import():
    """The FastAPI app object should be importable from app.api."""
    from app.api import app

    assert app is not None


def test_config_module_imports():
    """The config module should import without side-effect errors."""
    import app.config  # noqa: F401
