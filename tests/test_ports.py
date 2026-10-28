"""Tests for sketchup_mcp.ports: 1<vv><nn> numbering, finding the running
SketchUps, and select_sketchup, against fake SketchUp servers on real sockets."""
import asyncio
import json
import socket
import struct

import pytest

from sketchup_mcp import compat, config, ports
from sketchup_mcp import connection as conn_module
from sketchup_mcp import tools
from sketchup_mcp.connection import SketchUpConnection


@pytest.fixture(autouse=True)
def auto_ports(monkeypatch):
    """Automatic choice, no singleton, loopback: each test starts from scratch."""
    monkeypatch.setattr(config, "PORT", None)
    monkeypatch.setattr(config, "HOST", "127.0.0.1")
    monkeypatch.setattr(ports, "_target", {"port": None, "pinned": False})
    monkeypatch.setattr(conn_module, "_connection", None)


def closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


async def fake_sketchup(title: str):
    """A SketchUp MCP server that answers hello and get_model_info: (server, port)."""
    async def handle(reader, writer):
        try:
            while True:
                (length,) = struct.unpack(">I", await reader.readexactly(4))
                request = json.loads(await reader.readexactly(length))
                if request["method"] == "hello":
                    result = {"server_version": compat.MIN_RUBY, "client_id": 1}
                else:
                    info = {"title": title, "path": f"C:/{title}.skp"}
                    result = {"content": [{"type": "text", "text": json.dumps(info)}], "isError": False}
                body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
                writer.write(struct.pack(">I", len(body)) + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def model_title() -> str:
    conn = await conn_module.get_connection()
    result = await conn.send_command("get_model_info", {})
    return json.loads(result["content"][0]["text"])["title"]


# --- numbering ---

def test_instance_ports_are_app_version_instance():
    assert ports.instance_port("2026") == 12601
    assert ports.instance_port(2026, 2) == 12602
    assert ports.instance_port(" 2025 ") == 12501
    assert ports.port_version(12602) == ("2026", 2)
    assert ports.port_version(12501) == ("2025", 1)
    assert ports.port_version(22601) is None  # Revit's
    assert ports.port_version(12600) is None
    assert ports.port_version(9876) is None
    assert ports.version_ports("2026") == list(range(12601, 12610))
    assert ports.version_ports("SketchUp") == []


def test_candidates_are_each_version_then_the_extension_port(monkeypatch):
    monkeypatch.setattr(ports, "installed_years", lambda: ["2025", "2026"])
    assert ports.candidate_ports() == [*range(12501, 12510), *range(12601, 12610), 9876]
    assert ports.ranges(ports.candidate_ports()) == "12501-12509, 12601-12609, 9876"
    assert ports.ranges([]) == ""


def test_installed_years_from_program_files(monkeypatch, tmp_path):
    # 2025+ keep SketchUp.exe in a SketchUp subfolder, older ones directly; 2023 has no exe left
    for year, exe in (("2026", "SketchUp/SketchUp.exe"), ("2024", "SketchUp.exe"), ("2023", None)):
        folder = tmp_path / "SketchUp" / f"SketchUp {year}"
        folder.mkdir(parents=True)
        if exe:
            (folder / exe).parent.mkdir(parents=True, exist_ok=True)
            (folder / exe).write_bytes(b"")
    monkeypatch.setattr(ports.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    assert ports.installed_years() == ["2024", "2026"]
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nothing"))
    assert ports.installed_years() == ports.DEFAULT_YEARS


# --- choosing the port ---

async def test_resolve_sticks_to_the_sketchup_in_use_until_rescanned(monkeypatch):
    live = [12602, 9876]

    async def listening(_ports):
        return list(live)

    monkeypatch.setattr(ports, "listening", listening)
    assert await ports.resolve_port() == 12602
    live[:] = [12601]
    assert await ports.resolve_port() == 12602
    assert await ports.resolve_port(rescan=True) == 12601
    ports.pin(12603)
    assert await ports.resolve_port(rescan=True) == 12603
    assert ports.current() == {"port": 12603, "fixed_by_env": False, "pinned": True}
    ports.unpin()
    live[:] = []
    assert await ports.resolve_port() is None
    monkeypatch.setattr(config, "PORT", 4000)
    assert await ports.resolve_port(rescan=True) == 4000
    assert ports.current()["fixed_by_env"] is True


async def test_listening_checks_real_sockets():
    server, port = await fake_sketchup("A")
    try:
        closed = closed_port()
        assert await ports.listening([closed, port]) == [port]
    finally:
        server.close()


async def test_get_connection_finds_the_port_only_when_not_fixed(monkeypatch):
    conn = await conn_module.get_connection()
    assert conn.port is None and conn.find_port is ports.resolve_port
    monkeypatch.setattr(conn_module, "_connection", None)
    monkeypatch.setattr(config, "PORT", 4000)
    conn = await conn_module.get_connection()
    assert conn.port == 4000 and conn.find_port is None
    monkeypatch.setattr(conn_module, "_connection", None)


async def test_no_sketchup_names_the_ports_it_looked_at(monkeypatch):
    monkeypatch.setattr(ports, "candidate_ports", lambda: [closed_port()])
    conn = SketchUpConnection(host="127.0.0.1", port=None, timeout=2.0, find_port=ports.resolve_port)
    with pytest.raises(ConnectionError, match="no SketchUp MCP server answers on 127.0.0.1"):
        await conn.ensure_connected()


async def test_two_sketchups_reach_either_and_follow_a_closed_one(monkeypatch):
    first, port_a = await fake_sketchup("A")
    second, port_b = await fake_sketchup("B")
    monkeypatch.setattr(ports, "candidate_ports", lambda: [closed_port(), port_a, port_b])
    try:
        # the first that answers
        assert await model_title() == "A"

        # select_sketchup lists both and moves the next call to the one chosen
        listed = json.loads(await tools.select_sketchup(None, port=port_b))
        assert [(s["port"], s["title"]) for s in listed["running_sketchups"]] == [(port_a, "A"), (port_b, "B")]
        assert listed["target"] == {"port": port_b, "fixed_by_env": False, "pinned": True}
        assert await model_title() == "B"

        # back to automatic: the first again
        listed = json.loads(await tools.select_sketchup(None))
        assert listed["target"] == {"port": port_a, "fixed_by_env": False, "pinned": False}
        assert await model_title() == "A"

        # A closes: the next connect finds B
        await conn_module.close_connection()
        first.close()
        assert await model_title() == "B"
        assert ports.current()["port"] == port_b
    finally:
        await conn_module.close_connection()
        first.close()
        second.close()


async def test_select_by_version_takes_its_first_running_instance(monkeypatch):
    async def listening(candidates):
        return [p for p in candidates if p in (12601, 12602)]

    async def running():
        return [{"port": 12601}, {"port": 12602}]

    async def close():
        closed.append(True)

    closed = []
    monkeypatch.setattr(ports, "listening", listening)
    monkeypatch.setattr(ports, "running_sketchups", running)
    monkeypatch.setattr(tools, "close_connection", close)

    result = json.loads(await tools.select_sketchup(None, version="2026"))
    assert result["target"] == {"port": 12601, "fixed_by_env": False, "pinned": True}
    assert "2 SketchUp 2026 are open (ports 12601, 12602)" in result["note"]
    assert closed == [True]

    result = json.loads(await tools.select_sketchup(None, port=12609))
    assert result["warning"] == "Nothing answers on port 12609 yet."

    monkeypatch.setattr(config, "PORT", 9876)
    result = json.loads(await tools.select_sketchup(None, port=12602))
    assert result["target"]["port"] == 9876
    assert "SKETCHUP_MCP_PORT fixes the port" in result["warning"]
