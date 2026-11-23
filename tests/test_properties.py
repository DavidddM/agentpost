"""Randomised checks of the read-position and reply-size logic.

They run in-process against the storage and reply-trimming functions, with
fixed seeds so a failure can be replayed.
"""

import random

import pydantic_core
import pytest

from agentpost import db, server

DEVS = ("ana", "beka", "gio")
ALPHABETS = (
    "plain ascii words ",
    "ქართული ტექსტი ",
    "漢字かなカナ",
    "😀🎉🚀",
    '"\\\n\t',
    "\u0000\u0007\u001f",
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTPOST_DB", str(tmp_path / "property.sqlite"))
    db.init()
    for name in DEVS:
        db.add_dev(name)


def read_like_the_server(dev: str, thread_id: int, since_id: int, limit: int):
    thread, rows, _ = db.fetch_messages(dev, thread_id, since_id, limit)
    if rows:
        db.mark_read(dev, thread_id, since_id, rows[-1]["id"])
    return [r["id"] for r in rows]


@pytest.mark.parametrize("seed", range(12))
def test_a_message_is_only_marked_read_after_it_was_delivered(store, seed):
    rng = random.Random(seed)
    sender_of: dict[int, str] = {}
    thread_of: dict[int, int] = {}
    delivered = {dev: set() for dev in DEVS}
    thread_ids: list[int] = []

    def check() -> None:
        for dev in DEVS:
            listing, _ = db.list_threads(dev, new_only=False, limit=200)
            for t in listing:
                theirs = {
                    m
                    for m, sender in sender_of.items()
                    if thread_of[m] == t["id"] and sender != dev
                }
                counted_new = {m for m in theirs if m > t["read_id"]}
                assert theirs - delivered[dev] <= counted_new, (dev, t["id"])
                assert t["new"] == len(counted_new)

    for step in range(250):
        dev = rng.choice(DEVS)
        roll = rng.random()
        if not thread_ids or roll < 0.08:
            thread_id, message_id, _ = db.start_thread(dev, "t", "b", [])
            thread_ids.append(thread_id)
            sender_of[message_id], thread_of[message_id] = dev, thread_id
        elif roll < 0.45:
            thread_id = rng.choice(thread_ids)
            message_id, _ = db.reply(dev, thread_id, "b")
            sender_of[message_id], thread_of[message_id] = dev, thread_id
        else:
            thread_id = rng.choice(thread_ids)
            since = rng.choice((0, rng.randint(0, max(sender_of) + 2)))
            got = read_like_the_server(dev, thread_id, since, rng.randint(1, 4))
            delivered[dev].update(got)
        if step % 10 == 0:
            check()
    check()

    for dev in DEVS:
        for t in db.list_threads(dev, new_only=True, limit=200)[0]:
            since = t["read_id"]
            while got := read_like_the_server(dev, t["id"], since, 3):
                since = got[-1]
        assert db.list_threads(dev, new_only=True, limit=200)[0] == []


def random_body(rng: random.Random) -> str:
    alphabet = rng.choice(ALPHABETS)
    body = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6000)))
    while len(pydantic_core.to_json(body)) > server.MAX_MESSAGE_BYTES:
        body = body[: len(body) // 2]
    return body


@pytest.mark.parametrize("seed", range(40))
def test_a_reply_never_exceeds_the_budget_and_wastes_none_of_it(seed):
    rng = random.Random(seed)
    messages = [
        server.Message(
            id=i, sender="ana", at="2026-01-01T00:00:00Z", body=random_body(rng)
        )
        for i in range(1, rng.randint(2, 60))
    ]
    result = server.ThreadMessages(
        thread_id=1,
        title="ქ" * server.MAX_TITLE,
        private=True,
        members=[f"developer-{i}" for i in range(8)],
        started_by="ana",
        messages=list(messages),
        last_id=0,
        has_more=False,
    )

    def size() -> int:
        return len(pydantic_core.to_json(result, indent=2))

    trimmed = server._trim_to_budget(result, result.messages)
    kept = len(result.messages)
    assert size() <= server.MAX_REPLY_BYTES
    assert kept >= 1 and result.messages == messages[:kept]
    assert trimmed == (kept < len(messages))
    if trimmed:
        result.messages.append(messages[kept])
        assert size() > server.MAX_REPLY_BYTES


def test_the_largest_single_item_reply_fits_what_startup_allows_for():
    name, at, big = "n" * 32, "2026-01-01T00:00:00Z", server.SQLITE_MAX_INT
    title = "\x01" * server.MAX_TITLE
    members = [f"{i:02d}" + "n" * 30 for i in range(server.MAX_TO + 1)]
    body = "x" * (server.MAX_MESSAGE_BYTES - 2)
    assert len(pydantic_core.to_json(body)) == server.MAX_MESSAGE_BYTES
    thread = server.ThreadMessages(
        thread_id=big,
        title=title,
        private=True,
        members=members,
        started_by=name,
        messages=[server.Message(id=big, sender=name, at=at, body=body)],
        last_id=big,
        has_more=False,
    )
    summary = server.ThreadSummary(
        thread_id=big,
        title=title,
        private=True,
        members=members,
        started_by=name,
        messages=big,
        new=big,
        new_since_id=big,
        last_from=name,
        last_at=at,
        preview=body + "...",
    )
    listing = server.ThreadList(threads=[summary], has_more=False)
    allowed = server.MAX_MESSAGE_BYTES + server.REPLY_OVERHEAD_BYTES
    for reply in (thread, listing):
        assert len(server._json_bytes(reply)) <= allowed
