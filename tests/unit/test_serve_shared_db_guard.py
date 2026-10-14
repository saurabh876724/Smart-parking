"""`serve.py` must refuse to open a database another process is already writing.

This is the test for the incident of 2026-10-13: the Docker API container bind-mounts
`data/`, and a host `serve.py --persist` opened the same `smart_parking.db`. Windows
let both bind port 8000, so neither failed loudly, and by the time anyone looked the
file had a page belonging to two tables at once - `select action from audit_logs`
was returning camera source paths - and `PRAGMA integrity_check` raised.

The container cannot be seen from the host as a process or a file handle, so the
guard asks the two questions that do reach it: is the port already answering, and is
the stack's own name in `docker ps` beside the exact file this run would open.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def serve():
    """`scripts/serve.py` as a module. Its `main()` is guarded, so importing starts nothing."""
    spec = importlib.util.spec_from_file_location("serve", ROOT / "scripts" / "serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def listening():
    """A real socket answering on a real port, which is the only honest way to test a probe."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture
def free_port():
    """A port number nothing holds. The socket is closed before the test sees it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_a_port_that_answers_is_reported_in_use(serve, listening):
    assert serve.port_in_use("127.0.0.1", listening) is True


def test_a_port_that_refuses_is_not(serve, free_port):
    assert serve.port_in_use("127.0.0.1", free_port, timeout=0.2) is False


def test_binding_everywhere_is_found_when_asked_about_the_loopback(serve, listening):
    # `--host 0.0.0.0` is the Docker and LAN form, and a probe of 0.0.0.0 is not a
    # thing you can connect to, so the address is translated rather than skipped.
    assert serve.port_in_use("0.0.0.0", listening) is True


def test_the_default_database_is_the_one_the_container_shares(serve, tmp_path, monkeypatch):
    root = tmp_path
    default = (root / "data" / "smart_parking.db").resolve()
    default.parent.mkdir(parents=True)
    default.touch()
    monkeypatch.setattr(serve, "port_in_use", lambda *a, **k: False)
    monkeypatch.setattr(serve, "running_containers", lambda: [serve.COMPOSE_API_CONTAINER])

    reasons = serve.shared_database_conflicts(
        host="127.0.0.1", port=8000, database_file=default, root=root, allow_shared_db=False
    )
    assert len(reasons) == 1
    assert "docker compose down" in reasons[0]


def test_a_scratch_database_is_not_a_conflict(serve, tmp_path, monkeypatch):
    # `--url` exists for exactly this: a copy to work on while the stack runs. Refusing
    # it would remove the only escape the message above offers.
    root = tmp_path
    monkeypatch.setattr(serve, "port_in_use", lambda *a, **k: False)
    monkeypatch.setattr(serve, "running_containers", lambda: [serve.COMPOSE_API_CONTAINER])

    reasons = serve.shared_database_conflicts(
        host="127.0.0.1",
        port=8000,
        database_file=root / "data" / "scratch_api.db",
        root=root,
        allow_shared_db=False,
    )
    assert reasons == []


def test_a_taken_port_is_refused_even_without_the_container(serve, tmp_path, monkeypatch):
    root = tmp_path
    monkeypatch.setattr(serve, "port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(serve, "running_containers", lambda: [])

    reasons = serve.shared_database_conflicts(
        host="127.0.0.1",
        port=8000,
        database_file=root / "data" / "smart_parking.db",
        root=root,
        allow_shared_db=False,
    )
    assert len(reasons) == 1
    assert "--port 8010" in reasons[0]


def test_the_override_silences_both_checks(serve, tmp_path, monkeypatch):
    root = tmp_path
    monkeypatch.setattr(serve, "port_in_use", lambda *a, **k: True)
    monkeypatch.setattr(serve, "running_containers", lambda: [serve.COMPOSE_API_CONTAINER])
    reasons = serve.shared_database_conflicts(
        host="127.0.0.1",
        port=8000,
        database_file=root / "data" / "smart_parking.db",
        root=root,
        allow_shared_db=True,
    )
    assert reasons == []


def test_no_docker_means_no_opinion(serve, monkeypatch):
    # A laptop with no Docker Desktop installed must still be able to start the API,
    # so every docker failure is a silence.
    def missing(*args, **kwargs):
        raise OSError("docker: command not found")

    monkeypatch.setattr(serve.subprocess, "run", missing)
    assert serve.running_containers() == []


def test_a_docker_that_fails_is_not_read_as_a_running_stack(serve, monkeypatch):
    monkeypatch.setattr(
        serve.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="")
    )
    assert serve.running_containers() == []


def test_docker_names_are_parsed_from_its_output(serve, monkeypatch):
    monkeypatch.setattr(
        serve.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="smart-parking-api\nother\n\n", stderr=""),
    )
    assert serve.running_containers() == ["smart-parking-api", "other"]


# --- the wiring: the guard has to run before the port opens, not after ---


@pytest.fixture
def wired(serve, monkeypatch, tmp_path):
    """`serve.main()` with everything heavy replaced, so only the guard's own logic runs."""
    db_file = tmp_path / "data" / "smart_parking.db"
    db_file.parent.mkdir(parents=True)
    db_file.touch()

    class FakeDatabase:
        url = "sqlite:///fake"
        file = db_file

        @classmethod
        def from_config(cls, cfg, override=None):
            return cls()

        def new_file_notice(self):
            return None

        def check(self, required):
            return {"reachable": True, "missing_tables": []}

        @contextmanager
        def scope(self):
            yield object()

    class FakeRepositories:
        users = SimpleNamespace(total=lambda: 2)

    monkeypatch.setattr(serve, "Database", FakeDatabase)
    monkeypatch.setattr(serve, "Repositories", lambda session, normalise=None: FakeRepositories)
    monkeypatch.setattr(serve, "jwt_secret_for", lambda cfg: "x" * 48)
    monkeypatch.setattr(serve, "plate_normaliser_for", lambda cfg: (lambda p: p))
    monkeypatch.setattr(
        serve, "load_config", lambda root=None: SimpleNamespace(api=SimpleNamespace(prefix="/api/v1"))
    )
    monkeypatch.setattr(serve, "create_app", lambda *a, **k: object())
    started: list = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **k: started.append(k))
    monkeypatch.setattr(serve, "ROOT", tmp_path)
    return SimpleNamespace(serve=serve, started=started, db_file=db_file, tmp=tmp_path)


def test_main_refuses_to_open_the_port_when_the_guard_objects(wired, monkeypatch):
    monkeypatch.setattr(wired.serve, "shared_database_conflicts", lambda **kw: ["another writer holds this file"])
    monkeypatch.setattr(sys, "argv", ["serve.py", "--persist"])
    assert wired.serve.main() == 1
    assert wired.started == []


def test_main_starts_when_the_guard_is_content(wired, monkeypatch):
    monkeypatch.setattr(wired.serve, "shared_database_conflicts", lambda **kw: [])
    monkeypatch.setattr(sys, "argv", ["serve.py", "--persist"])
    assert wired.serve.main() == 0
    assert len(wired.started) == 1
