# Security and privacy notes

Walden separates public infrastructure identifiers from private social identity.

- Raw Matrix user IDs and room IDs are HMAC-pseudonymized before durable entity registration.
- The raw Matrix → Sui-object binding lives in the private SQLite binding store, not on Sui.
- Human identifiers are replaced with temporary `P1`, `P2`, … aliases before LLM-based session consolidation.
- The ordinary response prompt uses stable `wal://person/...` references rather than MXIDs.
- Exact graph payloads are AES-256-GCM encrypted before upload to Walrus. Walrus must be treated as public storage.
- MemWal is a secondary semantic mirror. Do not place information there that violates the deployment's semantic-memory policy.
- The Matrix E2EE store must persist across restarts and should be protected like other credentials.

The included HMAC pseudonym is stable and therefore linkable if someone already knows multiple Sui object IDs belong to the same deployment. It is pseudonymization, not anonymity.

The current Move object stores the HMAC-derived key hash so the entity remains self-describing to Walden. A stricter deployment can replace the included `EntityRegistry` implementation with one that stores only an opaque entity ID and head pointer on-chain.
