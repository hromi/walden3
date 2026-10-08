from types import SimpleNamespace
import pytest

from walden.config import Config
from walden.matrix_service import MatrixService


class FakeGraph:
    pass


class FakeAgent:
    def __init__(self):
        self.graph = FakeGraph()
        self.committed = []

    async def respond(self, event, mentioned, recent_events=None, names=None, carried=False):
        assert event.role == "human"
        assert mentioned == []
        assert recent_events and recent_events[-1].body == event.body
        return "Hello from Walden"

    async def commit_session(self, session, reason="session-end"):
        self.committed.append((session, reason))

    async def save_facts(self, session):
        self.saved = getattr(self, "saved", []) + [len(session.events)]
        session.saved = len(session.events)


class FakeClient:
    def __init__(self):
        self.sent = []

    async def room_send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(event_id="$walden-reply")


@pytest.mark.asyncio
async def test_matrix_turn_buffers_human_and_assistant_together():
    cfg = Config()
    cfg.matrix.user_id = "@walden:example.org"
    cfg.matrix.reply_mode = "all"
    cfg.matrix.session.max_events = 100
    agent = FakeAgent()
    service = MatrixService(cfg, agent)
    service.client = FakeClient()

    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        event_id="$human",
        sender="@alice:example.org",
        body="Hello",
        server_timestamp=123,
    )
    await service.on_message(room, event)

    session = service.sessions.sessions[room.room_id]
    assert [x.role for x in session.events] == ["human", "assistant"]
    assert session.events[1].event_id == "$walden-reply"
    assert session.events[1].sender == cfg.matrix.user_id
    assert service.client.sent[0]["content"]["body"] == "Hello from Walden"


def test_speaker_label_is_stripped_from_replies():
    from walden.agent import SPEAKER_LABEL
    assert SPEAKER_LABEL.sub("", "[wal://person/0xabc] You have three daughters.") == "You have three daughters."
    assert SPEAKER_LABEL.sub("", "See [wal://person/0xabc] here") == "See [wal://person/0xabc] here"
    assert SPEAKER_LABEL.sub("", "Walden: DDH, you introduced yourself.") == "DDH, you introduced yourself."
    assert SPEAKER_LABEL.sub("", "Walden is a book.") == "Walden is a book."


def test_inline_memory_refs_are_removed_from_replies():
    from walden.agent import clean_reply
    assert clean_reply("Walden: DDH (wal://person/0x84) met Walden (wal://person/0x6f) in wal://room/0x1.") == "DDH met Walden in."
    assert clean_reply("[wal://person/0xa] Hi [wal://person/0xb] there") == "Hi there"


class FakeRoom:
    room_id = "!room:example.org"
    users = {"@ddh:example.org": None, "@arch:example.org": None, "@walden:example.org": None, "@al:example.org": None}
    _names = {"@ddh:example.org": "DDH", "@arch:example.org": "The Archivist", "@walden:example.org": "walden", "@al:example.org": "Al"}

    def user_name(self, uid):
        return self._names.get(uid)


def _service():
    cfg = Config()
    cfg.matrix.user_id = "@walden:example.org"
    return MatrixService(cfg, FakeAgent())


def _ev(body, sender="@arch:example.org", content=None):
    return SimpleNamespace(sender=sender, body=body, source={"content": content or {"body": body}})


def test_mentions_by_plain_display_name_excluding_bot_and_sender():
    s = _service()
    assert s._mentioned(FakeRoom(), _ev("@walden tell me something about DDH"), "@walden tell me something about DDH") == ["@ddh:example.org"]
    # Whole words only: "Al" must not match inside "also"; the sender is never "mentioned".
    assert s._mentioned(FakeRoom(), _ev("also The Archivist"), "also The Archivist") == []


def test_mentions_from_matrix_metadata_and_pills():
    s = _service()
    content = {"body": "hi", "m.mentions": {"user_ids": ["@walden:example.org", "@zoe:elsewhere.org"]},
               "formatted_body": '<a href="https://matrix.to/#/%40ddh%3Aexample.org">x</a>'}
    assert s._mentioned(FakeRoom(), _ev("hi", content=content), "hi") == ["@zoe:elsewhere.org", "@ddh:example.org"]


@pytest.mark.parametrize("body,content,expected", [
    ("I think walden is slow", None, False),
    ("Walden 3 is the book title", None, False),
    ("@walden what is your task?", None, True),
    ("hey @Walden!", None, True),
    ("ping @walden:example.org", None, True),
    ("walden: hi", {"body": "walden: hi", "m.mentions": {"user_ids": ["@walden:example.org"]}}, True),
    ("walden hi", {"body": "walden hi", "formatted_body": '<a href="https://matrix.to/#/@walden:example.org">walden</a> hi'}, True),
    ("@waldenfan hi", None, False),
])
def test_mentions_mode_needs_explicit_mention(body, content, expected):
    s = _service()
    s.cfg.matrix.reply_mode = "mentions"
    assert s._should_reply(FakeRoom(), _ev(body, sender="@ddh:example.org", content=content), body) is expected


@pytest.mark.asyncio
async def test_facts_are_saved_after_each_exchange_and_every_n_messages():
    import asyncio
    cfg = Config()
    cfg.matrix.user_id = "@walden:example.org"; cfg.matrix.reply_mode = "mentions"; cfg.matrix.session.save_every = 3
    agent = FakeAgent()
    agent.respond = lambda *a, **k: asyncio.sleep(0, result="Hi")
    s = MatrixService(cfg, agent); s.client = FakeClient()
    room = FakeRoom()
    async def say(i, body):
        await s.on_message(room, SimpleNamespace(event_id=f"${i}", sender="@ddh:example.org", body=body, server_timestamp=i,
                                                 source={"content": {"body": body}}))
        await asyncio.sleep(0)
    await say(1, "@walden hello")          # exchange: human + reply -> save
    assert agent.saved == [2]
    await say(2, "just chatting"); await say(3, "still chatting")
    assert agent.saved == [2]              # 2 unsaved messages, below save_every
    await say(4, "and more")
    assert agent.saved == [2, 5]           # 3 unsaved messages without a reply -> save


def test_sentences_walden_already_said_are_dropped():
    from walden.agent import drop_repeats
    earlier = ["You are a junior professor. You have three daughters and a dog named Juni."]
    reply = "You are a junior professor. You have three daughters and a dog named Juni.\n\nThe Archivist is the first m3x.baumhaus bot."
    assert drop_repeats(reply, earlier) == "The Archivist is the first m3x.baumhaus bot."
    assert drop_repeats("You are a junior professor.", earlier) == "You are a junior professor."  # all repeats: keep
    reworded = "You are a junior professor, with three daughters and a dog named Juni. Juni loves the park."
    assert drop_repeats(reworded, earlier) == "Juni loves the park."


@pytest.mark.parametrize("q,meaning", [
    ("@walden and who are You ?", "@walden and who is Walden ?"),
    ("what do you know about me?", "what does Walden know about DDH?"),
    ("what do You know about the archivist ?", "what does Walden know about the archivist ?"),
    ("who am I ?", "who is DDH ?"),
    ("I have three daughters and my dog is Juni", "DDH has three daughters and DDH's dog is Juni"),
    ("you will help me with your skills", "Walden will help DDH with Walden's skills"),
])
def test_pronouns_are_resolved_for_the_model(q, meaning):
    from walden.agent import resolve_pronouns
    assert resolve_pronouns(q, "DDH") == meaning


def test_same_fact_told_to_someone_else_is_not_a_repeat():
    from walden.agent import drop_repeats
    earlier = ["You are a junior professor of digital education at Berlin University of the Arts. You have three daughters."]
    reply = "DDH is a junior professor of digital education at Berlin University of the Arts."
    assert drop_repeats(reply, earlier) == reply



@pytest.mark.asyncio
async def test_he_she_they_carry_over_who_the_previous_question_was_about():
    import asyncio
    cfg = Config(); cfg.matrix.user_id = "@walden:example.org"; cfg.matrix.reply_mode = "mentions"; cfg.matrix.session.save_every = 0
    agent = FakeAgent(); calls = []
    async def respond(event, mentioned, recent, names, carried=False):
        calls.append((event.body, mentioned, carried)); return "ok"
    agent.respond = respond
    s = MatrixService(cfg, agent); s.client = FakeClient(); room = FakeRoom()
    async def say(i, who, body):
        await s.on_message(room, SimpleNamespace(event_id=f"${i}", sender=who, body=body, server_timestamp=i, source={"content": {"body": body}}))
    await say(1, "@arch:example.org", "@walden what city did DDH visit recently?")
    await say(2, "@arch:example.org", "@walden and what did he learn there?")
    await say(3, "@arch:example.org", "@walden what is the weather?")
    await say(4, "@arch:example.org", "@walden and what did he say?")       # nothing to carry over any more
    assert calls == [("@walden what city did DDH visit recently?", ["@ddh:example.org"], False),
                     ("@walden and what did he learn there?", ["@ddh:example.org"], True),
                     ("@walden what is the weather?", [], False),
                     ("@walden and what did he say?", [], False)]


def test_stale_replies_are_left_out():
    from walden.agent import drop_stale_replies
    from walden.core.models import MatrixEventRecord as R
    ev = lambda i, role, body: R(event_id=f"${i}", room_id="!r", sender="@d:x" if role == "human" else "w", timestamp_ms=i, body=body, role=role)
    h = [ev(1, "human", "where did I go?"), ev(2, "assistant", "You have not told me."),
         ev(3, "human", "hello"), ev(4, "assistant", "Hi!"),
         ev(5, "system", "Memory was loaded from checkpoint #1: 3 facts added."), ev(6, "human", "thanks"), ev(7, "assistant", "You're welcome.")]
    assert [t.body for t in drop_stale_replies(h, "something else")] == [
        "where did I go?", "hello", "Memory was loaded from checkpoint #1: 3 facts added.", "thanks", "You're welcome."]
    h2 = [ev(1, "human", "Where did I go?"), ev(2, "assistant", "You have not told me."), ev(3, "human", "ok")]
    assert [t.body for t in drop_stale_replies(h2, "where did I go ?")] == ["Where did I go?", "ok"]


@pytest.mark.parametrize("body,answer", [
    ("yes", True), ("Yes!", True), ("@walden yes please", True), ("walden: ok", True), ("ja", True), ("do it", True),
    ("no", False), ("Nein.", False), ("@walden cancel", False),
    ("yesterday I went to Brussels", None), ("what do you know about me?", None), ("@walden who are you?", None),
])
def test_confirmation_answers(body, answer):
    assert _service()._confirmation(body) is answer


def test_punctuation_between_sentences_survives_the_repeat_filter():
    from walden.agent import drop_repeats
    reply = "The article is titled Entwachstum (Degrowth ? Now!) It describes the curriculum."
    assert drop_repeats(reply, ["Something else entirely."]) == reply
