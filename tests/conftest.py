"""Fixtures: a real server process per test, with its own database."""

import os
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
SMALL_LIMITS = {
    "AGENTPOST_MAX_MESSAGE_BYTES": "600",
    "AGENTPOST_MAX_REPLY_BYTES": "4000",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Board:
    """One server with its own database, plus the admin command for it."""

    def __init__(self, tmp_path: Path, **env: str) -> None:
        self.port = free_port()
        self.db = tmp_path / "board.sqlite"
        self.env = {
            **os.environ,
            "AGENTPOST_DB": str(self.db),
            "AGENTPOST_HOST": "127.0.0.1",
            "AGENTPOST_PORT": str(self.port),
            "AGENTPOST_PUBLIC_URL": "https://agents.example.com",
            "AGENTPOST_LOG_LEVEL": "warning",
            **env,
        }
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        """Start the server and wait for it; a failed start leaves no process."""
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agentpost.server"], cwd=ROOT, env=self.env
        )
        for _ in range(100):
            code = self.proc.poll()
            if code is not None:
                self.proc = None
                raise RuntimeError(f"server exited with code {code} while starting")
            try:
                if httpx2.get(f"{self.base}/healthz").status_code == 200:
                    return
            except httpx2.HTTPError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=10)
            self.proc = None

    def run_server(self) -> subprocess.CompletedProcess:
        """Run the server until it exits, for a start that is meant to fail."""
        return subprocess.run(
            [sys.executable, "-m", "agentpost.server"],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def spawn_admin(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "agentpost.admin", *args],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def admin(self, *args: str) -> subprocess.CompletedProcess:
        proc = self.spawn_admin(*args)
        out, err = proc.communicate()
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def token(self, cmd: str, name: str) -> str:
        out = self.admin(cmd, name)
        assert out.returncode == 0, out.stderr
        return token_in(out.stdout)

    def post_raw(self, token: str | None) -> int:
        """Status code of a bare tools/list request sent with this token."""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        probe = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        return httpx2.post(f"{self.base}/mcp", json=probe, headers=headers).status_code

    @asynccontextmanager
    async def client(self, token: str, mode: str = "auto", headers: dict | None = None):
        http = create_mcp_http_client(
            headers={"Authorization": f"Bearer {token}", **(headers or {})}
        )
        transport = streamable_http_client(f"{self.base}/mcp", http_client=http)
        async with Client(transport, mode=mode) as c:
            yield c


def token_in(admin_output: str) -> str:
    lines = admin_output.splitlines()
    return next(line.split("token:")[1].strip() for line in lines if "token:" in line)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def board(tmp_path):
    """A running server with small size limits, so paging is cheap to exercise."""
    b = Board(tmp_path, **SMALL_LIMITS)
    b.start()
    yield b
    b.stop()


@pytest.fixture
def default_board(tmp_path):
    """A running server with the limits a real deployment gets."""
    b = Board(tmp_path)
    b.start()
    yield b
    b.stop()


@pytest.fixture
def cold_board(tmp_path):
    """A board whose database does not exist yet and whose server is not started."""
    b = Board(tmp_path)
    yield b
    b.stop()
