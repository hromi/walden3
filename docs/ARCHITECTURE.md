# Architecture

## Temporal hypertext

```text
Matrix @alice ──private binding──► wal://person/0xA
                                      │
                                      ▼
                                 Sui Entity 0xA
                                 head = blob C
                                      │
                                      ▼
                        Walrus C ──previous──► B ──► A

Matrix room !r ────────────────► wal://room/0xR
                                      │
                                      ▼
                                 Sui Entity 0xR
                                 head = room blob N

                                  episode E
                         ┌────────────┼────────────┐
                         │            │            │
                  person 0xA   person 0xB    room 0xR
                  snapshot C   snapshot K    snapshot N
```

Stable Sui object IDs form persistent names. Immutable encrypted Walrus blobs form historical pages. `wal://` references form the hypertext.

## Bitemporality

An episode participant contains:

```json
{"person":"wal://person/0xA", "memory_at_time":"wal://blob/<C>"}
```

The stable ref answers "who is this and what do I know now?"; the snapshot answers "what did Walden know at the time of this episode?". Rooms have the same two-reference structure.

## Interpersonal memory

A current person revision is a complete snapshot, not a delta. Session consolidation produces sparse patches, but Walden deep-merges those patches with the previous complete snapshot before writing the next revision. This makes every head independently intelligible while preserving a full chain.

## Episodic memory

Episodes are immutable and do not require Sui objects. They record when, where, what, participants, room, source event IDs, and (optionally) a transcript. They point into historical person/room revisions.

## Exact storage vs semantic indexing

Walrus blobs are the source of truth. MemWal is a secondary index. A semantic service outage must never make a successful graph write invalid. Likewise, exact lineage traversal never depends on nearest-neighbor retrieval.
