# `wal://` URI scheme

The `wal://` scheme is an application-level Walden convention. It is not a native Walrus or Sui protocol URI.

```text
wal://person/<sui-object-id>
wal://room/<sui-object-id>
wal://episode/<walrus-blob-id>
wal://blob/<walrus-blob-id>
```

## Stable versus immutable references

`person` and `room` references are **stable names**. Resolving one means reading its Sui entity object and following `head_blob_id` to the current Walrus revision.

`episode` and `blob` references are **immutable names**. Resolving one reads that exact Walrus content ID.

An episode therefore stores both kinds of link:

```json
{
  "person": "wal://person/0x...",
  "memory_at_time": "wal://blob/..."
}
```

The first follows the person into the present; the second preserves Walden's historical state of knowledge at the episode time. The room fields use the same pattern.
