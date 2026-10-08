# Matrix integration

Walden uses `matrix-nio` 0.26+ for asynchronous sync and Olm/Megolm encrypted-room support. `sync_forever()` also handles recurring encryption/key-management work required by encrypted rooms.

For a durable deployment, create a dedicated Matrix account/device and use an access token in `config.yaml`. Keep `matrix.store_path` persistent across restarts; it contains the Matrix crypto/session state.

`ignore_unverified_devices: false` is the safer default. In an encrypted room, a send can fail when unverified devices are present until those devices are verified. Setting it to true is operationally easier but deliberately relaxes the device-trust check.

## Verifying the bot's session

With `encryption: true` Walden answers interactive emoji verification. In Element, logged in as the bot account, open Settings → Sessions, pick the bot's session and choose Verify. Walden accepts the request, logs the emoji at WARNING level (`compare emoji: ...`) and confirms its side automatically, because a bot cannot see your screen. Compare the emoji in Element with the log line yourself and only confirm if they match.

Only users listed in `matrix.verification_users` can verify with the bot. If the list is empty, only the bot's own account can. matrix-nio has no cross-signing support, so after verification Element trusts the bot's device, but the bot cannot cross-sign itself. Some other users may still see the device as unverified.

Walden handles ordinary `m.room.message` text, encrypted text after nio decrypts it, invitations (optional auto-join), outbound text/notices, session buffering, and its command surface. Human turns and Walden replies may both be included in an episode, but only human senders create `wal://person/...` entities.

Commands:

```text
!walden help
!walden close
!walden memory
!walden self
!walden forget <fact id>
!walden save
!walden checkpoints
!walden episodes [room]
!walden load <what> [key]
!walden verify [blob id]
!walden lineage
!walden health
```

`!walden archive` archives a whole room (see [ARCHIVE.md](ARCHIVE.md)). `!walden save`, `!walden load` and `!walden verify` publish, load and check on-chain checkpoints of the room's memory on Walrus; see [CHECKPOINTS.md](CHECKPOINTS.md).

`!walden close` forces an immediate durable session commit even when `matrix.session.auto_commit` is false.

For each reply Walden puts into the prompt: the current speaker's person memory, the person memory of everyone the message mentions, the room memory, and the summaries of the room's most recent episodes (`recall.max_episodes`). A mention is a Matrix mention (pill), or a room member's display name or username appearing as a word in the message. Other people's memory is never loaded, and there is never a global search over every known person's private memory.

## Memory as facts

Each person's memory, Walden's own memory and the room's memory are lists of single facts. Each fact records who said it, in which room, in which episode and on which date:

```json
{"id": "f-1a2b3c", "fact": "has a dog named Juni", "said_by_name": "DDH", "room": "wal://room/…", "episode": "wal://episode/…", "at": "2026-09-24"}
```

Facts are saved during the conversation: in the background after each Walden reply, or after `matrix.session.save_every` messages without one (default 5). The reply is never delayed by it. When the session ends, the rest is saved, the episode is written, and the facts from that session are linked to it. Each save reads only the new messages, plus the previous few as context. The model sees the facts already known for that room and returns only what changed: `add` a new fact, `update` a fact by id, or `retract` one. Every change is a new revision, so old facts stay in the lineage. Walden is a participant too: roles and tasks people give it, its commitments, projects and views are facts about Walden, credited to whoever said them.

Facts never leave the room they were learned in. Only facts from the current room go into the prompt or into the save step, even though a person has one memory across rooms. This is enforced in code, not by the prompt.

`!walden memory` lists what Walden knows about you in this room, `!walden self` what it knows about itself, both with fact ids. Both first save (locally) anything said since the last save, so the listing includes the latest messages. `!walden forget <id>` removes a fact about you, or one you told Walden about itself or the room.

People are shown to the model by display name. Raw Matrix user IDs are never sent to the model. Episode summaries also refer to people by display name.
