# On-chain checkpoints (Walrus)

## Where checkpoints are stored

`memory.checkpoint.backend` chooses the store:

- **`memwal` (default): Walrus Memory on mainnet.** Each checkpoint is one memory of Walden's Walrus Memory agent account (`MEMWAL_ACCOUNT_ID`, written with the delegate key `MEMWAL_PRIVATE_KEY`), in one namespace per room (`walden-<room>`). The hosted relayer (`relayer.memory.walrus.xyz`) pays storage and Seal-encrypts the memory before writing it to Walrus. Walden sends the checkpoint already AES-encrypted, so the relayer only sees the counts in the header and ciphertext. Walrus Memory has no read-by-ID, so `load` and `verify` list the room's namespace and pick the blob; Walden's own encryption and content hash still prove the content is exactly what it stored. Checkpoints are limited to 64 KiB.
- **`walrus`: a plain Walrus blob**, written with the `walrus` CLI on `memory.checkpoint.context` (testnet by default). The header is publicly readable, the blob's Sui object is tagged with `walden.*` attributes, and you manage its lifetime (`walrus extend`). The sections below about the public header, Sui attributes, epochs and costs refer to this backend.

While people talk, Walden's memory lives in the local store: facts are saved after each exchange, for free and instantly. A **checkpoint** publishes a room's current memory to Walrus as one blob, on request and after confirmation.

```text
!walden save                 build the checkpoint, price it on Walrus (dry run), show the overview, wait for "yes"
!walden checkpoints          list checkpoints of rooms you are in, by name: walden#1, walden#2, ...
!walden episodes [room]      list the episodes of a room's latest checkpoint: walden/e1, walden/e2, ...
!walden load <what> [key]    show what loading would change here, wait for "yes"
yes / no                     confirm or cancel (only the person who asked, within 5 minutes)
!walden verify [blob id]     download a checkpoint from Walrus, decrypt it, check hashes and the chain
```

`<what>` can be:

| `<what>` | loads |
|---|---|
| `walden`, `walden#2` | the latest checkpoint of the room named "Walden", or its checkpoint #2 |
| `walden/e3` | only episode 3 of that room: its summary and the facts learned in it |
| `!roomid:server`, `#alias:server` | the latest checkpoint of that room |
| `@you:server` | only what Walden knows about you, from the latest checkpoint of every other room you are in (your own ID only) |
| a Walrus blob ID | that checkpoint; add `key` for a checkpoint made by another Walden |

**You can only load memory from rooms you are a member of**, whichever way you name them. Blob IDs and the `walden.room` tag of a checkpoint are public on-chain, so without this rule anyone moderating any room could load another room's memory. A checkpoint from another Walden, loaded with its key, is the exception: holding that key is the permission. Loading one episode or one person is a partial load: it adds and updates, but never removes anything.

`!walden deploy` still works as the old name of `save`. Only room moderators (power level 50 or more) can save or load, unless `memory.checkpoint.deploy_users` lists specific users. The network is `memory.checkpoint.context`: `testnet` by default; mainnet must be set explicitly.

## What is stored

One blob per checkpoint:

```text
WALDEN-CHECKPOINT/1
{"schema": "walden/checkpoint/1", "seq": 2, "previous": "<blob id of #1>", "previous_content_sha256": "…",
 "room": "wal://room/0x…", "counts": {...}, "content_sha256": "…", "kernel_sha256": "…", ...}
{AES-256-GCM envelope}
```

- **Public header:** what is inside (counts only), when it was made, the hash of the encrypted content, and a link to the previous checkpoint of the room. Anyone with the link can read the header and walk the chain.
- **Encrypted payload:** every fact learned in this room about its people, about Walden and about the room, each with who said it and when, plus the episode summaries. Facts from other rooms are never included.
- **`kernel_sha256`:** a hash of Walden's immutable value kernel (inherited from Frank). Every checkpoint commits to the values the agent ran with.
- **Sui attributes:** the blob's Sui object is tagged with `walden.kind`, `walden.room`, `walden.seq`, `walden.previous` and `walden.content_sha256`, so checkpoints can be found on-chain without Walden's database.

## Loading: carrying memory into another room

Walden joins every room it is invited to. `!walden load <blob id>` downloads a checkpoint, decrypts it and checks its content hash, then shows what it would add here: facts per person, about Walden and about the room, plus past episodes. On "yes" it copies them into this room. Each copy keeps who said it and when, and records where it came from (`imported_from`: blob, source room, original fact and episode). Facts this room already knows, and Walden reciting its own kernel, are skipped; loading the same checkpoint twice adds nothing.

A checkpoint holds the room's whole memory at that moment, so loading the latest one transfers everything; earlier ones in the chain are history.

Loads add up: loading checkpoints of rooms A, B and C one after another merges all three. **Re-loading from the same room syncs.** Every imported fact records its source room, checkpoint number and original fact id, and a fact keeps its id when it is reworded in its room. So when a newer checkpoint of a room is loaded where an older one of the same room was loaded before:

- facts reworded in the source room are updated here, instead of appearing twice;
- facts no longer in the source room's memory (forgotten or retracted there) are removed here;
- new facts are added.

The room's own facts and facts loaded from other rooms are never touched. Loading a checkpoint *older* than one already loaded from that room only adds what is missing and changes nothing, so it cannot undo newer changes. The overview before "yes" shows, per person, how many facts would be added, updated and removed.

People are matched by their pseudonymous reference, which is the same for this Walden in every room. For a checkpoint from another Walden instance (pass its key), people are matched by display name with this room's members; anyone who cannot be matched is skipped and named in the overview.

Loading moves facts about people into a room where they may not be present, so only moderators can do it, and the overview names whose facts would move. A key posted in the room is visible to everyone there: Walden deletes that message if it is allowed to, and says so.

## Verifying

`!walden verify` downloads the checkpoint from Walrus, decrypts it, checks that the content matches the hash in its header, and follows `previous` links back through the room's history. Each step also checks that the earlier checkpoint matches the hash its successor recorded. A changed, swapped or foreign-room checkpoint fails verification.

## Demo script

1. Create a new Matrix room and invite `@walden:udk.ai` (you are its admin, so you can deploy).
2. Talk: "@walden I have two cats, Ada and Turing", "@walden you will help me write a children's book".
3. `!walden memory` and `!walden self` show the facts, with who said them.
4. `!walden save` shows contents, size, cost, wallet balance and the future blob ID. Answer `yes`.
5. Walden replies with the blob ID, Sui object and explorer links. Open the aggregator link: the header is readable, the facts are not.
6. `!walden verify` proves the round trip from Walrus. Talk more, deploy again, and `verify` shows the chain #2 → #1.
7. Invite Walden into a second room, run `!walden load <blob id>` there and answer `yes`: ask Walden about the people from the first room.
