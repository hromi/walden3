# Deployment

## Hackathon path

1. Run local mode and `walden self-test`.
2. Serve Granite through an OpenAI-compatible endpoint and configure `llm`.
3. Create a dedicated Matrix bot and persist its nio store.
4. Install the official Walrus and Sui CLIs; fund the wallet with SUI/WAL.
5. Publish `move/walden_memory` and set its package ID.
6. Switch `memory.walrus.backend` to `cli` and `memory.sui.backend` to `cli`.
7. Generate the MemWal account/delegate credentials and optionally enable semantic search.
8. Test with a non-sensitive Matrix room before real users.

## Why CLI mode exists

The entire agent is Python. For transaction signing, CLI mode delegates to the official Sui/Walrus binaries instead of embedding a second, unofficial transaction-signing implementation. Walrus JSON mode is explicitly intended for programmatic access and returns JSON-only stdout.

## HTTP Walrus mode

If you operate an authenticated publisher, the Python process can store directly over HTTP and read through an aggregator. The publisher wallet pays on-chain storage costs. Do not expose an unrestricted funded publisher publicly.

## Retention

Walrus availability is epoch-bounded. A production job must extend important blobs before expiry. The included schema deliberately keeps blob IDs visible in lineages so a renewal crawler can walk current heads and renew all reachable revisions. Renewal automation is deployment-specific because the exact desired retention policy and funding wallet are policy choices.

## Walrus Memory on mainnet (this deployment)

Checkpoints (`!walden save`) are stored as memories of this Walrus Memory account through the hosted mainnet relayer `https://relayer.memory.walrus.xyz`:

| | |
|---|---|
| Agent / account ID (`MEMWAL_ACCOUNT_ID`) | `0x1979de15948d9fb7994ec9a78e313e7a454818050e2853142e81f13c73282094` |
| Account owner (Sui address, alias `memwalOwner` in the local keystore) | `0xbe54a8cf260e068dea6c4ce23e91c430af6cbd10791289e0c2d9e64b359473d5` |
| Delegate key used by Walden (`MEMWAL_PRIVATE_KEY`), its Sui address | `0x3f157b28b44e7abab3a4fb981925b2f6e39408ab42da9e2e7fd6da52e648bd68` |
| Walrus Memory package / AccountRegistry used by the relayer | `0xe7c16fbea0560e7057e2bf7422feaa4fb313749fc69c9e9092fac7a33b81d7f5` / `0x8bf82c9e09e36b8d1c38298f68b7cb68e7b8762887e7592add9986d5e9cf199f` |

The package and registry IDs in the Walrus Memory docs (`0xcee7…24c6` / `0x0da9…7edd`) belong to an older deployment that the relayer no longer serves; the relayer's own `GET /config` shows the current package. An account created there (`0x4428e06c…e0cf`) is unused. The owner's recovery phrase is in `~/.sui/sui_config/memwal-owner.recovery.json`. To revoke Walden's access, the owner calls `account::remove_delegate_key` with the delegate's public key.
