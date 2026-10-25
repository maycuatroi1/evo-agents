"""``evo-agents hub market seller-key``: a new seller key goes to a file only its owner reads, and the command prints
its public half and id, never the seed (plan plugin-market, criterion a7)."""

from __future__ import annotations

import base64
import json
import stat

from evo_agents.cli import main
from evo_agents.plugins.license import public_key_of
from evo_agents.plugins.signing import key_id


def test_seller_key_writes_the_seed_and_prints_only_the_public_half(tmp_path, capsys):
    target = tmp_path / "seller.key"
    assert main(["hub", "market", "seller-key", str(target), "--json"]) == 0
    printed = capsys.readouterr().out
    found = json.loads(printed)
    seed_text = target.read_text().strip()
    seed = base64.b64decode(seed_text, validate=True)
    assert len(seed) == 32
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert found == {
        "file": str(target),
        "key_id": key_id(base64.b64decode(public_key_of(seed))),
        "public_key": public_key_of(seed),
    }
    assert seed_text not in printed


def test_seller_key_never_overwrites_a_key(tmp_path, capsys):
    target = tmp_path / "seller.key"
    target.write_text("kept\n", newline="\n")
    assert main(["hub", "market", "seller-key", str(target)]) == 1
    assert target.read_text() == "kept\n"
    assert "never overwrites" in capsys.readouterr().err
