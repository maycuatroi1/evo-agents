import pytest

from tests.plugins.backends import make_root, remove_root


@pytest.fixture
def plugin_root(tmp_path):
    """The directory a test's plugin host places plugins in (``tests.plugins.backends.make_root``)."""
    root = make_root(tmp_path)
    yield root
    remove_root(root)
