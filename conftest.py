"""Early-loading shim for command-line options.

Pytest does not discover a nested ``tests/conftest.py`` early enough when it
is invoked without an explicit test path. Loading the shared module here keeps
both forms working::

    pytest --config config/test.yaml
    pytest --config config/test.yaml tests/test_http.py
"""

# Keep imports such as ``from conftest import AcceptanceConfig`` compatible
# with the existing test modules when this root shim is imported as conftest.
from tests.conftest import *  # noqa: F401,F403,E402
