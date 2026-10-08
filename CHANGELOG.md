# Changelog

## 0.2.0 — 2026-09-23

First Python-first Walden release.

- Matrix-native asynchronous agent using `matrix-nio`, including E2EE-capable sync/send paths.
- Stable `wal://person/<sui-id>` and `wal://room/<sui-id>` identities.
- Immutable encrypted Walrus person/room revision lineages.
- Immutable episodic memories with person and room historical snapshots.
- Optional Walrus Memory / MemWal semantic mirror.
- Local SQLite/file backends plus Sui CLI and Walrus CLI/HTTP adapters.
- OpenAI-compatible LLM integration, session consolidation, FastAPI interface and CLI.
- Single `config.yaml` configuration surface.
- Included Move package for Sui entity heads, tests, Dockerfile, CI and research paper.
