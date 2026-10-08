import json
import pytest

from walden.config import Config
from walden.consolidation import Consolidator, schema
from walden.core.models import MatrixEventRecord
from walden.llm import LLM


def sec(add=(), update=(), retract=()):
    return {"add": list(add), "update": list(update), "retract": list(retract)}


class MemLLM(LLM):
    def __init__(self, memory):
        self.memory = memory; self.messages = None; self.schema = None

    async def chat(self, messages, json_schema=None):
        self.messages = messages; self.schema = json_schema
        return json.dumps({"episode_summary": "s", "episode_topics": [], "memory": self.memory})


def ev(i, sender, body, role="human", name=""):
    return MatrixEventRecord(event_id=f"${i}", room_id="!r:x", sender=sender, timestamp_ms=i, body=body, role=role, sender_name=name)


SESSION = [ev(1, "@alice:example.org", "I prefer tea", name="Alice"),
           ev(2, "@walden:example.org", "Noted", role="assistant")]
ALICE = "@alice:example.org"


@pytest.mark.asyncio
async def test_consolidation_hides_raw_matrix_ids_and_maps_alias_back():
    llm = MemLLM({"P1": sec(add=[{"fact": "prefers tea", "said_by": "P1"}]), "WALDEN": sec(), "ROOM": sec()})
    result = await Consolidator(Config(), llm).consolidate(SESSION)
    rendered = "\n".join(m["content"] for m in llm.messages)
    assert "@alice:example.org" not in rendered and "@walden:example.org" not in rendered
    assert "[P1 (Alice)] I prefer tea" in rendered
    assert result.operations == [{"op": "add", "about": ALICE, "said_by": ALICE, "fact": "prefers tea"}]


def test_schema_limits_speakers_and_ids_per_subject():
    s = schema({"P1": ["f-aaaaaa"], "WALDEN": [], "ROOM": []}, ["P1", "WALDEN"])
    mem = s["properties"]["memory"]
    assert mem["required"] == ["P1", "WALDEN", "ROOM"]
    p1 = mem["properties"]["P1"]["properties"]
    assert p1["update"]["items"]["properties"]["id"]["enum"] == ["f-aaaaaa"]
    assert p1["add"]["items"]["properties"]["said_by"]["enum"] == ["P1", "WALDEN"]


@pytest.mark.asyncio
async def test_walden_and_room_sections_and_bad_items():
    llm = MemLLM({
        "P1": sec(add=[{"fact": "x", "said_by": "Nobody"}]),                       # unknown speaker
        "WALDEN": sec(add=[{"fact": "co-writes Walden 3", "said_by": "P1"}],
                      retract=[{"id": "f-unknown", "said_by": "P1"}]),             # id not shown
        "ROOM": sec(add=[{"fact": "is for writing Walden 3", "said_by": "Alice"}]),
        "P7": sec(add=[{"fact": "y", "said_by": "P1"}]),                           # unknown subject
    })
    result = await Consolidator(Config(), llm).consolidate(SESSION)
    assert [(o["about"], o["fact"], o["said_by"]) for o in result.operations] == [
        ("walden", "co-writes Walden 3", ALICE), ("room", "is for writing Walden 3", ALICE)]


@pytest.mark.asyncio
async def test_known_facts_are_shown_and_can_be_updated_or_retracted():
    known = {ALICE: [{"id": "f-aaaaaa", "fact": "prefers coffee"}], "walden": [{"id": "f-bbbbbb", "fact": "is shy"}]}
    llm = MemLLM({"P1": sec(update=[{"id": "f-aaaaaa", "fact": "prefers tea", "said_by": "P1"},
                                    {"id": "f-bbbbbb", "fact": "stolen id", "said_by": "P1"}]),   # Walden's id in P1's section
                  "WALDEN": sec(retract=[{"id": "f-bbbbbb", "said_by": "P1"}]), "ROOM": sec()})
    result = await Consolidator(Config(), llm).consolidate(SESSION, known)
    prompt = llm.messages[-1]["content"]
    assert "P1 (Alice):\n- [f-aaaaaa] prefers coffee" in prompt and "WALDEN:\n- [f-bbbbbb] is shy" in prompt
    assert result.operations == [
        {"op": "update", "about": ALICE, "said_by": ALICE, "id": "f-aaaaaa", "fact": "prefers tea"},
        {"op": "retract", "about": "walden", "said_by": ALICE, "id": "f-bbbbbb"}]


@pytest.mark.asyncio
async def test_aliases_inside_facts_become_display_names():
    llm = MemLLM({"P1": sec(), "ROOM": sec(),
                  "WALDEN": sec(add=[{"fact": "helps P1 (Alice) and P1 write; WALDEN agreed", "said_by": "P1"}])})
    result = await Consolidator(Config(), llm).consolidate(SESSION)
    assert result.operations[0]["fact"] == "helps Alice and Alice write; Walden agreed"


@pytest.mark.asyncio
async def test_person_section_only_holds_what_that_person_said():
    session = SESSION + [ev(3, "@bob:example.org", "Alice has a cat", name="Bob")]
    llm = MemLLM({"P1": sec(add=[{"fact": "has a cat", "said_by": "P2"}, {"fact": "prefers tea", "said_by": "P1"}]),
                  "P2": sec(), "WALDEN": sec(), "ROOM": sec()})
    result = await Consolidator(Config(), llm).consolidate(session)
    assert [o["fact"] for o in result.operations] == ["prefers tea"]


@pytest.mark.parametrize("fact,dropped", [
    ("is inspired by David Attenborough, Paulo Freire, Greta Thunberg and Rosa Luxemburg", True),
    ("honors the living soil and the nourishing food it gives", True),
    ("speaks honestly without softening the truth", True),
    ("was invited by DDH to co-write 'Walden 3'", False),
    ("is to help DDH clarify his ideas for people who know little about education", False),
])
def test_facts_that_restate_the_kernel_are_recognised(fact, dropped):
    from walden.persona import restates_prompt
    cfg = Config(); cfg.agent.system_prompt = "You are Walden, derived from Frank. Be truthful."
    assert restates_prompt(fact, cfg) is dropped


@pytest.mark.asyncio
async def test_walden_reciting_its_kernel_is_not_stored():
    llm = MemLLM({"P1": sec(), "ROOM": sec(), "WALDEN": sec(add=[
        {"fact": "honors the living soil and the nourishing food it gives", "said_by": "WALDEN"},
        {"fact": "co-writes Walden 3", "said_by": "P1"}])})
    result = await Consolidator(Config(), llm).consolidate(SESSION)
    assert [o["fact"] for o in result.operations] == ["co-writes Walden 3"]


@pytest.mark.asyncio
async def test_summaries_of_a_long_session_are_merged_into_one():
    class MergeLLM(LLM):
        prompt = None
        async def chat(self, messages, json_schema=None):
            MergeLLM.prompt = messages[-1]["content"]; return "DDH talked about Juni; The Archivist introduced themselves."
    c = Consolidator(Config(), MergeLLM())
    assert await c.merge_summaries(["only one."]) == "only one."
    assert await c.merge_summaries(["DDH talked about Juni.", "The Archivist introduced themselves."]) == "DDH talked about Juni; The Archivist introduced themselves."
    assert "Part 2: The Archivist introduced themselves." in MergeLLM.prompt and "at most 80 words" in MergeLLM.prompt


@pytest.mark.asyncio
async def test_doubled_names_in_summaries_are_collapsed():
    session = SESSION + [ev(3, "@arch:x", "hi", name="The Archivist")]
    llm = MemLLM({"P1": sec(), "P2": sec(), "WALDEN": sec(), "ROOM": sec()})
    llm.summary = "P2 (The Archivist) and The Archivist (The Archivist) greeted P1."
    async def chat(messages, json_schema=None):
        return json.dumps({"episode_summary": llm.summary, "episode_topics": [], "memory": llm.memory})
    llm.chat = chat
    result = await Consolidator(Config(), llm).consolidate(session)
    assert result.episode_summary == "The Archivist and The Archivist greeted Alice."


@pytest.mark.asyncio
async def test_subject_name_at_the_start_of_a_fact_is_removed():
    llm = MemLLM({"P1": sec(add=[{"fact": "Alice likes tea", "said_by": "P1"}]), "WALDEN": sec(add=[{"fact": "Walden helps Alice", "said_by": "P1"}]), "ROOM": sec()})
    result = await Consolidator(Config(), llm).consolidate(SESSION)
    assert [o["fact"] for o in result.operations] == ["likes tea", "helps Alice"]


@pytest.mark.asyncio
async def test_deep_pass_gleans_until_nothing_new():
    rounds = [
        {"facts": [{"about": "P1", "fact": "Alice founded a bakery"}, {"about": "P1", "fact": "prefers tea"}]},   # "prefers tea" is known
        {"facts": [{"about": "P1", "fact": "speaks Czech"}, {"about": "WALDEN", "fact": "speaks honestly and does not soften truth"}]},
        {"facts": []},
    ]
    class GleanLLM(LLM):
        prompts = []
        async def chat(self, messages, json_schema=None):
            GleanLLM.prompts.append(messages[-1]["content"]); return json.dumps(rounds[len(GleanLLM.prompts) - 1])
    known = {ALICE: [{"id": "f-1", "fact": "prefers tea"}]}
    long = [ev(1, ALICE, "This is what an AI wrote about me: ... (long text)", name="Alice")]
    ops = await Consolidator(Config(), GleanLLM()).extract_more(long, known, rounds=3)
    facts = [o["fact"] for o in ops]
    assert "founded a bakery" in facts and "speaks Czech" in facts                  # name stripped, second round used
    assert not any("soften truth" in f for f in facts)                               # Walden kernel restatement dropped
    assert all(o["said_by"] == ALICE and o["op"] == "add" for o in ops)
    assert len(GleanLLM.prompts) == 3 and "FOUND SO FAR:\n- Alice founded a bakery\n- prefers tea" in GleanLLM.prompts[1]
