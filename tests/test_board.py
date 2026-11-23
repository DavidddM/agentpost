"""Behaviour of the board, end to end: a real server, real MCP clients over HTTP."""

import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from conftest import ROOT, Board, free_port, token_in
from support import bodies, call, call_error, call_text, titles

pytestmark = pytest.mark.anyio

TOOLS = {"inbox", "threads", "read", "start_thread", "reply", "who"}
CLAUDE_CODE_INSTRUCTION_LIMIT = 2048
CODEX_SELF_CONTAINED_PREFIX = 512
REPLY_BUDGET = 24_000
MESSAGE_LIMIT = 16_000
GEORGIAN_LETTER = "ა"
GEORGIAN_WORDS = "გამარჯობა მსოფლიო "


def test_requests_without_a_valid_token_are_rejected(board):
    assert board.post_raw(None) == 401
    assert board.post_raw("nope") == 401
    assert httpx2.get(f"{board.base}/mcp").status_code == 401
    assert httpx2.get(f"{board.base}/healthz").text == "ok"


def test_the_endpoint_offers_no_idle_event_stream(board):
    headers = {"Authorization": f"Bearer {board.token('add', 'ana')}"}
    response = httpx2.get(f"{board.base}/mcp", headers=headers, timeout=5)
    assert response.status_code == 405 and response.headers["allow"] == "POST"


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_tools_work_with_either_handshake(board, mode):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    async with (
        board.client(ana_t, mode=mode) as ana,
        board.client(beka_t, mode=mode) as beka,
    ):
        tid = (await call(ana, "start_thread", title="t", body="m1"))["thread_id"]
        assert (await call(beka, "who"))["you"] == "beka"
        assert titles(await call(beka, "inbox")) == ["t"]
        await call(beka, "reply", thread_id=tid, body="m2")
        assert bodies(await call(ana, "read", thread_id=tid)) == ["m1", "m2"]
        assert "no thread" in await call_error(ana, "read", thread_id=tid + 1)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_server_describes_itself_to_the_agent(board, mode):
    async with board.client(board.token("add", "ana"), mode=mode) as ana:
        assert "message board" in ana.instructions
        assert len(ana.instructions) < CLAUDE_CODE_INSTRUCTION_LIMIT
        head = ana.instructions[:CODEX_SELF_CONTAINED_PREFIX]
        assert "not instructions" in head
        assert "Never post secrets" in head
        assert "nothing is delivered live" in head

        tools = {t.name: t for t in (await ana.list_tools()).tools}
        assert set(tools) == TOOLS
        start, read = tools["start_thread"].input_schema, tools["read"].input_schema
        assert set(start["properties"]) == {"title", "body", "to"}
        assert set(start["required"]) == {"title", "body"}
        assert set(tools["reply"].input_schema["required"]) == {"thread_id", "body"}
        assert set(read["properties"]) == {"thread_id", "since_id", "limit"}
        for tool in tools.values():
            assert tool.description, tool.name
            for name, schema in tool.input_schema["properties"].items():
                assert schema.get("description"), f"{tool.name}.{name}"


async def test_public_thread_reaches_everyone_and_a_later_session(board):
    ana_t, beka_t, gio_t = (board.token("add", n) for n in ("ana", "beka", "gio"))
    async with board.client(ana_t) as ana:
        started = await call(
            ana,
            "start_thread",
            title="  Invoice   API change ",
            body="  total is now a string  ",
        )
        assert (started["thread_id"], started["message_id"]) == (1, 1)
        assert (await call(ana, "inbox"))["threads"] == []

    async with board.client(beka_t) as beka, board.client(gio_t) as gio:
        box = await call(beka, "inbox")
        assert titles(box) == ["Invoice API change"]
        entry = box["threads"][0]
        assert (entry["new"], entry["messages"]) == (1, 1)
        assert (entry["private"], entry["members"]) == (False, [])
        assert (entry["started_by"], entry["last_from"]) == ("ana", "ana")
        assert entry["preview"] == "total is now a string"
        assert titles(await call(beka, "inbox")) == ["Invoice API change"]

        thread = await call(beka, "read", thread_id=1)
        assert bodies(thread) == ["total is now a string"]
        assert thread["title"] == "Invoice API change"
        assert (await call(beka, "inbox"))["threads"] == []
        assert titles(await call(gio, "inbox")) == ["Invoice API change"]

        await call(gio, "reply", thread_id=1, body="ack, updating the connector")
        entry = (await call(beka, "inbox"))["threads"][0]
        assert (entry["new"], entry["last_from"]) == (1, "gio")
        new = await call(beka, "read", thread_id=1, since_id=entry["new_since_id"])
        assert bodies(new) == ["ack, updating the connector"]

    async with board.client(beka_t) as beka:
        assert (await call(beka, "inbox"))["threads"] == []
        assert titles(await call(beka, "threads")) == ["Invoice API change"]
        assert bodies(await call(beka, "read", thread_id=1)) == [
            "total is now a string",
            "ack, updating the connector",
        ]


async def test_private_thread_is_hidden_from_everyone_else(board):
    names = ("ana", "beka", "gio", "nika")
    ana_t, beka_t, gio_t, nika_t = (board.token("add", n) for n in names)
    async with (
        board.client(ana_t) as ana,
        board.client(beka_t) as beka,
        board.client(gio_t) as gio,
        board.client(nika_t) as nika,
    ):
        pub = (await call(ana, "start_thread", title="public", body="for all"))[
            "thread_id"
        ]
        duo = (
            await call(ana, "start_thread", title="duo", body="only beka", to=["Beka"])
        )["thread_id"]
        trio = (
            await call(
                beka, "start_thread", title="trio", body="three", to=["ana", "gio"]
            )
        )["thread_id"]

        assert titles(await call(beka, "inbox")) == ["duo", "public"]
        assert titles(await call(beka, "threads")) == ["trio", "duo", "public"]
        assert titles(await call(gio, "threads")) == ["trio", "public"]
        assert titles(await call(nika, "threads")) == ["public"]
        assert titles(await call(nika, "inbox")) == ["public"]

        listed = {t["title"]: t for t in (await call(ana, "threads"))["threads"]}
        assert listed["duo"]["private"] is True
        assert listed["duo"]["members"] == ["ana", "beka"]
        assert listed["trio"]["members"] == ["ana", "beka", "gio"]

        for outsider, tid in ((gio, duo), (nika, duo), (nika, trio)):
            wanted = f"no thread with id {tid}"
            assert wanted in await call_error(outsider, "read", thread_id=tid)
            assert wanted in await call_error(
                outsider, "reply", thread_id=tid, body="let me in"
            )
        missing = await call_error(nika, "read", thread_id=999)
        hidden = await call_error(nika, "read", thread_id=duo)
        assert missing.replace("999", "N") == hidden.replace(str(duo), "N")
        raw = await call_text(nika, "threads")
        assert "duo" not in raw and "only beka" not in raw and "three" not in raw

        await call(beka, "reply", thread_id=duo, body="got it")
        assert bodies(await call(ana, "read", thread_id=duo)) == ["only beka", "got it"]
        await call(gio, "reply", thread_id=trio, body="here")
        assert bodies(await call(ana, "read", thread_id=trio)) == ["three", "here"]
        assert bodies(await call(nika, "read", thread_id=pub)) == ["for all"]


async def test_read_position_is_per_thread(board):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        a = (await call(ana, "start_thread", title="shopify", body="a1"))["thread_id"]
        b = (await call(ana, "start_thread", title="geoloc", body="b1"))["thread_id"]
        await call(beka, "read", thread_id=a)
        assert titles(await call(beka, "inbox")) == ["geoloc"]

        await call(ana, "reply", thread_id=a, body="a2")
        box = await call(beka, "inbox")
        assert titles(box) == ["shopify", "geoloc"]
        assert [t["new"] for t in box["threads"]] == [1, 1]

        await call(beka, "read", thread_id=b)
        assert titles(await call(beka, "inbox")) == ["shopify"]


async def test_skipping_ahead_leaves_the_skipped_messages_unread(board):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        tid = (await call(ana, "start_thread", title="t", body="m1"))["thread_id"]
        await call(beka, "read", thread_id=tid)
        for body in ("m2", "m3", "m4"):
            await call(ana, "reply", thread_id=tid, body=body)

        assert bodies(await call(beka, "read", thread_id=tid, since_id=3)) == ["m4"]
        entry = (await call(beka, "inbox"))["threads"][0]
        assert (entry["new"], entry["new_since_id"]) == (3, 1)

        page = await call(beka, "read", thread_id=tid, since_id=entry["new_since_id"])
        assert bodies(page) == ["m2", "m3", "m4"]
        assert (await call(beka, "inbox"))["threads"] == []


async def test_polling_after_your_own_post_marks_the_answer_read(board):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        tid = (await call(ana, "start_thread", title="t", body="q"))["thread_id"]
        await call(beka, "read", thread_id=tid)
        mine = (await call(beka, "reply", thread_id=tid, body="a"))["message_id"]
        await call(ana, "reply", thread_id=tid, body="thanks")

        page = await call(beka, "read", thread_id=tid, since_id=mine)
        assert bodies(page) == ["thanks"]
        assert (await call(beka, "inbox"))["threads"] == []


async def test_read_position_ignores_message_ids_of_other_threads(board):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        a = (await call(ana, "start_thread", title="a", body="a1"))["thread_id"]
        b = (await call(ana, "start_thread", title="b", body="b1"))["thread_id"]
        await call(beka, "read", thread_id=a)
        other = (await call(ana, "reply", thread_id=b, body="b2"))["message_id"]
        await call(ana, "reply", thread_id=a, body="a2")

        page = await call(beka, "read", thread_id=a, since_id=other)
        assert bodies(page) == ["a2"]
        assert titles(await call(beka, "inbox")) == ["b"]


async def test_sender_comes_from_the_token_not_from_the_caller(board):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    forged = {"title": "t", "body": "hi", "sender": "beka", "started_by": "beka"}
    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        result = await ana.call_tool("start_thread", forged)
        if not result.is_error:
            thread = await call(beka, "read", thread_id=1)
            assert thread["started_by"] == "ana"
            assert thread["messages"][0]["sender"] == "ana"
        assert (await call(ana, "who"))["you"] == "ana"
        assert (await call(beka, "who"))["you"] == "beka"


async def test_validation(board):
    ana_t = board.token("add", "ana")
    board.token("add", "beka")

    async def start(**args):
        return await call_error(
            ana, "start_thread", **{"title": "t", "body": "b"} | args
        )

    async with board.client(ana_t) as ana:
        assert "body is empty" in await start(body="   ")
        assert "title is empty" in await start(title="  ")
        assert "limit is 120" in await start(title="x" * 121)
        assert "limit is 600" in await start(body="x" * 601)
        assert "no developer named 'nobody'" in await start(to=["beka", "nobody"])
        assert "yourself" in await start(to=["ana"])
        assert "`to` is empty" in await start(to=[])
        crowd = [f"dev{i}" for i in range(17)]
        assert "limit is 16" in await start(to=crowd)
        assert "no developer named" in await start(to=crowd[:16])
        assert (await call(ana, "threads"))["threads"] == []

        tid = (await call(ana, "start_thread", title="t", body="b"))["thread_id"]
        assert "body is empty" in await call_error(
            ana, "reply", thread_id=tid, body=" "
        )
        assert "limit is 600" in await call_error(
            ana, "reply", thread_id=tid, body="x" * 601
        )
        await call_error(ana, "read", thread_id=tid, limit=0)
        await call_error(ana, "read", thread_id=tid, limit=201)
        await call_error(ana, "read", thread_id=tid, since_id=-1)
        await call_error(ana, "inbox", limit=0)
        await call_error(ana, "threads", offset=-1)
        assert bodies(await call(ana, "read", thread_id=tid)) == ["b"]


async def test_blank_name_in_to_never_makes_a_thread_public(board):
    ana_t = board.token("add", "ana")
    board.token("add", "beka")
    async with board.client(ana_t) as ana:
        for to in ([""], [" "], ["beka", ""], ["  ", "beka"]):
            error = await call_error(
                ana, "start_thread", title="secret", body="b", to=to
            )
            assert "blank" in error, to
        assert (await call(ana, "threads"))["threads"] == []


async def test_absurd_ids_get_the_ordinary_answer(board):
    ana_t = board.token("add", "ana")
    huge = 2**70
    async with board.client(ana_t) as ana:
        tid = (await call(ana, "start_thread", title="t", body="m1"))["thread_id"]
        for bad in (huge, 0, -1):
            wanted = f"no thread with id {bad}"
            assert wanted in await call_error(ana, "read", thread_id=bad)
            assert wanted in await call_error(ana, "reply", thread_id=bad, body="x")
        assert bodies(await call(ana, "read", thread_id=tid, since_id=huge)) == []
        assert (await call(ana, "threads", offset=huge))["threads"] == []


async def test_long_thread_comes_back_in_parts(board):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        tid = (await call(ana, "start_thread", title="long", body="m0"))["thread_id"]
        for i in range(1, 5):
            await call(ana, "reply", thread_id=tid, body=f"m{i}")

        page = await call(beka, "read", thread_id=tid, limit=2)
        assert bodies(page) == ["m0", "m1"] and page["has_more"] is True
        assert (await call(beka, "inbox"))["threads"][0]["new"] == 3
        page = await call(
            beka, "read", thread_id=tid, since_id=page["last_id"], limit=2
        )
        assert bodies(page) == ["m2", "m3"] and page["has_more"] is True
        page = await call(
            beka, "read", thread_id=tid, since_id=page["last_id"], limit=2
        )
        assert bodies(page) == ["m4"] and page["has_more"] is False
        assert (await call(beka, "inbox"))["threads"] == []
        end = await call(beka, "read", thread_id=tid, since_id=page["last_id"])
        assert bodies(end) == [] and end["has_more"] is False
        assert end["last_id"] == page["last_id"]

        big = (await call(ana, "start_thread", title="big", body="a" * 500))[
            "thread_id"
        ]
        for letter in "bcdefghijkl":
            await call(ana, "reply", thread_id=big, body=letter * 500)
        seen, since, pages = [], 0, 0
        while True:
            text = await call_text(beka, "read", thread_id=big, since_id=since)
            assert len(text.encode()) <= 4000
            page = await call(beka, "read", thread_id=big, since_id=since)
            seen += [b[0] for b in bodies(page)]
            since, pages = page["last_id"], pages + 1
            if not page["has_more"]:
                break
        assert seen == list("abcdefghijkl") and pages > 1
        assert (await call(beka, "inbox"))["threads"] == []


async def test_non_latin_thread_stays_under_the_reply_budget(default_board):
    board = default_board
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    body = (GEORGIAN_WORDS * 300)[:5000]
    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        tid = (await call(ana, "start_thread", title="ქართული", body=body))["thread_id"]
        for _ in range(3):
            await call(ana, "reply", thread_id=tid, body=body)

        received, since, pages = 0, 0, 0
        while True:
            text = await call_text(beka, "read", thread_id=tid, since_id=since)
            assert len(text.encode()) <= REPLY_BUDGET
            page = await call(beka, "read", thread_id=tid, since_id=since)
            assert all(b == body.strip() for b in bodies(page))
            received, since, pages = (
                received + len(bodies(page)),
                page["last_id"],
                pages + 1,
            )
            if not page["has_more"]:
                break
        assert received == 4 and pages > 1


async def test_message_size_is_limited_in_bytes_not_characters(default_board):
    board = default_board
    ana_t = board.token("add", "ana")
    async with board.client(ana_t) as ana:
        too_big = await call_error(
            ana, "start_thread", title="t", body=GEORGIAN_LETTER * 6000
        )
        assert "bytes" in too_big and str(MESSAGE_LIMIT) in too_big
        assert "number the parts" in too_big
        tid = (await call(ana, "start_thread", title="t", body=GEORGIAN_LETTER * 5000))[
            "thread_id"
        ]
        await call(ana, "reply", thread_id=tid, body="x" * 15_000)
        assert "bytes" in await call_error(
            ana, "reply", thread_id=tid, body="x" * (MESSAGE_LIMIT + 1)
        )
        escaped = "a\n" * 6000
        assert len(escaped) < MESSAGE_LIMIT
        assert "bytes" in await call_error(ana, "reply", thread_id=tid, body=escaped)


async def test_thread_listing_pages(board):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        for i in range(5):
            await call(ana, "start_thread", title=f"t{i}", body="x" * 300)
        first = await call(beka, "threads", limit=2)
        assert titles(first) == ["t4", "t3"] and first["has_more"] is True
        assert titles(await call(beka, "threads", limit=2, offset=2)) == ["t2", "t1"]
        last = await call(beka, "threads", limit=2, offset=4)
        assert titles(last) == ["t0"] and last["has_more"] is False
        box = await call(beka, "inbox", limit=3)
        assert titles(box) == ["t4", "t3", "t2"] and box["has_more"] is True
        assert box["threads"][0]["preview"] == "x" * 200 + "..."


async def test_listings_stay_under_the_reply_budget(default_board):
    board = default_board
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        for i in range(60):
            title = f"{i:02d} " + GEORGIAN_LETTER * 110
            await call(ana, "start_thread", title=title, body=GEORGIAN_LETTER * 400)

        text = await call_text(beka, "inbox", limit=200)
        assert len(text.encode()) <= REPLY_BUDGET
        box = await call(beka, "inbox", limit=200)
        assert box["has_more"] is True and 1 < len(box["threads"]) < 60

        seen: list[str] = []
        while True:
            text = await call_text(beka, "threads", limit=200, offset=len(seen))
            assert len(text.encode()) <= REPLY_BUDGET
            page = await call(beka, "threads", limit=200, offset=len(seen))
            seen += [t["title"][:2] for t in page["threads"]]
            if not page["has_more"]:
                break
        assert seen == [f"{i:02d}" for i in reversed(range(60))]


async def test_preview_is_not_cut_at_a_nul_character(board):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    async with board.client(ana_t) as ana, board.client(beka_t) as beka:
        await call(ana, "start_thread", title="mid", body="abc\x00def")
        await call(ana, "start_thread", title="lead", body="\x00starts with one")
        box = (await call(beka, "inbox"))["threads"]
        previews = {t["title"]: t["preview"] for t in box}
        assert previews == {"mid": "abc\x00def", "lead": "\x00starts with one"}


async def test_a_developer_who_joins_later_sees_history_but_not_as_new(board):
    ana_t = board.token("add", "ana")
    async with board.client(ana_t) as ana:
        await call(ana, "start_thread", title="old", body="before nika joined")
        nika_t = board.token("add", "nika")
        async with board.client(nika_t) as nika:
            assert (await call(nika, "inbox"))["threads"] == []
            assert titles(await call(nika, "threads")) == ["old"]
            assert bodies(await call(nika, "read", thread_id=1)) == [
                "before nika joined"
            ]
            await call(ana, "reply", thread_id=1, body="after nika joined")
            box = await call(nika, "inbox")
            assert titles(box) == ["old"] and box["threads"][0]["new"] == 1


async def test_who_revoke_and_rotate(board):
    ana_t, beka_t = board.token("add", "ana"), board.token("add", "beka")
    board.token("add", "gio")
    async with board.client(ana_t) as ana:
        who = await call(ana, "who")
        assert [d["name"] for d in who["devs"]] == ["ana", "beka", "gio"]
        seen = {d["name"]: d["last_seen"] for d in who["devs"]}
        assert seen["ana"] and seen["beka"] is None

        async with board.client(beka_t) as beka:
            await call(beka, "start_thread", title="from beka", body="hello")
        assert board.admin("revoke", "beka").returncode == 0
        assert board.post_raw(beka_t) == 401
        assert [d["name"] for d in (await call(ana, "who"))["devs"]] == ["ana", "gio"]
        assert "no developer named 'beka'" in await call_error(
            ana, "start_thread", title="t", body="hi", to=["beka"]
        )
        assert bodies(await call(ana, "read", thread_id=1)) == ["hello"]

        new_t = board.token("rotate", "beka")
        assert new_t != beka_t
        async with board.client(new_t) as beka:
            assert (await call(beka, "who"))["you"] == "beka"
        assert board.post_raw(beka_t) == 401


def test_admin_command(board):
    out = board.admin("add", "Nika")
    assert out.returncode == 0
    url = "https://agents.example.com/mcp"
    claude = f'claude mcp add --transport http agentpost {url} --header "Authorization'
    codex = f"codex mcp add agentpost --url {url} --bearer-token-env-var"
    assert claude in out.stdout and codex in out.stdout
    assert board.admin("add", "nika").returncode == 1
    assert board.admin("add", "bad name!").returncode == 1
    assert board.admin("revoke", "ghost").returncode == 1
    assert board.admin("rotate", "ghost").returncode == 1
    listing = board.admin("list").stdout
    assert "nika" in listing and "active" in listing
    assert token_in(out.stdout).encode() not in Path(board.db).read_bytes()


@pytest.mark.parametrize("suffix", ["", "/", "/mcp", "/mcp/"])
def test_join_commands_name_the_endpoint_once(cold_board, suffix):
    board = cold_board
    board.env["AGENTPOST_PUBLIC_URL"] = "https://agents.example.com" + suffix
    out = board.admin("add", "nika").stdout
    assert out.count(" https://agents.example.com/mcp ") == 2
    assert "/mcp/" not in out


async def test_works_behind_a_public_hostname(board):
    ana_t = board.token("add", "ana")
    tunnel = {"Host": "agents.example.com"}
    async with board.client(ana_t, headers=tunnel) as ana:
        assert (await call(ana, "who"))["you"] == "ana"


async def test_messages_and_clients_survive_a_restart(board):
    ana_t = board.token("add", "ana")
    async with board.client(ana_t) as ana:
        await call(ana, "start_thread", title="durable", body="before restart")
        board.stop()
        board.start()
        assert bodies(await call(ana, "read", thread_id=1)) == ["before restart"]
        assert titles(await call(ana, "threads")) == ["durable"]


def test_healthcheck_follows_the_configured_port(board):
    def healthcheck(port: int) -> int:
        env = {**board.env, "AGENTPOST_PORT": str(port)}
        return subprocess.run(
            [sys.executable, "-m", "agentpost.healthcheck"], cwd=ROOT, env=env
        ).returncode

    assert healthcheck(board.port) == 0
    assert healthcheck(free_port()) == 1


def start_misconfigured(tmp_path, **env: str) -> subprocess.CompletedProcess:
    return Board(tmp_path, **env).run_server()


def test_limits_that_could_strand_a_message_are_refused_at_startup(tmp_path):
    out = start_misconfigured(tmp_path, AGENTPOST_MAX_MESSAGE_BYTES="23000")
    assert out.returncode != 0
    assert "AGENTPOST_MAX_REPLY_BYTES" in out.stderr
    assert "Traceback" not in out.stderr


@pytest.mark.parametrize(
    "name",
    ["AGENTPOST_MAX_MESSAGE_BYTES", "AGENTPOST_MAX_REPLY_BYTES", "AGENTPOST_PORT"],
)
def test_a_setting_that_is_not_a_number_is_refused_clearly(tmp_path, name):
    out = start_misconfigured(tmp_path, **{name: "lots"})
    assert out.returncode != 0
    assert f"{name} must be a whole number" in out.stderr
    assert "Traceback" not in out.stderr
