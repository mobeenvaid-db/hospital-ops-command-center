# Capacity Command — Config tests (schema validation)

import sys
from unittest.mock import patch

import pytest


def test_invalid_schema_name_raises():
    """Invalid LAKEBASE_SCHEMA (e.g. SQL injection) must raise ValueError on import."""
    with patch.dict("os.environ", {"LAKEBASE_SCHEMA": "x; DROP TABLE"}, clear=False):
        if "app.backend.config" in sys.modules:
            del sys.modules["app.backend.config"]
        with pytest.raises(ValueError, match="Invalid schema"):
            import app.backend.config  # noqa: F401


def test_valid_schema_name_loads():
    """Valid LAKEBASE_SCHEMA (alphanumeric + underscore) loads without error."""
    with patch.dict("os.environ", {"LAKEBASE_SCHEMA": "hospital_ops"}, clear=False):
        if "app.backend.config" in sys.modules:
            del sys.modules["app.backend.config"]
        import app.backend.config as cfg

        assert cfg.SCHEMA == "hospital_ops"
