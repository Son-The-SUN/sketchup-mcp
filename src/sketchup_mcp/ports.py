"""Which port each SketchUp's MCP server listens on, and which one the tools talk to.

The GSA autostart (gsa_mcp_autostart.rb in SketchUp's Plugins folder) starts each
SketchUp's server on 1<vv><nn>: 1 for SketchUp, vv its version (``Sketchup.version``:
SketchUp 2026 -> 26), nn its instance of that version from 01. The first SketchUp 2026
listens on 12601, a second SketchUp 2026 on 12602, the first SketchUp 2025 on 12501.

A server started from Plugins > MCP Server > Start Server listens on the extension's own
port (9876 unless changed in Settings...), looked at last. SKETCHUP_MCP_PORT in the
environment fixes the port instead (config.PORT).
"""
import asyncio
import glob
import json
import os
import sys

from sketchup_mcp import config

SKETCHUP_APP = 1
# the extension's default port, for a server started from its menu
LEGACY_PORT = 9876
# instances of one version looked for (the autostart numbers up to 99)
INSTANCES = 9
# versions to look for when no SketchUp is found installed (the autostart is for 2021+)
DEFAULT_YEARS = [str(y) for y in range(2021, 2031)]

# port the tools talk to; pinned = chosen by select_sketchup, never re-scanned
_target: dict = {"port": None, "pinned": False}


def instance_port(year, instance=1) -> int:
    """1<vv><nn>: SketchUp 2026's first instance listens on 12601."""
    return SKETCHUP_APP * 10000 + int(str(year).strip()) % 100 * 100 + int(instance)


def port_version(port):
    """(year, instance) of a 1<vv><nn> port, else None: 12602 -> ("2026", 2)."""
    if not port or port // 10000 != SKETCHUP_APP or port % 100 == 0:
        return None
    return "20{:02d}".format(port // 100 % 100), port % 100


def version_ports(version) -> list[int]:
    """The instance ports looked at for one SketchUp version, [] when it is not a year."""
    try:
        return [instance_port(version, n) for n in range(1, INSTANCES + 1)]
    except (TypeError, ValueError):
        return []


def installed_years() -> list[str]:
    """SketchUp years installed on this machine, oldest first, else DEFAULT_YEARS."""
    if sys.platform == "win32":
        # SketchUp 2025+ keeps SketchUp.exe in a SketchUp subfolder (beside LayOut), older ones directly
        root = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "SketchUp")
        found = glob.glob(os.path.join(root, "SketchUp 20[0-9][0-9]", "SketchUp.exe")) + glob.glob(
            os.path.join(root, "SketchUp 20[0-9][0-9]", "SketchUp", "SketchUp.exe"))
        years = [os.path.relpath(path, root).split(os.sep)[0][-4:] for path in found]
    elif sys.platform == "darwin":
        years = [os.path.basename(path)[-4:] for path in glob.glob("/Applications/SketchUp 20[0-9][0-9]")]
    else:
        years = []
    return sorted(set(years)) or DEFAULT_YEARS


def candidate_ports() -> list[int]:
    """Each installed version's instance ports (oldest version first), then LEGACY_PORT."""
    ports = [p for year in installed_years() for p in version_ports(year)]
    return list(dict.fromkeys(ports + [LEGACY_PORT]))


def ranges(ports) -> str:
    """"12601-12609, 9876": runs of consecutive ports, for messages."""
    parts: list[str] = []
    run: list[int] = []
    for port in list(ports) + [None]:
        if run and port == run[-1] + 1:
            run.append(port)
            continue
        if run:
            parts.append(str(run[0]) if len(run) == 1 else "{}-{}".format(run[0], run[-1]))
        run = [port] if port is not None else []
    return ", ".join(parts)


async def is_listening(port: int, timeout: float = 0.5) -> bool:
    """True when something accepts connections on the port (a busy SketchUp still does)."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(config.HOST, port), timeout)
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError):
        return False
    return True


async def listening(ports) -> list[int]:
    """The ports that accept connections, in order (checked together: a closed port takes a while on Windows)."""
    ports = list(ports)
    alive = await asyncio.gather(*(is_listening(p) for p in ports))
    return [p for p, ok in zip(ports, alive) if ok]


async def live_version_ports(version) -> list[int]:
    """The listening instance ports of one SketchUp version."""
    return await listening(version_ports(version))


async def resolve_port(rescan: bool = False):
    """Port for the next connection: SKETCHUP_MCP_PORT, the pinned port, the SketchUp in use, or
    the first candidate that answers (None when none does)."""
    if config.PORT:
        return config.PORT
    if _target["port"] and (_target["pinned"] or not rescan):
        return _target["port"]
    live = await listening(candidate_ports())
    _target["port"] = live[0] if live else None
    return _target["port"]


def pin(port) -> None:
    """Talk to this port until unpin(); None goes back to automatic."""
    _target["port"] = port
    _target["pinned"] = bool(port)


def unpin() -> None:
    pin(None)


def current() -> dict:
    return {"port": config.PORT or _target["port"], "fixed_by_env": bool(config.PORT),
            "pinned": _target["pinned"]}


async def _describe(port: int, timeout: float) -> dict:
    from sketchup_mcp.connection import SketchUpConnection
    from sketchup_mcp.errors import SketchUpError

    entry: dict = {"port": port, "version": None, "title": None, "path": None}
    known = port_version(port)
    if known:
        entry["version"], entry["instance"] = known
    conn = SketchUpConnection(host=config.HOST, port=port, timeout=timeout)
    try:
        await conn.connect()
        entry["server_version"] = conn._server_version
        result = await conn.send_command("get_model_info", {})
        info = json.loads(result["content"][0]["text"])
        entry["title"] = info.get("title")
        entry["path"] = info.get("path") or None
    except (OSError, SketchUpError, KeyError, IndexError, TypeError, ValueError) as e:
        entry["note"] = "listening but did not answer get_model_info (SketchUp busy?): {}".format(e)
    finally:
        await conn.disconnect()
    return entry


async def running_sketchups(timeout: float = 5.0) -> list[dict]:
    """[{port, version, instance, title, path, server_version}] for every candidate port that listens."""
    ports = [config.PORT] if config.PORT else candidate_ports()
    live = await listening(ports)
    return list(await asyncio.gather(*(_describe(p, timeout) for p in live)))
