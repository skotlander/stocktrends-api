"""
Session-level module stubs for tests that import main.

sqlalchemy, jwt, cryptography, stripe, and mysql are not installed in this
test environment.  These stubs satisfy module-level imports without a live DB
or those libraries.  test_ai_tools.py and test_discovery_entrypoints.py both
depend on this infrastructure being in sys.modules before main is imported.
"""
import sys
from unittest.mock import MagicMock

_SQLALCHEMY_EXC = MagicMock()
_SQLALCHEMY_EXC.DBAPIError = Exception

_STUBS = {
    "sqlalchemy": MagicMock(),
    "sqlalchemy.orm": MagicMock(),
    "sqlalchemy.exc": _SQLALCHEMY_EXC,
    "db": MagicMock(),
    "jwt": MagicMock(),
    "cryptography": MagicMock(),
    "cryptography.hazmat": MagicMock(),
    "cryptography.hazmat.primitives": MagicMock(),
    "cryptography.hazmat.primitives.asymmetric": MagicMock(),
    "cryptography.hazmat.primitives.asymmetric.ed25519": MagicMock(),
    "cryptography.hazmat.primitives.serialization": MagicMock(),
    "mysql": MagicMock(),
    "mysql.connector": MagicMock(),
    "stripe": MagicMock(),
}

for _name, _stub in _STUBS.items():
    sys.modules.setdefault(_name, _stub)


# The settlement-ordering acceptance suite shares a measurement harness.
# Registering it as a plugin makes its fixtures available without every test
# module importing them by hand.
pytest_plugins = ["support.payment_harness"]
