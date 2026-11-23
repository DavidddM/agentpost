"""The database file: first use, concurrent writers, and files that cannot be used."""

import sqlite3
import time

import anyio
import httpx2
import pytest
from support import bodies, call, titles

pytestmark = pytest.mark.anyio


def test_concurrent_first_use_of_a_new_database(cold_board, tmp_path):
    board = cold_board
    for attempt in range(5):
        board.env["AGENTPOST_DB"] = str(tmp_path / f"fresh{attempt}.sqlite")
        procs = [board.spawn_admin("add", f"dev{i}") for i in range(8)]
        results = [(p.communicate(), p.returncode) for p in procs]
        for (_, stderr), returncode in results:
            assert returncode == 0, stderr
        listing = board.admin("list").stdout
        assert all(f"dev{i}" in listing for i in range(8))


def test_server_and_admin_can_initialise_a_new_database_together(cold_board):
    board = cold_board
    adders = [board.spawn_admin("add", f"dev{i}") for i in range(4)]
    board.start()
    for proc in adders:
        _, stderr = proc.communicate()
        assert proc.returncode == 0, stderr
    assert board.proc.poll() is None
    assert board.admin("list").returncode == 0


def test_database_from_another_schema_version_is_refused_clearly(cold_board):
    board = cold_board
    old = sqlite3.connect(board.db)
    old.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
    old.commit()
    old.close()
    out = board.admin("list")
    assert out.returncode != 0
    assert "different agentpost version" in out.stderr
    assert "Traceback" not in out.stderr


def test_a_file_that_is_not_a_database_is_refused_clearly(cold_board):
    board = cold_board
    board.db.write_bytes(b"this is not an SQLite file, whatever its name says\n" * 4)
    for out in (board.admin("list"), board.admin("add", "ana"), board.run_server()):
        assert out.returncode != 0
        assert f"cannot use the database at {board.db}" in out.stderr
        assert "Traceback" not in out.stderr


def test_a_database_path_that_cannot_be_created_is_refused_clearly(cold_board):
    board = cold_board
    blocker = board.db.parent / "a-file-not-a-folder"
    blocker.write_text("in the way")
    board.env["AGENTPOST_DB"] = str(blocker / "board.sqlite")
    for out in (board.admin("list"), board.run_server()):
        assert out.returncode != 0
        assert "cannot use the database at" in out.stderr
        assert "Traceback" not in out.stderr


def test_adding_the_same_name_twice_at_once_fails_cleanly(board):
    for attempt in range(5):
        procs = [board.spawn_admin("add", f"twin{attempt}") for _ in range(6)]
        results = [(p.communicate(), p.returncode) for p in procs]
        assert sorted(code for _, code in results) == [0, 1, 1, 1, 1, 1]
        for (_, stderr), returncode in results:
            assert "Traceback" not in stderr
            assert returncode == 0 or "already exists" in stderr


async def test_a_stuck_writer_stalls_neither_health_checks_nor_reads(board):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    health_url = f"{board.base}/healthz"

    def timed(fn):
        started = time.monotonic()
        return fn(), time.monotonic() - started

    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        tid = (await call(ana, "start_thread", title="t", body="m1"))["thread_id"]
        lock = sqlite3.connect(board.db, isolation_level=None)
        try:
            lock.execute("UPDATE devs SET last_seen_at = '2000-01-01T00:00:00Z'")
            lock.execute("BEGIN IMMEDIATE")
            statuses: list[int] = []

            async def authenticated_request():
                status = await anyio.to_thread.run_sync(board.post_raw, ana_t)
                statuses.append(status)

            async with anyio.create_task_group() as tg:
                tg.start_soon(authenticated_request)
                await anyio.sleep(0.3)
                health, stall = await anyio.to_thread.run_sync(
                    timed, lambda: httpx2.get(health_url, timeout=30)
                )
            assert health.status_code == 200
            assert stall < 1.5, f"/healthz took {stall:.1f}s behind a locked database"
            assert statuses == [200]

            with anyio.fail_after(3):
                assert titles(await call(beka, "threads")) == ["t"]
                assert bodies(await call(beka, "read", thread_id=tid)) == ["m1"]

            async def blocked_write():
                await call(ana, "reply", thread_id=tid, body="m2")

            async with anyio.create_task_group() as tg:
                tg.start_soon(blocked_write)
                await anyio.sleep(0.5)
                assert bodies(await call(beka, "read", thread_id=tid)) == ["m1"]
                lock.execute("ROLLBACK")
            assert bodies(await call(beka, "read", thread_id=tid)) == ["m1", "m2"]
        finally:
            lock.close()
