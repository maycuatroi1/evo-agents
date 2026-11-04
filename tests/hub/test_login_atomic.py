"""``hub login`` saves the URL and the token of a hub in one atomic write (``client.save_credentials``): a Ctrl-C at
any point leaves the old login or the new one, never the token of one hub beside the URL of another."""

import json

import pytest

from evo_agents.hub import client as hub_client


def test_login_atomic_interrupted_after_the_first_write_keeps_one_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hub_client.save_credentials("https://old.test", "octo", "evh_old")
    real = hub_client.write_private
    writes = []

    def interrupted(path, data):
        writes.append(path.name)
        if len(writes) == 2:  # Ctrl-C between the two files
            raise KeyboardInterrupt
        real(path, data)

    monkeypatch.setattr(hub_client, "write_private", interrupted)
    with pytest.raises(KeyboardInterrupt):
        hub_client.save_credentials("https://new.test", "octo", "evh_new")
    assert writes == ["config.json", "token"]
    # The token file still holds the old hub's token, but the login read is the new hub's whole.
    assert hub_client.load_credentials() == hub_client.Credentials("https://new.test", "octo", "evh_new")


def test_login_atomic_interrupted_before_any_write_keeps_the_old_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hub_client.save_credentials("https://old.test", "octo", "evh_old")

    def interrupted(path, data):
        raise KeyboardInterrupt

    monkeypatch.setattr(hub_client, "write_private", interrupted)
    with pytest.raises(KeyboardInterrupt):
        hub_client.save_credentials("https://new.test", "octo", "evh_new")
    assert hub_client.load_credentials() == hub_client.Credentials("https://old.test", "octo", "evh_old")


def test_login_atomic_reads_the_token_file_beside_a_config_without_one(tmp_path, monkeypatch):
    """What an older release or a CI script writes: config.json with the URL alone, the token beside it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / ".evo" / "hub"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"url": "https://hub.test", "login": "octo"}))
    (directory / "token").write_text("evh_file\n")
    assert hub_client.load_credentials() == hub_client.Credentials("https://hub.test", "octo", "evh_file")
