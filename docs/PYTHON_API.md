# Python integration API

Walden is usable as a Matrix bot or as a library. The important boundary is `MemoryGraph`; storage/index implementations satisfy small async ports in `walden/ports.py`.

```python
from walden.config import load_config
from walden.factory import build

cfg = load_config("config.yaml")
agent = build(cfg)
graph = agent.graph

alice = await graph.ensure_person("@alice:example.org")
room = await graph.ensure_room("!room:example.org")

await graph.update_person(
    alice,
    {"projects": {"walden": {"status": "active"}}},
    {"source": "explicit-user-statement"},
)

current = await graph.resolve(alice)
history = await graph.lineage(alice)

episode = await graph.create_episode(
    room_ref=room,
    person_refs=[alice],
    when={"start": "2026-09-23T12:00:00Z", "end": "2026-09-23T12:20:00Z"},
    where={"kind": "matrix-room"},
    what={"type": "conversation", "summary": "Discussed Walden memory graphs."},
    source={"protocol": "matrix"},
)
```

## Ports

`walden/ports.py` defines four extension points:

- `BlobStore`: exact immutable bytes by content ID;
- `EntityRegistry`: stable mutable person/room heads;
- `BindingStore`: private external-identity → stable-object binding;
- `SemanticIndex`: optional relevance mirror.

The included factory selects implementations entirely from `config.yaml`. A different database, hardware wallet, Sui SDK, Seal implementation, or vector store can therefore be added without changing the memory-graph model.

## Revision rules

`update_person()` and `update_room()` accept **sparse patches**. Walden reads the current complete snapshot, deep-merges the patch, writes a new complete immutable snapshot, and advances the stable head. `create_episode()` never mutates a person or room; it snapshots their current blob IDs and writes an immutable episode.
