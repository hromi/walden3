# Upstream interfaces used

Walden is designed against public interfaces checked in September 2026. Pin working versions in production and rerun integration checks after upgrades.

- **Walrus client JSON mode:** programmatic `walrus json` store/read; reads without `out` return Base64 in `blob`.
  https://docs.wal.app/docs/walrus-client/json-mode
- **Walrus Memory Python SDK:** `MemWal.create`, `remember_and_wait`, `recall(RecallParams(...))`.
  https://docs.wal.app/walrus-memory/python-sdk/usage/memwal
  https://docs.wal.app/walrus-memory/python-sdk/api-reference
- **Matrix / matrix-nio 0.26:** asynchronous `AsyncClient`, `room_send`, `sync_forever`, persistent E2EE store.
  https://matrix-nio.readthedocs.io/en/latest/nio.html
  https://matrix-nio.readthedocs.io/en/latest/examples.html
- **Sui object model:** object IDs are persistent while contents and versions can change.
  https://docs.sui.io/develop/write-move/sui-move-concepts
- **Sui CLI:** Move build/publish and `sui client call` operations.
  https://docs.sui.io/doc/sui-cli-cheatsheet.pdf

The authoritative Walden graph deliberately uses exact-addressed encrypted Walrus blobs. MemWal is a semantic mirror rather than the exact lineage store.
