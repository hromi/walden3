import pytest
from pathlib import Path

from walden.config import Config
from walden.adapters.local import LocalBlobStore, SQLiteRegistry, NullSemanticIndex
from walden.memory_graph import MemoryGraph
from walden.agent import WaldenAgent
from walden.llm import LLM
from walden.core.models import MatrixEventRecord


class CaptureLLM(LLM):
    def __init__(self):
        self.messages = None

    async def chat(self, messages, json_schema=None):
        self.messages = messages
        return "ok"


@pytest.mark.asyncio
async def test_multiplayer_matrix_prompt_uses_stable_refs_not_raw_mxids(tmp_path: Path):
    cfg = Config()
    cfg.memory.identity_hmac_key = "identity-test"
    cfg.memory.encryption_key = "encryption-test"
    cfg.memory.walrus.local_dir = str(tmp_path / "blobs")
    cfg.memory.sui.local_db = str(tmp_path / "db.sqlite")
    store = LocalBlobStore(cfg.memory.walrus.local_dir)
    reg = SQLiteRegistry(cfg.memory.sui.local_db)
    graph = MemoryGraph(cfg, store, reg, reg, NullSemanticIndex())
    llm = CaptureLLM()
    agent = WaldenAgent(cfg, graph, llm)

    room = "!r:example.org"
    alice = MatrixEventRecord(event_id="$1", room_id=room, sender="@alice:example.org", timestamp_ms=1, body="Tea?", role="human")
    walden = MatrixEventRecord(event_id="$2", room_id=room, sender="@walden:example.org", timestamp_ms=2, body="Yes.", role="assistant")
    bob = MatrixEventRecord(event_id="$3", room_id=room, sender="@bob:example.org", timestamp_ms=3, body="I prefer coffee.", role="human")

    result = await agent.respond(bob, ["@bob:example.org", "@alice:example.org"], [alice, walden, bob])
    assert result == "ok"
    rendered = "\n".join(m["content"] for m in llm.messages)
    assert "wal://person/" in rendered
    assert "wal://room/" in rendered
    assert "@alice:example.org" not in rendered
    assert "@bob:example.org" not in rendered
    assert "Tea?" in rendered and "I prefer coffee." in rendered


def _agent(tmp_path, episodes=True):
    from walden.adapters.bindings import SQLiteEpisodeIndex
    cfg = Config()
    cfg.memory.identity_hmac_key = "identity-test"
    cfg.memory.encryption_key = "encryption-test"
    cfg.memory.walrus.local_dir = str(tmp_path / "blobs")
    cfg.memory.sui.local_db = str(tmp_path / "db.sqlite")
    reg = SQLiteRegistry(cfg.memory.sui.local_db)
    idx = SQLiteEpisodeIndex(cfg.memory.sui.local_db) if episodes else None
    graph = MemoryGraph(cfg, LocalBlobStore(cfg.memory.walrus.local_dir), reg, reg, NullSemanticIndex(), idx)
    llm = CaptureLLM()
    return WaldenAgent(cfg, graph, llm), graph, llm


@pytest.mark.asyncio
async def test_only_speaker_and_mentioned_people_memory_is_loaded(tmp_path: Path):
    agent, graph, llm = _agent(tmp_path)
    room = "!r:example.org"
    room_ref = await graph.ensure_room(room)
    for uid, fact, where in [("@ddh:x", "dog Juni", room_ref), ("@carol:x", "secret garden", room_ref),
                             ("@arch:x", "archives", room_ref), ("@ddh:x", "other-room secret", "wal://room/elsewhere")]:
        ref = await graph.ensure_person(uid); mem = (await graph.resolve(ref))["memory"]
        await graph.set_memory(ref, {"facts": mem.get("facts", []) + [{"id": f"f-{len(fact)}", "fact": fact, "room": where, "said_by_name": "x", "at": "d"}]})
    names = {"@ddh:x": "DDH", "@arch:x": "The Archivist"}
    carol = MatrixEventRecord(event_id="$0", room_id=room, sender="@carol:x", timestamp_ms=0, body="hi", role="human", sender_name="Carol")
    ask = MatrixEventRecord(event_id="$1", room_id=room, sender="@arch:x", timestamp_ms=1, body="tell me about DDH", role="human", sender_name="The Archivist")

    await agent.respond(ask, ["@ddh:x"], [carol, ask], names)
    rendered = "\n".join(m["content"] for m in llm.messages)
    assert "dog Juni" in rendered and "archives" in rendered
    assert "secret garden" not in rendered  # Carol spoke recently but was not mentioned
    assert "other-room secret" not in rendered  # facts never leave the room they were learned in
    assert "WHAT YOU KNOW ABOUT DDH (another person, not you)\n- dog Juni (said by x, d)" in rendered
    assert "[Carol] hi" in rendered and "[The Archivist] tell me about DDH" in rendered
    assert "@ddh:x" not in rendered


@pytest.mark.asyncio
async def test_recent_room_episodes_are_in_prompt(tmp_path: Path):
    agent, graph, llm = _agent(tmp_path)
    room_ref = await graph.ensure_room("!r:example.org")
    other_ref = await graph.ensure_room("!other:example.org")
    for i, (ref, summary) in enumerate([(room_ref, "DDH invited Walden to co-write Walden 3."), (other_ref, "Unrelated room chat.")]):
        await graph.create_episode(room_ref=ref, person_refs=[], when={"start_ms": i, "end_ms": 1_700_000_000_000 + i},
                                   where={}, what={"summary": summary, "topics": ["book"]}, source={})
    ask = MatrixEventRecord(event_id="$1", room_id="!r:example.org", sender="@ddh:x", timestamp_ms=1, body="what are we writing?", role="human")
    await agent.respond(ask, [], [ask], {"@ddh:x": "DDH"})
    rendered = "\n".join(m["content"] for m in llm.messages)
    assert "EARLIER EPISODES IN THIS ROOM" in rendered
    assert "DDH invited Walden to co-write Walden 3. [topics: book]" in rendered
    assert "Unrelated room chat." not in rendered


@pytest.mark.asyncio
async def test_walden_remembers_itself_and_people_as_facts(tmp_path: Path):
    import json
    from walden.session import Session

    class OpsLLM(CaptureLLM):
        async def chat(self, messages, json_schema=None):
            self.messages = messages
            if json_schema:
                e = {"add": [], "update": [], "retract": []}
                return json.dumps({"episode_summary": "DDH invited Walden to co-write Walden 3; Walden agreed.", "episode_topics": ["book"],
                                   "memory": {"P1": {**e, "add": [{"fact": "is writing the book Walden 3", "said_by": "P1"}]},
                                              "WALDEN": {**e, "add": [{"fact": "co-writes Walden 3 with DDH", "said_by": "P1"}]},
                                              "ROOM": e}})
            return "ok"

    agent, graph, _ = _agent(tmp_path)
    agent.cfg.matrix.user_id = "@walden:x"
    agent.llm = agent.consolidator.llm = llm = OpsLLM()
    s = Session("!r:x")
    s.add(MatrixEventRecord(event_id="$1", room_id="!r:x", sender="@ddh:x", timestamp_ms=1_790_000_000_000, body="Join our book?", role="human", sender_name="DDH"))
    s.add(MatrixEventRecord(event_id="$2", room_id="!r:x", sender="@walden:x", timestamp_ms=1_790_000_001_000, body="Gladly.", role="assistant"))
    episode = await agent.commit_session(s)

    room_ref = await graph.ensure_room("!r:x"); ddh_ref = await graph.ensure_person("@ddh:x")
    [me] = (await graph.resolve(await agent.self_ref()))["memory"]["facts"]
    [ddh] = (await graph.resolve(ddh_ref))["memory"]["facts"]
    assert me["fact"] == "co-writes Walden 3 with DDH" and me["said_by"] == ddh_ref and me["said_by_name"] == "DDH"
    assert ddh["fact"] == "is writing the book Walden 3" and ddh["room"] == room_ref and ddh["episode"] == episode
    assert ddh["at"] == "2026-09-21"

    ask = MatrixEventRecord(event_id="$3", room_id="!r:x", sender="@arch:x", timestamp_ms=3, body="what do you do?", role="human")
    await agent.respond(ask, [], [ask], {"@arch:x": "The Archivist"})
    rendered = "\n".join(m["content"] for m in llm.messages)
    assert "WHAT YOU KNOW ABOUT YOURSELF, WALDEN\n- co-writes Walden 3 with DDH (said by DDH, 2026-09-21)" in rendered


@pytest.mark.asyncio
async def test_prompt_starts_with_immutable_kernel_and_person_rule(tmp_path: Path):
    from walden.persona import KERNEL
    agent, graph, llm = _agent(tmp_path)
    agent.cfg.agent.system_prompt = "Config part."
    ask = MatrixEventRecord(event_id="$1", room_id="!r:x", sender="@ddh:x", timestamp_ms=1, body="who is DDH?", role="human", sender_name="DDH")
    await agent.respond(ask, [], [ask], {"@ddh:x": "DDH"})
    first = llm.messages[0]["content"]
    assert first.startswith(KERNEL.strip()) and "Config part." in first and "What you do:" in first
    task = llm.messages[-1]["content"]
    assert "MESSAGE TO ANSWER NOW, from DDH:\n[DDH] who is DDH?" in task
    assert 'DDH is asking about themselves: answer "You are ..."' in task

    other = MatrixEventRecord(event_id="$2", room_id="!r:x", sender="@arch:x", timestamp_ms=2, body="what do you know about ddh?", role="human", sender_name="The Archivist")
    await agent.respond(other, ["@ddh:x"], [other], {"@ddh:x": "DDH", "@arch:x": "The Archivist"})
    task = llm.messages[-1]["content"]
    assert 'The question is about DDH, not about The Archivist. Refer to DDH by name ("DDH is ..."), never as "you". Do not describe The Archivist.' in task
    assert "You are ..." not in task

    walden = MatrixEventRecord(event_id="$3", room_id="!r:x", sender="@arch:x", timestamp_ms=3, body="who are you?", role="human", sender_name="The Archivist")
    await agent.respond(walden, [], [walden], {"@arch:x": "The Archivist"})
    assert 'answer "I am Walden ..." in your own words' in llm.messages[-1]["content"] and "You are ..." not in llm.messages[-1]["content"]
    assert "a different agent called Ada" in llm.messages[-1]["content"]

    agent.cfg.agent.system_prompt = ""
    await agent.respond(ask, [], [ask], {"@ddh:x": "DDH"})
    assert llm.messages[0]["content"].startswith(KERNEL.strip()) and "Config part." not in llm.messages[0]["content"]


@pytest.mark.asyncio
async def test_long_sessions_are_read_in_chunks_that_see_earlier_facts(tmp_path: Path):
    import json
    from walden.session import Session

    class ChunkLLM(CaptureLLM):
        prompts = []
        async def chat(self, messages, json_schema=None):
            if json_schema is None: return "merged summary."
            self.prompts.append(messages[-1]["content"])
            e = {"add": [], "update": [], "retract": []}
            fact = "has a dog named Juni" if len(self.prompts) == 1 else "has a dog named Juni"  # repeated: deduplicated
            return json.dumps({"episode_summary": f"part {len(self.prompts)}.", "episode_topics": ["dog"],
                               "memory": {"P1": {**e, "add": [{"fact": fact, "said_by": "P1"}]}, "WALDEN": e, "ROOM": e}})

    agent, graph, _ = _agent(tmp_path)
    agent.cfg.matrix.user_id = "@walden:x"; agent.cfg.memory.consolidation.events_per_pass = 1
    agent.llm = agent.consolidator.llm = llm = ChunkLLM()
    s = Session("!r:x")
    for i, body in enumerate(["I have a dog", "named Juni"]):
        s.add(MatrixEventRecord(event_id=f"${i}", room_id="!r:x", sender="@ddh:x", timestamp_ms=i, body=body, role="human", sender_name="DDH"))
    episode = await agent.commit_session(s)
    assert len(llm.prompts) == 2 and "has a dog named Juni" in llm.prompts[1].split("TRANSCRIPT:")[1]
    [fact] = (await graph.resolve(await graph.ensure_person("@ddh:x")))["memory"]["facts"]
    assert fact["episode"] == episode
    assert (await graph.resolve(episode))["what"]["summary"] == "merged summary."


@pytest.mark.asyncio
async def test_facts_saved_per_exchange_are_linked_to_the_episode_at_close(tmp_path: Path):
    import json
    from walden.session import Session

    class StepLLM(CaptureLLM):
        calls = []
        async def chat(self, messages, json_schema=None):
            if json_schema is None: return "s1. s2."
            transcript = messages[-1]["content"].split("TRANSCRIPT:\n")[1].split("\n\nKNOWN FACTS")[0]
            self.calls.append(transcript)
            e = {"add": [], "update": [], "retract": []}
            fact = "has a dog named Juni" if "Juni" in transcript.split("NEW MESSAGES")[-1] else "has three daughters"
            return json.dumps({"episode_summary": f"s{len(self.calls)}.", "episode_topics": [],
                               "memory": {"P1": {**e, "add": [{"fact": fact, "said_by": "P1"}]}, "WALDEN": e, "ROOM": e}})

    agent, graph, _ = _agent(tmp_path)
    agent.cfg.matrix.user_id = "@walden:x"
    agent.llm = agent.consolidator.llm = llm = StepLLM()
    s = Session("!r:x")
    def say(i, body, role="human"):
        s.add(MatrixEventRecord(event_id=f"${i}", room_id="!r:x", sender="@ddh:x" if role == "human" else "@walden:x",
                                timestamp_ms=1_790_000_000_000 + i, body=body, role=role, sender_name="DDH" if role == "human" else ""))
    say(1, "My dog is Juni"); say(2, "Nice.", "assistant")
    await agent.save_facts(s)
    ddh = await graph.ensure_person("@ddh:x")
    [f] = (await graph.resolve(ddh))["memory"]["facts"]
    assert f["fact"] == "has a dog named Juni" and f["episode"] is None and f["session"] == s.sid and f["events"] == ["$1", "$2"]

    say(3, "I have three daughters")
    episode = await agent.commit_session(s)
    assert len(llm.calls) == 2 and "My dog is Juni" not in llm.calls[1].split("NEW MESSAGES")[-1]  # saved part not re-read
    facts = (await graph.resolve(ddh))["memory"]["facts"]
    assert [x["fact"] for x in facts] == ["has a dog named Juni", "has three daughters"]
    assert all(x["episode"] == episode for x in facts)
    assert (await graph.resolve(episode))["what"]["summary"] == "s1. s2."
    await agent.save_facts(s)  # a late background save after close does nothing
    assert len(llm.calls) == 2


def test_long_messages_are_split_into_parts():
    from walden.agent import split_long
    body = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(8))     # ~2.6k characters
    e = MatrixEventRecord(event_id="$1", room_id="!r", sender="@d:x", timestamp_ms=1, body=body, role="human", sender_name="DDH")
    parts = split_long(e)
    assert len(parts) >= 3 and all(len(p.body) < 900 for p in parts)
    assert parts[0].body.startswith(f"(part 1/{len(parts)} of one message) Paragraph 0.") and parts[-1].event_id == "$1"
    assert split_long(e.model_copy(update={"body": "short"})) == [e.model_copy(update={"body": "short"})]


@pytest.mark.asyncio
async def test_a_long_post_is_read_in_several_passes(tmp_path: Path):
    import json
    from walden.session import Session
    class PassLLM(CaptureLLM):
        passes = []
        async def chat(self, messages, json_schema=None):
            if json_schema is None: return "merged."
            new = messages[-1]["content"].split("NEW MESSAGES (take facts only from here):\n")[-1].split("\n\nKNOWN FACTS")[0]
            self.passes.append(new)
            e = {"add": [], "update": [], "retract": []}
            return json.dumps({"episode_summary": "s.", "episode_topics": [], "memory": {"P1": e, "WALDEN": e, "ROOM": e}})
    agent, graph, _ = _agent(tmp_path)
    agent.llm = agent.consolidator.llm = llm = PassLLM()
    s = Session("!r:x")
    s.add(MatrixEventRecord(event_id="$1", room_id="!r:x", sender="@ddh:x", timestamp_ms=1, role="human", sender_name="DDH",
                            body="\n\n".join(f"Fact {i}: " + "detail " * 70 for i in range(10))))
    await agent.save_facts(s)
    assert len(llm.passes) >= 3 and s.saved == 1
    assert "Fact 0:" in llm.passes[0] and "Fact 9:" in llm.passes[-1]


def test_memory_over_budget_keeps_the_relevant_lines_not_just_the_first():
    from walden.context import ContextBuilder
    facts = [f"- works on topic number {i} in great detail (said by DDH, 2026-10-02)" for i in range(200)]
    facts.insert(150, "- An article *Entwachstum ? Jetzt !* has just been published as part of this Compendium (said by DDH, 2026-10-02)")
    sections = [("WHAT YOU KNOW ABOUT YOURSELF, WALDEN", ["- helps DDH (said by DDH, 2026-09-24)"]),
                ("WHAT YOU KNOW ABOUT DDH (another person, not you)", facts),
                ("EARLIER EPISODES IN THIS ROOM (oldest first)", ["- 2026-10-02 22:08 UTC: DDH shared a long profile."])]
    out = ContextBuilder._fit("what article has been published in the compendium?", sections, 3000)
    assert len(out) <= 3200 and "Entwachstum" in out
    assert "EARLIER EPISODES IN THIS ROOM" in out and "less relevant lines not shown" in out
    assert out.index("topic number 0") < out.index("Entwachstum") if "topic number 0 " in out else True   # original order kept
    small = ContextBuilder._fit("x", sections[:1], 3000)
    assert small == "WHAT YOU KNOW ABOUT YOURSELF, WALDEN\n- helps DDH (said by DDH, 2026-09-24)"
