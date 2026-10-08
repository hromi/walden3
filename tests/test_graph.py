import pytest
from pathlib import Path
from walden.config import Config
from walden.adapters.local import LocalBlobStore, SQLiteRegistry, NullSemanticIndex
from walden.memory_graph import MemoryGraph

@pytest.mark.asyncio
async def test_temporal_graph(tmp_path:Path):
    cfg=Config()
    cfg.memory.identity_hmac_key="identity-test"
    cfg.memory.encryption_key="encryption-test"
    cfg.memory.walrus.local_dir=str(tmp_path/"blobs")
    cfg.memory.sui.local_db=str(tmp_path/"db.sqlite")
    store=LocalBlobStore(cfg.memory.walrus.local_dir); reg=SQLiteRegistry(cfg.memory.sui.local_db)
    g=MemoryGraph(cfg,store,reg,reg,NullSemanticIndex())
    p=await g.ensure_person("@alice:example.org"); r=await g.ensure_room("!r:example.org")
    await g.update_person(p,{"preferences":{"drink":"tea"}})
    await g.update_person(p,{"projects":{"walden":"active"}})
    current=await g.resolve(p)
    assert current["memory"]["preferences"]["drink"]=="tea"
    assert current["memory"]["projects"]["walden"]=="active"
    line=await g.lineage(p); assert len(line)==3
    ep=await g.create_episode(room_ref=r,person_refs=[p],when={"start":1,"end":2},where={},what={"summary":"hello"},source={})
    e=await g.resolve(ep)
    assert e["who"][0]["person"]==p and e["who"][0]["memory_at_time"].startswith("wal://blob/")
    assert e["room_memory_at_time"].startswith("wal://blob/")
