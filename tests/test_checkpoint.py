import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from walden.config import Config
from walden.adapters.local import LocalBlobStore, SQLiteRegistry, NullSemanticIndex
from walden.adapters.bindings import SQLiteEpisodeIndex
from walden.memory_graph import MemoryGraph
from walden.checkpoint import Checkpointer, decode, encode, sha256


def setup(tmp_path):
    cfg = Config()
    cfg.memory.identity_hmac_key = "identity-test"; cfg.memory.encryption_key = "encryption-test"
    cfg.memory.walrus.local_dir = str(tmp_path / "blobs"); cfg.memory.sui.local_db = str(tmp_path / "db.sqlite")
    cfg.memory.checkpoint.backend = "walrus"   # most tests use the plain-Walrus testnet path; MemWal has its own test
    reg = SQLiteRegistry(cfg.memory.sui.local_db)
    graph = MemoryGraph(cfg, LocalBlobStore(cfg.memory.walrus.local_dir), reg, reg, NullSemanticIndex(), SQLiteEpisodeIndex(cfg.memory.sui.local_db))
    return cfg, graph


async def seed(graph):
    room = await graph.ensure_room("!r:x"); ddh = await graph.ensure_person("@ddh:x"); me = await graph.ensure_person("@walden:x")
    await graph.set_memory(ddh, {"facts": [{"id": "f-1", "fact": "has a dog named Juni", "room": room},
                                           {"id": "f-2", "fact": "secret from another room", "room": "wal://room/other"}]})
    await graph.set_memory(me, {"facts": [{"id": "f-3", "fact": "co-writes Walden 3", "room": room}]})
    await graph.create_episode(room_ref=room, person_refs=[ddh], when={"end_ms": 1}, where={}, what={"summary": "DDH talked about Juni."}, source={})
    return room, ddh, me


@pytest.mark.asyncio
async def test_checkpoint_round_trip_hides_content_and_other_rooms(tmp_path: Path):
    cfg, graph = setup(tmp_path)
    room, ddh, me = await seed(graph)
    cp = Checkpointer(cfg, graph)
    b = await cp.build(room, {ddh: "DDH"}, me)
    header, envelope = decode(b.blob)
    assert header["seq"] == 1 and header["previous"] is None and header["network"] == "testnet"
    assert header["counts"] == {"people": 1, "person_facts": 1, "walden_facts": 1, "room_facts": 0, "episodes": 1}
    # Content and names are encrypted. (Phrases with spaces/quotes: base64 ciphertext cannot contain them by chance.)
    assert b"has a dog named Juni" not in b.blob and b'"name": "DDH"' not in b.blob and b'"name":"DDH"' not in b.blob
    h, payload = cp.open(b.blob)
    assert payload["people"][ddh]["facts"][0]["fact"] == "has a dog named Juni"
    assert "secret from another room" not in json.dumps(payload)     # facts never leave their room
    assert payload["episodes"][0]["summary"] == "DDH talked about Juni."


@pytest.mark.asyncio
async def test_tampering_is_detected(tmp_path: Path):
    cfg, graph = setup(tmp_path)
    room, ddh, me = await seed(graph)
    cp = Checkpointer(cfg, graph)
    header, envelope = decode((await cp.build(room, {ddh: "DDH"}, me)).blob)
    header["content_sha256"] = sha256(b"something else")
    with pytest.raises(ValueError, match="content hash"):
        cp.open(encode(header, envelope))


@pytest.mark.asyncio
async def test_checkpoints_chain_and_verify(tmp_path: Path):
    cfg, graph = setup(tmp_path)
    room, ddh, me = await seed(graph)
    cp = Checkpointer(cfg, graph); stored = {}
    async def read(blob_id): return stored[blob_id]
    cp.read = read
    for n in (1, 2):
        b = await cp.build(room, {ddh: "DDH"}, me)
        stored[f"blob{n}"] = b.blob
        cp.index.add(room_ref=room, seq=b.header["seq"], blob_id=f"blob{n}", object_id="0x1",
                     content_sha256=b.header["content_sha256"], network="testnet", created_at=b.header["created_at"])
    assert decode(stored["blob2"])[0]["previous"] == "blob1"
    lines = await cp.verify("blob2", room)
    assert [l.split()[0] for l in lines] == ["#2", "#1"]
    with pytest.raises(ValueError, match="another room"):
        await cp.verify("blob2", "wal://room/other")


@pytest.mark.asyncio
async def test_deploy_needs_confirmation_from_the_same_moderator(tmp_path: Path):
    from walden.matrix_service import MatrixService
    cfg, graph = setup(tmp_path)
    room_ref, ddh, me = await seed(graph)
    cfg.matrix.user_id = "@walden:x"
    agent = SimpleNamespace(graph=graph, self_ref=lambda: _async(me), save_facts=lambda s: _async(None),
                            commit_session=lambda s, r="": _async(None))
    svc = MatrixService(cfg, agent)
    sent = []
    async def send(room_id, text): sent.append(text)
    svc._send = send
    cp = svc.checkpointer; calls = []
    async def dry_run(b): return {"blob_id": "BLOB", "size": 2048, "encoded_size": 66 * 2**20, "storage_frost": 1_334_403}
    async def balance(): return {"SUI": 1.0, "WAL": 0.5}
    async def epoch_days(): return 1.0
    async def store(b): calls.append("store"); return {"blob_id": "BLOB", "object_id": "0xOBJ", "end_epoch": 543, "cost_frost": 1_400_000, "status": "newlyCreated"}
    async def set_attributes(o, h): calls.append("attrs")
    cp.dry_run, cp.balance, cp.epoch_days, cp.store, cp.set_attributes = dry_run, balance, epoch_days, store, set_attributes
    levels = {"@ddh:x": 100, "@guest:x": 0}
    room = SimpleNamespace(room_id="!r:x", users={"@ddh:x": None, "@walden:x": None},
                           power_levels=SimpleNamespace(get_user_level=lambda u: levels.get(u, 0)),
                           user_name=lambda u: {"@ddh:x": "DDH"}.get(u))
    ev = lambda who, body: SimpleNamespace(event_id="$e", sender=who, body=body, server_timestamp=1, source={"content": {"body": body}})

    await svc.on_message(room, ev("@guest:x", "!walden deploy"))
    assert "Only room moderators" in sent[-1] and not svc.pending_deploys
    await svc.on_message(room, ev("@ddh:x", "!walden deploy"))
    assert "checkpoint #1" in sent[-1] and "0.001334 WAL" in sent[-1] and "Blob ID will be: BLOB" in sent[-1]
    await svc.on_message(room, ev("@guest:x", "yes"))          # someone else's yes does not count
    assert calls == []
    await svc.on_message(room, ev("@ddh:x", "Yes"))
    assert calls == ["store", "attrs"]
    assert "Checkpoint #1 is stored" in sent[-1] and "https://walruscan.com/testnet/blob/BLOB" in sent[-1]
    assert svc.checkpointer.index.latest(room_ref, "testnet")["blob_id"] == "BLOB"


async def _async(v):
    return v


@pytest.mark.asyncio
async def test_load_carries_a_rooms_memory_into_another_room(tmp_path: Path):
    from walden.checkpoint import plan_import, apply_import
    from walden.core.facts import facts_of, in_room
    cfg, graph = setup(tmp_path)
    room_a, ddh, me = await seed(graph)
    room_b = await graph.ensure_room("!b:x")
    cp = Checkpointer(cfg, graph)
    b = await cp.build(room_a, {ddh: "DDH"}, me)
    header, payload = cp.open(b.blob)
    plan = await plan_import(cp, "BLOB", header, payload, target_room=room_b, members={}, self_ref=me)
    assert {plan.labels[r]: len(v) for r, v in plan.facts.items()} == {"DDH": 1, "Walden": 1}
    assert await apply_import(cp, plan, room_b) == {"added": 2, "updated": 0, "removed": 0}
    [f] = in_room(facts_of((await graph.resolve(ddh))["memory"]), room_b)
    assert f["fact"] == "has a dog named Juni" and f["imported_from"]["blob"] == "BLOB" and f["imported_from"]["room"] == room_a
    assert len(in_room(facts_of((await graph.resolve(ddh))["memory"]), room_a)) == 1      # the original stays where it was
    assert [e["what"]["type"] for e in await graph.recent_episodes(room_b, 10)] == ["imported-episode"]
    again = await plan_import(cp, "BLOB", header, payload, target_room=room_b, members={}, self_ref=me)
    assert again.facts == {} and again.duplicates == 2                                         # loading twice adds nothing


@pytest.mark.asyncio
async def test_checkpoint_from_another_walden_needs_its_key_and_matches_people_by_name(tmp_path: Path):
    from walden.checkpoint import plan_import
    cfg, graph = setup(tmp_path)
    (tmp_path / "other").mkdir()
    cfg2, graph2 = setup(tmp_path / "other")
    cfg2.memory.encryption_key = "the-other-walden"; cfg2.memory.identity_hmac_key = "other-identity"
    room2, ddh2, me2 = await seed(graph2)
    stranger = await graph2.ensure_person("@stranger:x")
    await graph2.set_memory(stranger, {"facts": [{"id": "f-9", "fact": "likes jazz", "room": room2}]})
    blob = (await Checkpointer(cfg2, graph2).build(room2, {ddh2: "DDH", stranger: "Stranger"}, me2)).blob
    cp = Checkpointer(cfg, graph)
    with pytest.raises(ValueError, match="cannot decrypt"):
        cp.open(blob)
    header, payload = cp.open(blob, key="the-other-walden")
    here = await graph.ensure_room("!here:x"); ddh_here = await graph.ensure_person("@ddh:x")
    plan = await plan_import(cp, "B", header, payload, target_room=here, members={ddh_here: "DDH"}, self_ref=await graph.ensure_person("@walden:x"))
    assert plan.labels[ddh_here] == "DDH" and plan.skipped_people == ["Stranger"]


def _svc(cfg, graph, me, rooms):
    """A MatrixService with a fake Matrix client that is in `rooms` ({room id: (name, members)})."""
    from walden.matrix_service import MatrixService
    cfg.matrix.user_id = "@walden:x"
    agent = SimpleNamespace(graph=graph, self_ref=lambda: _async(me), save_facts=lambda s: _async(None),
                            commit_session=lambda s, r="": _async(None))
    svc = MatrixService(cfg, agent); sent = []
    async def send(room_id, text): sent.append(text)
    svc._send = send
    svc.client = SimpleNamespace(rooms={rid: SimpleNamespace(display_name=name, users=dict.fromkeys(users))
                                        for rid, (name, users) in rooms.items()})
    return svc, sent


def _room(rid, users, levels):
    return SimpleNamespace(room_id=rid, users=dict.fromkeys(users), user_name=lambda u: {"@ddh:x": "DDH"}.get(u),
                           power_levels=SimpleNamespace(get_user_level=lambda u: levels.get(u, 0)))


def _ev(who, body):
    return SimpleNamespace(event_id="$e", sender=who, body=body, server_timestamp=1, source={"content": {"body": body}})


async def _saved_checkpoint(svc, graph, room_ref, ddh, me):
    b = await svc.checkpointer.build(room_ref, {ddh: "DDH"}, me)
    svc.checkpointer.index.add(room_ref=room_ref, seq=b.header["seq"], blob_id=f"BLOB{b.header['seq']}", object_id="",
                               content_sha256=b.header["content_sha256"], network="testnet", created_at=b.header["created_at"])
    blobs = getattr(svc, "_blobs", {}); blobs[f"BLOB{b.header['seq']}"] = b.blob; svc._blobs = blobs
    async def read(blob_id): return svc._blobs[blob_id]
    svc.checkpointer.read = read


@pytest.mark.asyncio
async def test_load_command_needs_a_moderator_and_yes(tmp_path: Path):
    from walden.core.facts import facts_of, in_room
    cfg, graph = setup(tmp_path)
    room_a, ddh, me = await seed(graph)
    svc, sent = _svc(cfg, graph, me, {"!r:x": ("Walden", ["@ddh:x", "@walden:x"]), "!b:x": ("Other", ["@ddh:x", "@walden:x", "@guest:x"])})
    await _saved_checkpoint(svc, graph, room_a, ddh, me)
    room = _room("!b:x", ["@ddh:x", "@walden:x", "@guest:x"], {"@ddh:x": 100})
    await svc.on_message(room, _ev("@guest:x", "!walden load BLOB1"))
    assert "Only room moderators" in sent[-1]
    await svc.on_message(room, _ev("@ddh:x", "!walden load BLOB1"))
    assert "content hash ok" in sent[-1] and "- DDH: 1 new facts" in sent[-1] and "- Walden: 1 new facts" in sent[-1]
    await svc.on_message(room, _ev("@ddh:x", "yes"))
    assert sent[-1].startswith("Loaded checkpoint #1 (BLOB1): 2 facts added, 0 updated, 0 removed, 1 episodes added")
    room_b = await graph.ensure_room("!b:x")
    assert [f["fact"] for f in in_room(facts_of((await graph.resolve(ddh))["memory"]), room_b)] == ["has a dog named Juni"]
    await svc.on_message(room, _ev("@ddh:x", "!walden load BLOB1"))
    assert "nothing new for this room" in sent[-1]


@pytest.mark.asyncio
async def test_load_by_room_name_number_episode_and_own_id(tmp_path: Path):
    cfg, graph = setup(tmp_path)
    room_a, ddh, me = await seed(graph)
    svc, sent = _svc(cfg, graph, me, {"!r:x": ("Walden", ["@ddh:x", "@walden:x"]), "!b:x": ("Other", ["@ddh:x", "@walden:x", "@guest:x"])})
    await _saved_checkpoint(svc, graph, room_a, ddh, me)
    room = _room("!b:x", ["@ddh:x", "@walden:x", "@guest:x"], {"@ddh:x": 100, "@guest:x": 100})

    await svc.on_message(room, _ev("@ddh:x", "!walden checkpoints"))
    assert "- walden#1  (Walden," in sent[-1]
    await svc.on_message(room, _ev("@ddh:x", "!walden episodes walden"))
    assert "- walden/e1 (" in sent[-1] and "DDH talked about Juni." in sent[-1]
    for target in ("walden", "walden#1", "!r:x"):
        await svc.on_message(room, _ev("@ddh:x", f"!walden load {target}"))
        assert "walden#1, made" in sent[-1] and "- DDH: 1 new facts" in sent[-1], target
        await svc.on_message(room, _ev("@ddh:x", "no"))
    await svc.on_message(room, _ev("@ddh:x", "!walden load walden/e1"))
    assert "walden#1 (part)" in sent[-1] and "- 1 past episodes" in sent[-1] and "DDH:" not in sent[-1]   # the seeded facts have no episode
    await svc.on_message(room, _ev("@ddh:x", "no"))
    await svc.on_message(room, _ev("@ddh:x", "!walden load @ddh:x"))
    assert "(part)" in sent[-1] and "- DDH: 1 new facts" in sent[-1] and "Walden:" not in sent[-1]
    await svc.on_message(room, _ev("@ddh:x", "no"))
    await svc.on_message(room, _ev("@ddh:x", "!walden load @guest:x"))
    assert "only load what Walden knows about you" in sent[-1]
    # The guest moderates this room but is not a member of "Walden": no loading its memory, by any name.
    for target in ("walden", "!r:x", "BLOB1"):
        await svc.on_message(room, _ev("@guest:x", f"!walden load {target}"))
        assert "rooms you are a member of" in sent[-1], target
    await svc.on_message(room, _ev("@guest:x", "!walden checkpoints"))
    assert "walden#1" not in sent[-1]


@pytest.mark.asyncio
async def test_memory_command_saves_pending_messages_first(tmp_path: Path):
    from walden.matrix_service import MatrixService
    cfg, graph = setup(tmp_path)
    room_ref, ddh, me = await seed(graph)
    cfg.matrix.user_id = "@walden:x"; cfg.matrix.reply_mode = "commands"; cfg.matrix.session.save_every = 0
    order = []
    async def save_facts(session):
        order.append(("save", len(session.events)))
        mem = (await graph.resolve(ddh))["memory"]
        await graph.set_memory(ddh, {"facts": mem["facts"] + [{"id": "f-new", "fact": "visited Brussels", "room": room_ref}]})
    agent = SimpleNamespace(graph=graph, self_ref=lambda: _async(me), save_facts=save_facts, commit_session=lambda s, r="": _async(None))
    svc = MatrixService(cfg, agent)
    async def send(room_id, text): order.append(("send", text))
    svc._send = send
    room = SimpleNamespace(room_id="!r:x", users={}, user_name=lambda u: None)
    ev = lambda body: SimpleNamespace(event_id="$e", sender="@ddh:x", body=body, server_timestamp=1, source={"content": {"body": body}})
    await svc.on_message(room, ev("I just came back from Brussels"))
    await svc.on_message(room, ev("!walden memory"))
    assert order[0] == ("save", 1) and order[1][0] == "send" and "visited Brussels" in order[1][1]



@pytest.mark.asyncio
async def test_reloading_from_the_same_room_syncs(tmp_path: Path):
    from walden.checkpoint import plan_import, apply_import
    from walden.core.facts import facts_of, in_room
    cfg, graph = setup(tmp_path)
    room_a, ddh, me = await seed(graph)
    room_b = await graph.ensure_room("!b:x")
    await graph.set_memory(ddh, {"facts": facts_of((await graph.resolve(ddh))["memory"]) + [
        {"id": "f-4", "fact": "lives in Berlin", "room": room_a},
        {"id": "f-own", "fact": "likes tea", "room": room_b}]})            # room B's own fact
    cp = Checkpointer(cfg, graph)
    async def checkpoint():
        b = await cp.build(room_a, {ddh: "DDH"}, me)
        cp.index.add(room_ref=room_a, seq=b.header["seq"], blob_id=f"blob{b.header['seq']}", object_id="", content_sha256=b.header["content_sha256"],
                     network="testnet", created_at=b.header["created_at"])
        return cp.open(b.blob)
    async def load(header, payload):
        plan = await plan_import(cp, f"blob{header['seq']}", header, payload, target_room=room_b, members={ddh: "DDH"}, self_ref=me)
        return plan, await apply_import(cp, plan, room_b)
    _mem = None
    in_b = lambda: [f["fact"] for f in in_room(facts_of(_mem), room_b)]

    h1, p1 = await checkpoint()
    plan, n = await load(h1, p1)
    assert n == {"added": 3, "updated": 0, "removed": 0}

    # In room A: reword f-1 (same id), forget f-4, learn something new.
    mem = (await graph.resolve(ddh))["memory"]
    facts = [dict(f, fact="has a dog named Juni, 80% poodle") if f["id"] == "f-1" else f for f in facts_of(mem) if f["id"] != "f-4"]
    await graph.set_memory(ddh, {"facts": facts + [{"id": "f-5", "fact": "visited Brussels", "room": room_a}]})
    h2, p2 = await checkpoint()
    plan, n = await load(h2, p2)
    assert plan.synced_from == 1 and not plan.older
    assert n == {"added": 1, "updated": 1, "removed": 1}
    _mem = (await graph.resolve(ddh))["memory"]
    assert sorted(in_b()) == ["has a dog named Juni, 80% poodle", "likes tea", "visited Brussels"]

    plan, n = await load(h1, p1)                                            # older checkpoint: add-only
    assert plan.older and n == {"added": 1, "updated": 0, "removed": 0}
    _mem = (await graph.resolve(ddh))["memory"]
    assert "lives in Berlin" in in_b() and "has a dog named Juni, 80% poodle" in in_b()
    assert "has a dog named Juni" not in in_b()                             # the old wording does not come back
    assert sorted(f["fact"] for f in in_room(facts_of(_mem), room_a)) == ["has a dog named Juni, 80% poodle", "visited Brussels"]  # source untouched



@pytest.mark.asyncio
async def test_memwal_backend_stores_and_reads_back_by_blob_id(tmp_path: Path):
    cfg, graph = setup(tmp_path)
    cfg.memory.checkpoint.backend = "memwal"
    cfg.memory.checkpoint.memwal_key = "k"; cfg.memory.checkpoint.memwal_account_id = "0xACCOUNT"
    room, ddh, me = await seed(graph)
    cp = Checkpointer(cfg, graph)
    assert cp.network == "walrus-memory:prod" and cp.where == "Walrus Memory (mainnet)"
    stored = {}

    class FakeMemWal:
        async def remember_and_wait(self, text, namespace=None, timeout_ms=0):
            stored[f"MW{len(stored)}"] = (namespace, text)
            return SimpleNamespace(id="m1", blob_id=f"MW{len(stored)-1}", owner="0xOWNER", namespace=namespace)
        async def recall(self, params):
            return SimpleNamespace(results=[SimpleNamespace(blob_id=b, text=t, distance=0.1)
                                            for b, (ns, t) in stored.items() if ns == params.namespace])
        async def list_namespaces(self):
            return SimpleNamespace(namespaces=[SimpleNamespace(name=ns) for ns, _ in stored.values()])
    cp._mw = FakeMemWal()

    b = await cp.build(room, {ddh: "DDH"}, me)
    dry = await cp.dry_run(b)
    assert dry["namespace"] == cp.namespace(room) and dry["namespace"].startswith("walden-")
    res = await cp.store(b)
    assert res["blob_id"] == "MW0" and res["namespace"] == dry["namespace"]
    assert b"has a dog named Juni" not in stored["MW0"][1].encode()     # the relayer only sees Walden's ciphertext
    cp.index.add(room_ref=room, seq=1, blob_id="MW0", object_id="", content_sha256=b.header["content_sha256"],
                 network=cp.network, created_at=b.header["created_at"], namespace=res["namespace"])
    assert (await cp.verify("MW0", room))[0].startswith("#1 MW0: content hash ok")
    assert cp.links("MW0") == ["https://walruscan.com/mainnet/blob/MW0", "https://suiscan.xyz/mainnet/object/0xACCOUNT"]


@pytest.mark.asyncio
async def test_memwal_rejects_checkpoints_over_64_kib(tmp_path: Path):
    from walden.checkpoint import Bundle
    cfg, graph = setup(tmp_path)
    cfg.memory.checkpoint.backend = "memwal"
    cp = Checkpointer(cfg, graph)
    with pytest.raises(RuntimeError, match="at most 64 KiB"):
        await cp.dry_run(Bundle(b"x" * (65 * 1024), {"room": "wal://room/0xabc"}))


@pytest.mark.asyncio
async def test_save_and_confirm_on_walrus_memory(tmp_path: Path):
    cfg, graph = setup(tmp_path)
    cfg.memory.checkpoint.backend = "memwal"; cfg.memory.checkpoint.memwal_account_id = "0xACCOUNT"
    room_ref, ddh, me = await seed(graph)
    svc, sent = _svc(cfg, graph, me, {"!r:x": ("Walden", ["@ddh:x", "@walden:x"])})
    cp = svc.checkpointer
    async def store(b): return {"blob_id": "MWBLOB", "object_id": "", "owner": "0xO", "namespace": cp.namespace(room_ref), "status": "done"}
    cp.store = store
    room = _room("!r:x", ["@ddh:x", "@walden:x"], {"@ddh:x": 100})
    await svc.on_message(room, _ev("@ddh:x", "!walden save"))
    assert "ready for Walrus Memory (mainnet)" in sent[-1] and "paid by the Walrus Memory relayer" in sent[-1] and "Blob ID will be" not in sent[-1]
    await svc.on_message(room, _ev("@ddh:x", "yes"))
    assert "Checkpoint #1 is stored on Walrus Memory (mainnet)" in sent[-1] and "- Blob ID: MWBLOB" in sent[-1] and "0xACCOUNT" in sent[-1]
    row = cp.index.latest(room_ref, cp.network)
    assert row["blob_id"] == "MWBLOB" and row["namespace"] == cp.namespace(room_ref)


@pytest.mark.asyncio
async def test_an_error_while_handling_a_message_does_not_escape(tmp_path: Path):
    cfg, graph = setup(tmp_path)
    room_ref, ddh, me = await seed(graph)
    svc, sent = _svc(cfg, graph, me, {"!r:x": ("Walden", ["@ddh:x", "@walden:x"])})
    async def boom(*a): raise KeyError("blob_id")
    svc._deploy_prepare = boom
    room = _room("!r:x", ["@ddh:x", "@walden:x"], {"@ddh:x": 100})
    await svc.on_message(room, _ev("@ddh:x", "!walden save"))        # must not raise
    assert "internal error (KeyError)" in sent[-1]


@pytest.mark.asyncio
async def test_session_end_mirrors_memory_to_walrus_memory_only_when_changed(tmp_path: Path):
    cfg, graph = setup(tmp_path)
    cfg.memory.checkpoint.backend = "memwal"; cfg.memory.checkpoint.memwal_key = "k"; cfg.memory.checkpoint.memwal_account_id = "0xA"
    room_ref, ddh, me = await seed(graph)
    svc, sent = _svc(cfg, graph, me, {"!r:x": ("Walden", ["@ddh:x", "@walden:x"])})
    cp = svc.checkpointer; stored = []
    async def store(b): stored.append(b); return {"blob_id": f"AUTO{len(stored)}", "object_id": "", "namespace": cp.namespace(room_ref), "status": "done"}
    cp.store = store
    await svc._auto_save("!r:x")
    await svc._auto_save("!r:x")                                     # nothing changed: no second blob
    assert len(stored) == 1 and cp.index.latest(room_ref, cp.network)["blob_id"] == "AUTO1"
    mem = (await graph.resolve(ddh))["memory"]
    await graph.set_memory(ddh, {"facts": mem["facts"] + [{"id": "f-9", "fact": "visited Brussels", "room": room_ref}]})
    await svc._auto_save("!r:x")
    assert len(stored) == 2 and cp.index.latest(room_ref, cp.network)["seq"] == 2
