"""Check what a command printed with --json against the keys the contract declares for it (seam hub-cli-v1)."""

from functools import cache

from evo_agents.hub.contract import commands


@cache
def _commands() -> dict:
    return commands()


def assert_json_keys(command: str, data, *options: str) -> None:
    """Fail unless ``data``, printed by ``command`` (``"hub plan list"``) with --json and ``options``, has the
    shape and the keys of the contract."""
    output = _commands()[command]["json"]
    for option in options:
        output = output["with"][option]
    if output["kind"] == "map":
        assert isinstance(data, dict), f"{command} --json printed {type(data).__name__}, not an object"
        return
    items = data if output["kind"] == "array" else [data]
    assert isinstance(data, list if output["kind"] == "array" else dict), f"{command} --json: not an {output['kind']}"
    for item in items:
        assert set(item) == set(output["keys"]), (
            f"{command} --json printed the keys {sorted(item)}, the contract says {sorted(output['keys'])}"
        )
