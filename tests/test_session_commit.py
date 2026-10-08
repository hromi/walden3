import pytest
from pathlib import Path

from walden.config import Config
from walden.adapters.local import LocalBlobStore, SQLiteRegistry, NullSemanticIndex
from walden.memory_graph import MemoryGraph
from walden.agent import WaldenAgent
from walden.llm import EchoLLM
from walden.session import Session
from walden.core.models import MatrixEventRecord


@pytest.mark.asyncio
async def test_assistant_turn_is_in_episode_but_not_a_person(tmp_path: Path):
    cfg = Config()
    cfg.matrix.user_id = "@walden:example.org"
    cfg.memory.identity_hmac_key = "identity-test"
    cfg.memory.encryption_key = "encryption-test"
    cfg.memory.walrus.local_dir = str(tmp_path / "blobs")
    cfg.memory.sui.local_db = str(tmp_path / "db.sqlite")
    cfg.memory.consolidation.enabled = False
    cfg.memory.include_raw_transcript_in_episode = True

    store = LocalBlobStore(cfg.memory.walrus.local_dir)
    reg = SQLiteRegistry(cfg.memory.sui.local_db)
    graph = MemoryGraph(cfg, store, reg, reg, NullSemanticIndex())
    agent = WaldenAgent(cfg, graph, EchoLLM())

    session = Session("!room:example.org")
    session.add(MatrixEventRecord(
        event_id="$human", room_id=session.room_id, sender="@alice:example.org",
        timestamp_ms=1, body="Hello", role="human"
    ))
    session.add(MatrixEventRecord(
        event_id="$walden", room_id=session.room_id, sender=cfg.matrix.user_id,
        timestamp_ms=2, body="Hello Alice", role="assistant"
    ))

    episode_ref = await agent.commit_session(session, "test")
    episode = await graph.resolve(episode_ref)
    assert [w["role"] for w in episode["who"]] == ["participant", "self"]
    assert episode["who"][1]["person"] == await agent.self_ref()
    assert episode["raw_transcript"][0]["role"] == "human"
    assert episode["raw_transcript"][1]["role"] == "assistant"
