import os
import threading
import time

from evo_agents import filelock


def test_a_lock_is_held_against_every_other_descriptor_until_it_is_released(tmp_path):
    path = tmp_path / "x.lock"
    first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert filelock.try_lock(first) is True
        assert filelock.try_lock(second) is False
        filelock.unlock(first)
        assert filelock.try_lock(second) is True
        assert filelock.try_lock(first) is False
    finally:
        os.close(first)
        os.close(second)
    third = os.open(path, os.O_RDWR)
    try:
        assert filelock.try_lock(third) is True
    finally:
        os.close(third)


def test_lock_waits_for_the_holder(tmp_path):
    path = tmp_path / "x.lock"
    held = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    waiter = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert filelock.try_lock(held)
        threading.Timer(0.3, filelock.unlock, (held,)).start()
        started = time.monotonic()
        filelock.lock(waiter)
        assert time.monotonic() - started >= 0.2
        filelock.unlock(waiter)
    finally:
        os.close(held)
        os.close(waiter)
