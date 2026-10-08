import json, time
from pathlib import Path
from types import SimpleNamespace
import pytest

from walden.config import Config
from walden.adapters.local import LocalBlobStore, SQLiteRegistry, NullSemanticIndex
from walden.adapters.bindings import SQLiteEpisodeIndex
from walden.memory_graph import MemoryGraph
from walden.agent import WaldenAgent
from walden.checkpoint import Checkpointer
from walden.archive import Archiver, ArchiveIndex, Message, segment, derive_key, new_salt, decode
from walden.llm import LLM


class FactsLLM(LLM):
    """Extraction: every human message 'I <x>' becomes the fact '<x>' about its sender."""
    async def chat(self, messages, json_schema=None):
        if json_schema is None: return "merged summary."
        text = messages[-1]["content"]
        if "memory" in json_schema.get("properties", {}):
            transcript = text.split("TRANSCRIPT:\n")[1].split("\n\nKNOWN FACTS")[0]
            new = transcript.split("NEW MESSAGES (take facts only from here):\n")[-1]
            sec = {}
            for line in new.splitlines():
                if line.startswith("[P") and "] I " in line:
                    alias = line[1:].split(" ")[0].rstrip("]")
                    sec.setdefault(alias, {"add": [], "update": [], "retract": []})["add"].append({"fact": line.split("] I ", 1)[1], "said_by": alias})
            for k in json_schema["properties"]["memory"]["properties"]:
                sec.setdefault(k, {"add": [], "update": [], "retract": []})
            return json.dumps({"episode_summary": f"talked ({len(new.splitlines())} lines).", "episode_topics": ["t"], "memory": sec})
        return json.dumps({"facts": []})


class FakeMemWal:
    def __init__(self): self.store = {}
    async def remember_and_wait(self, text, namespace=None, timeout_ms=0):
        b = f"MW{len(self.store)}"; self.store[b] = (namespace, text)
        return SimpleNamespace(id=b, blob_id=b, owner="0xO", namespace=namespace)
    async def recall(self, params):
        return SimpleNamespace(results=[SimpleNamespace(blob_id=b, text=t, distance=0.1) for b, (ns, t) in self.store.items() if ns == params.namespace])
    async def list_namespaces(self):
        names = sorted({ns for ns, _ in self.store.values()})
        return SimpleNamespace(namespaces=[SimpleNamespace(name=n, memory_count=sum(1 for x, _ in self.store.values() if x == n)) for n in names])


def setup(tmp_path):
    cfg = Config()
    cfg.memory.identity_hmac_key = "identity-test"; cfg.memory.encryption_key = "encryption-test"
    cfg.memory.walrus.local_dir = str(tmp_path / "blobs"); cfg.memory.sui.local_db = str(tmp_path / "db.sqlite")
    cfg.memory.checkpoint.memwal_key = "k"; cfg.memory.checkpoint.memwal_account_id = "0xA"
    cfg.memory.consolidation.deep_rounds = 0
    cfg.matrix.user_id = "@walden:x"
    reg = SQLiteRegistry(cfg.memory.sui.local_db)
    graph = MemoryGraph(cfg, LocalBlobStore(cfg.memory.walrus.local_dir), reg, reg, NullSemanticIndex(), SQLiteEpisodeIndex(cfg.memory.sui.local_db))
    agent = WaldenAgent(cfg, graph, FactsLLM())
    cp = Checkpointer(cfg, graph); cp._mw = FakeMemWal()
    walrus = []
    async def run(*args, timeout=300):
        req = json.loads(args[2]); store = req["command"]["store"]
        assert req["context"] == "mainnet"
        if store.get("dryRun"): return json.dumps([{"storageCost": 1000}]).encode()
        walrus.append(open(store["files"][0], "rb").read())
        return json.dumps([{"blobStoreResult": {"newlyCreated": {"blobObject": {"blobId": f"FILE{len(walrus)}", "id": f"0xF{len(walrus)}"}}}}]).encode()
    cp._run = run
    archiver = Archiver(cfg, agent, cp, ArchiveIndex(cfg.memory.sui.local_db))
    return cfg, graph, agent, archiver, walrus


def msgs(start, n, file_at=None):
    out = []
    for i in range(start, start + n):
        who = "@ana:x" if i % 2 else "@ben:x"
        m = Message(f"$e{i}", who, "Ana" if i % 2 else "Ben", 1_790_000_000_000 + i * 60_000, f"I like thing {i}")
        if i == file_at: m.file = {"name": "photo.jpg", "mimetype": "image/jpeg", "size": 5, "sha256": "x"}; m.data = b"\xff\xd8IMG"
        out.append(m)
    return out


def test_episodes_split_on_pauses_and_size():
    ms = msgs(0, 10)
    ms[5].ts += 7 * 3600_000; [setattr(m, "ts", m.ts + 7 * 3600_000) for m in ms[6:]]
    assert [len(e) for e in segment(ms, max_messages=80)] == [5, 5]
    assert [len(e) for e in segment(msgs(0, 10), max_messages=4)] == [4, 4, 2]


@pytest.mark.asyncio
async def test_archive_round_trip_and_wrong_password(tmp_path: Path):
    cfg, graph, agent, archiver, walrus = setup(tmp_path)
    salt = new_salt(); key = derive_key("hunter2", salt)
    res = await archiver.run(room_id="!r:x", room_name="Old room", messages=msgs(0, 6, file_at=2), password_key=key, salt=salt)
    assert (res.seq, res.episodes, res.people, res.files) == (1, 1, 2, 1) and res.facts == 6
    assert b"\xff\xd8IMG" not in walrus[0]                                   # files are encrypted before upload
    stored = archiver.cp._mw.store
    assert all(b"I like thing" not in t.encode() for _, t in stored.values())  # Walrus Memory only sees ciphertext
    header, _ = decode(stored[res.manifest_blob][1].encode())
    assert header["kind"] == "manifest" and header["kdf"]["alg"] == "scrypt"

    hdr, payload = await archiver.load_payload(res.manifest_blob, "hunter2")
    ana = await graph.ensure_person("@ana:x")
    assert sorted(f["fact"] for f in payload["people"][ana]["facts"]) == ["like thing 1", "like thing 3", "like thing 5"]
    assert payload["episodes"][0]["summary"] and hdr["archive"] and hdr["files"] == 1
    assert all(f["episode"] == payload["episodes"][0]["ref"] for f in payload["people"][ana]["facts"])   # facts point at their episode
    with pytest.raises(ValueError, match="wrong password"):
        await archiver.load_payload(res.manifest_blob, "nope")


@pytest.mark.asyncio
async def test_re_archiving_continues_and_links_back(tmp_path: Path):
    from walden.archive import open_piece
    cfg, graph, agent, archiver, walrus = setup(tmp_path)
    salt = new_salt(); key = derive_key("pw", salt)
    first = await archiver.run(room_id="!r:x", room_name="R", messages=msgs(0, 4), password_key=key, salt=salt)
    room_ref = await graph.ensure_room("!r:x")
    assert archiver.index.latest(room_ref)["last_event_id"] == "$e3"
    second = await archiver.run(room_id="!r:x", room_name="R", messages=msgs(4, 4), password_key=key, salt=salt)
    assert second.seq == 2
    store = archiver.cp._mw.store
    _, manifest = open_piece(store[second.manifest_blob][1].encode(), "pw")
    assert manifest["previous_manifest"] == first.manifest_blob and manifest["range"]["first_event"] == "$e4"
    _, first_manifest = open_piece(store[first.manifest_blob][1].encode(), "pw")
    _, ep = open_piece(store[manifest["episodes"][0]["blob"]][1].encode(), "pw")
    assert ep["previous_episode"] == first_manifest["episodes"][-1]["blob"]          # the episode chain runs across archives
    ana = await graph.ensure_person("@ana:x")
    _, person = open_piece(store[next(p["blob"] for p in manifest["people"] if p["ref"] == ana)][1].encode(), "pw")
    assert person["previous"] == next(p["blob"] for p in first_manifest["people"] if p["ref"] == ana)


def _service(tmp_path):
    from walden.matrix_service import MatrixService
    cfg, graph, agent, archiver, walrus = setup(tmp_path)
    svc = MatrixService(cfg, agent); svc.checkpointer = archiver.cp
    async def days(ctx): return 14.0
    svc.checkpointer.epoch_days_on = days
    sent = []; left = []
    async def send(room_id, text): sent.append((room_id, text))
    async def leave(rid): left.append(rid)
    svc._send = send
    users = {"@ana:x": None, "@ben:x": None, "@walden:x": None}
    svc.client = SimpleNamespace(rooms={"!t:x": SimpleNamespace(display_name="Target", users=users,
                                                                user_name=lambda u: {"@ana:x": "Ana", "@ben:x": "Ben"}.get(u))},
                                 room_leave=leave)
    job = {"id": "j1", "room_id": "!t:x", "room_name": "Target", "requester": "@ana:x", "requester_room": "!w3:x", "deadline": None,
           "state": "proposed", "included": set(), "key_enc": svc._master().encrypt(derive_key("pw", b"s" * 16).encode()).decode(),
           "salt": (b"s" * 16).hex(), "estimate_frost": 1000, "created_at": "now"}
    return svc, sent, left, job


@pytest.mark.asyncio
async def test_opt_in_notice_include_and_scheduled_run(tmp_path: Path):
    from walden.matrix_archive import FAREWELL
    svc, sent, left, job = _service(tmp_path)
    await svc._archive_confirm(SimpleNamespace(room_id="!w3:x"), {"job": job, "hours": 24})
    notice = sent[0][1]
    assert sent[0][0] == "!t:x" and notice.startswith("Hello, I am Walden") and "!walden include" in notice
    assert "direct message to Ana (@ana:x)" in notice and "files paid for until" in notice and "Nothing of anyone who does not reply" in notice
    room = SimpleNamespace(room_id="!t:x")
    assert "will be part of the archive" in await svc._include_command(room, SimpleNamespace(sender="@ana:x"), include=True)
    await svc._include_command(room, SimpleNamespace(sender="@ben:x"), include=True)
    assert "nothing of yours" in await svc._include_command(room, SimpleNamespace(sender="@ben:x"), include=False)
    [j] = svc.archives.jobs("scheduled", "!t:x")
    assert j["included"] == {"@ana:x"}

    async def history(rid, after, only, with_files=True):
        return [m for m in msgs(0, 6) if m.sender in only], {"undecryptable": 0, "skipped_files": 0, "excluded": 3}
    svc._history = history
    await svc._archive_run(j)
    in_room = [t for r, t in sent if r == "!t:x"]
    assert "has been archived as MW" in in_room[-2] and "1 people who opted in" in in_room[-2] and "walruscan.com/mainnet/blob/MW" in in_room[-2]
    assert in_room[-1] == FAREWELL and left == ["!t:x"]
    [done] = svc.archives.jobs("done")
    assert done["key_enc"] is None
    assert [t for r, t in sent if r == "!w3:x"][-1].startswith("Archived Target as MW")


@pytest.mark.asyncio
async def test_nobody_opted_in_stores_nothing(tmp_path: Path):
    from walden.matrix_archive import FAREWELL
    svc, sent, left, job = _service(tmp_path)
    await svc._archive_confirm(SimpleNamespace(room_id="!w3:x"), {"job": job, "hours": 1})
    [j] = svc.archives.jobs("scheduled")
    await svc._archive_run(j)
    assert [t for r, t in sent if r == "!t:x"][-2:] == ["Nobody asked to be included, so nothing from this room has been stored.", FAREWELL]
    assert left == ["!t:x"] and svc.checkpointer._mw.store == {}


@pytest.mark.asyncio
async def test_history_keeps_included_people_and_walden_replies_to_them(tmp_path: Path):
    from nio import RoomMessageText
    svc, sent, left, job = _service(tmp_path)
    def ev(i, who, body):
        return RoomMessageText.from_dict({"type": "m.room.message", "event_id": f"$h{i}", "sender": who, "origin_server_ts": 1_790_000_000_000 + i,
                                          "content": {"msgtype": "m.text", "body": body}})
    timeline = [ev(1, "@ana:x", "I study bees"), ev(2, "@walden:x", "Bees are wonderful."), ev(3, "@ben:x", "I hate bees"),
                ev(4, "@walden:x", "Ben, that is a pity."), ev(5, "@ana:x", "!walden memory"), ev(6, "@walden:x", "What I know about you in this room ...")]
    async def room_messages(rid, start=None, direction=None, limit=100):
        return SimpleNamespace(chunk=list(reversed(timeline)), end=None)
    svc.client.room_messages = room_messages; svc.client.next_batch = "t"
    msgs_, stats = await svc._history("!t:x", None, {"@ana:x"})
    assert [m.body for m in msgs_] == ["I study bees", "Bees are wonderful."]       # Ben, Walden's reply to Ben, commands and listings left out
    everyone, _ = await svc._history("!t:x", None, None)
    assert [m.body for m in everyone] == ["I study bees", "Bees are wonderful.", "I hate bees", "Ben, that is a pity."]


@pytest.mark.asyncio
async def test_target_room_by_name(tmp_path: Path):
    svc, sent, left, job = _service(tmp_path)
    svc.client.rooms["!u:x"] = SimpleNamespace(display_name="UdK2300", users={"@ana:x": None})
    assert await svc._join("udk 2300") == "!u:x"
    with pytest.raises(RuntimeError, match="not in a room called Nowhere"):
        await svc._join("Nowhere")


@pytest.mark.asyncio
async def test_history_page_retries_smaller_after_a_broken_response(tmp_path: Path, monkeypatch):
    import asyncio as aio
    svc, sent, left, job = _service(tmp_path)
    limits = []
    async def room_messages(rid, start=None, direction=None, limit=100):
        limits.append(limit)
        if len(limits) < 3: raise RuntimeError("Response payload is not completed")
        return SimpleNamespace(chunk=[], end=None)
    async def no_sleep(s): return None
    monkeypatch.setattr(aio, "sleep", no_sleep)
    svc.client.room_messages = room_messages
    r = await svc._messages_page("!t:x", "tok", None)
    assert r.chunk == [] and limits == [100, 100, 30]


@pytest.mark.asyncio
async def test_download_uses_authenticated_media(tmp_path: Path, monkeypatch):
    import httpx
    svc, sent, left, job = _service(tmp_path)
    svc.client.homeserver = "https://hs.example"; svc.client.access_token = "TOKEN"
    seen = {}
    def handler(request):
        seen["url"] = str(request.url); seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"IMG")
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert await svc._download("mxc://media.example/abc123") == b"IMG"
    assert seen["url"].startswith("https://hs.example/_matrix/client/v1/media/download/media.example/abc123") and seen["auth"] == "Bearer TOKEN"


@pytest.mark.asyncio
async def test_a_failed_archive_can_be_retried_without_a_new_notice(tmp_path: Path, monkeypatch):
    import asyncio as aio
    async def no_sleep(s): return None
    monkeypatch.setattr(aio, "sleep", no_sleep)
    svc, sent, left, job = _service(tmp_path)
    await svc._archive_confirm(SimpleNamespace(room_id="!w3:x"), {"job": job, "hours": 1})
    await svc._include_command(SimpleNamespace(room_id="!t:x"), SimpleNamespace(sender="@ana:x"), include=True)
    [j] = svc.archives.jobs("scheduled")
    async def history(rid, after, only, with_files=True):
        return [m for m in msgs(0, 4) if m.sender in only], {"undecryptable": 0, "skipped_files": 0, "excluded": 0}
    svc._history = history
    mw = svc.checkpointer._mw; real = mw.remember_and_wait; calls = {"n": 0}
    async def flaky(text, namespace=None, timeout_ms=0):
        calls["n"] += 1
        if calls["n"] <= 3: raise RuntimeError("remember job failed: durable upload exceeded step budget")
        return await real(text, namespace, timeout_ms)
    mw.remember_and_wait = flaky
    await svc._archive_run(j)                                      # three failures in a row: the run fails
    [f] = svc.archives.jobs("failed")
    assert f["key_enc"] and f["included"] == {"@ana:x"} and "step budget" in f["error"]
    assert "archive retry" in [t for r, t in sent if r == "!w3:x"][-1]
    assert "archive retry" in svc._archive_status("@ana:x")
    assert "Retrying within a minute" in await svc._archive_retry("@ana:x", [])
    [again] = svc.archives.jobs("scheduled")
    await svc._archive_run(again)                                   # now the relayer works
    assert [t for r, t in sent if r == "!t:x"][-2].startswith("The memory of this room has been archived")


@pytest.mark.asyncio
async def test_retry_with_password_revives_a_job_without_key(tmp_path: Path):
    svc, sent, left, job = _service(tmp_path)
    job.update(state="failed", key_enc=None, included={"@ana:x"}, error="x"); svc.archives.put_job(job)
    assert "key was not kept" in await svc._archive_retry("@ana:x", ["Target"])
    assert "Retrying within a minute: Target" in await svc._archive_retry("@ana:x", ["target", "pw"])
    [j] = svc.archives.jobs("scheduled")
    assert svc._master().decrypt(j["key_enc"].encode()).decode() == derive_key("pw", bytes.fromhex(job["salt"]))


@pytest.mark.asyncio
async def test_retry_finds_the_job_by_part_of_its_name(tmp_path: Path):
    svc, sent, left, job = _service(tmp_path)
    job.update(room_name="UdK2300 / Klasse Leben public", state="failed", key_enc=None, included={"@ana:x"}, error="x")
    svc.archives.put_job(job)
    assert "Retrying within a minute: UdK2300 / Klasse Leben public" in await svc._archive_retry("@ana:x", ["UdK2300", "pw"])
