import importlib.util


def pytest_addoption(parser):
    # pyproject.toml sets asyncio_default_fixture_loop_scope for pytest-asyncio, which some environments have
    # installed; where it is missing, declare the key so pytest does not warn that it is unknown.
    if importlib.util.find_spec("pytest_asyncio") is None:
        parser.addini("asyncio_default_fixture_loop_scope", "read by pytest-asyncio when it is installed")
