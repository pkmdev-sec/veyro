from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from foreman.bridges.opencode import (
    OPENCODE_API_VERSION,
    OPENCODE_VERSION,
    OpenCodeClient,
    OpenCodeError,
)

DEFAULT_EXECUTABLE = Path.home() / ".opencode/bin/opencode"
_LISTENING = re.compile(r"^opencode server listening on (http://127\.0\.0\.1:[0-9]+)$")
_REQUIRED_API = {
    ("/event", "get"),
    ("/session/{sessionID}/prompt_async", "post"),
    ("/permission/{requestID}/reply", "post"),
    ("/session/{sessionID}/abort", "post"),
}


async def run_canary(
    *,
    executable: Path = DEFAULT_EXECUTABLE,
    attachment: bool = False,
) -> dict[str, object]:
    await _require_version(executable)
    username = f"foreman-{secrets.token_hex(8)}"
    password = secrets.token_urlsafe(32)
    env = {
        **os.environ,
        "OPENCODE_SERVER_USERNAME": username,
        "OPENCODE_SERVER_PASSWORD": password,
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
    }
    process: asyncio.subprocess.Process | None = None
    client: OpenCodeClient | None = None
    base_url: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="foreman-sup010-") as directory:
            if attachment:
                env.update(
                    {
                        "HOME": directory,
                        "XDG_CONFIG_HOME": f"{directory}/config",
                        "XDG_DATA_HOME": f"{directory}/data",
                        "XDG_STATE_HOME": f"{directory}/state",
                        "XDG_CACHE_HOME": f"{directory}/cache",
                    }
                )
            process = await asyncio.create_subprocess_exec(
                str(executable),
                "serve",
                "--pure",
                "--hostname",
                "127.0.0.1",
                "--port",
                "0",
                cwd=directory,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            base_url = await _read_server_url(process)
            await _require_unauthorized(base_url)
            client = OpenCodeClient(base_url, username=username, password=password, timeout=5)
            health = await client.health()
            if health != {"healthy": True, "version": OPENCODE_VERSION}:
                raise OpenCodeError("live OpenCode health did not match the pinned version")
            schema = await client.request_json("GET", "/doc")
            _validate_schema(schema)
            stream = client.events(directory=Path(directory))
            first_event = await asyncio.wait_for(anext(stream), timeout=5)
            await stream.aclose()
            if first_event.get("type") != "server.connected":
                raise OpenCodeError("OpenCode SSE did not begin with server.connected")
            if attachment:
                from foreman.agents import AgentId
                from foreman.supervision.attachment_canary import run_canary as verify_existing

                try:
                    session = await client.request_json(
                        "POST", "/session", body={}, directory=Path(directory)
                    )
                    if not isinstance(session, dict) or not isinstance(session.get("id"), str):
                        raise OpenCodeError("empty canary session was not created")
                    result = await verify_existing(
                        provider=AgentId.OPENCODE,
                        repository=Path(directory),
                        selector=session["id"],
                        server=base_url,
                        environment=env,
                    )
                    return {
                        **result,
                        "fixture_session_created": True,
                        "isolated_native_storage": True,
                    }
                finally:
                    await client.close()
                    await _stop_process(process)
                    await _require_port_closed(base_url)
        return {
            "provider": "opencode",
            "version": OPENCODE_VERSION,
            "authenticated": True,
            "api_version": OPENCODE_API_VERSION,
            "sse": "server.connected",
            "session_created": False,
            "prompt_sent": False,
        }
    finally:
        password = ""
        if client is not None:
            await client.close()
        if process is not None:
            await _stop_process(process)
        if base_url is not None:
            await _require_port_closed(base_url)


async def _require_version(executable: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        str(executable),
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    if process.returncode != 0 or stdout.decode(errors="replace").strip() != OPENCODE_VERSION:
        raise OpenCodeError(f"OpenCode {OPENCODE_VERSION} is required")


async def _read_server_url(process: asyncio.subprocess.Process) -> str:
    assert process.stdout is not None
    for _ in range(20):
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        if not line:
            raise OpenCodeError("OpenCode server exited before reporting its loopback URL")
        match = _LISTENING.fullmatch(line.decode(errors="replace").strip())
        if match:
            return match.group(1)
    raise OpenCodeError("OpenCode server did not report its loopback URL")


async def _require_unauthorized(base_url: str) -> None:
    def request() -> int:
        try:
            with urllib.request.urlopen(f"{base_url}/global/health", timeout=3) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    status = await asyncio.to_thread(request)
    if status != 401:
        raise OpenCodeError("OpenCode server accepted an unauthenticated request")


def _validate_schema(value: object) -> None:
    if not isinstance(value, dict):
        raise OpenCodeError("OpenCode /doc response is not an object")
    info = value.get("info")
    if not isinstance(info, dict) or info.get("version") != OPENCODE_API_VERSION:
        raise OpenCodeError("OpenCode API version does not match the pinned schema")
    paths = value.get("paths")
    if not isinstance(paths, dict):
        raise OpenCodeError("OpenCode schema has no paths")
    for path, method in _REQUIRED_API:
        operations = paths.get(path)
        if not isinstance(operations, dict) or method not in operations:
            raise OpenCodeError(f"OpenCode schema is missing {method.upper()} {path}")


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def _require_port_closed(base_url: str) -> None:
    parsed = urllib.parse.urlsplit(base_url)
    assert parsed.hostname is not None and parsed.port is not None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, parsed.port), timeout=1
        )
    except (OSError, TimeoutError):
        return
    writer.close()
    await writer.wait_closed()
    del reader
    raise OpenCodeError("OpenCode canary server port remained open after cleanup")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the no-prompt OpenCode server canary")
    parser.add_argument("--executable", type=Path, default=DEFAULT_EXECUTABLE)
    parser.add_argument(
        "--attachment",
        action="store_true",
        help="Create one empty isolated fixture, then prove read-only attachment",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run_canary(
                    executable=args.executable,
                    attachment=args.attachment,
                )
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
