# Walrus Memory: integration friction, bugs and ideas

What we ran into while integrating Walrus Memory into Walden3, a Matrix chatbot running a **local open-weight model** (IBM Granite 4.0 H-Small, `granite4:small-h`, via **Ollama 0.34** on an NVIDIA A40). Environment: Linux 5.15, Python 3.12, `memwal` Python SDK 0.1.11, hosted mainnet relayer `https://relayer.memory.walrus.xyz` (relayer 0.1.0, API 1.0.0), `sui` CLI 1.79.1, `walrus` CLI 1.56.0. Dates: 2–8 October 2026.

## The model and runtime

The model itself caused no friction with Walrus Memory: the relayer computes embeddings on its side, so a local model needs no embedding setup, and Walden's own structured extraction runs against Ollama's OpenAI-compatible endpoint with `response_format: json_schema` (strict), which Ollama enforces. The friction was all in accounts, deployment metadata, payload size and the SDK surface, below.

One design consequence of using a managed relayer with a privacy-sensitive, local-model setup: the relayer sees plaintext in order to embed it. Walden therefore encrypts every checkpoint itself (AES-256-GCM) before calling `remember`, so the relayer only sees ciphertext and a small public header. The price is that the relayer's semantic search is useless for those memories; Walden does its own recall.

## Bugs and friction (ready to file as GitHub issues)

### 1. Mainnet package and registry IDs in the docs point at a deployment the relayer no longer serves (401)

- **Steps:** follow *Contract → Overview* for mainnet (`MEMWAL_PACKAGE_ID=0xcee7a6fd…24c6`, `MEMWAL_REGISTRY_ID=0x0da982ce…7edd`); `create_account(registry, clock)`, then `add_delegate_key`; use the delegate key with the Python SDK against `https://relayer.memory.walrus.xyz`.
- **Expected:** `list_namespaces()` / `remember()` work.
- **Actual:** `401 … typically wrong private key, key not registered on this account, account ID mismatch, or staging/mainnet mismatch`. The relayer's `GET /config` reports `packageId: 0xe7c16fbe…d7f5`, a separate deployment (version 1, its own types). Its `AccountRegistry` is `0x8bf82c9e…199f`, which we had to find with a GraphQL `objects(filter: {type: "…::account::AccountRegistry"})` query. Recreating the account there fixed it.
- **Suggestion:** update the IDs in the docs; add `registryId` to the relayer's `/config`; make the 401 message mention "account belongs to a different package than this relayer" when that is the case (the relayer can tell from the account object's type).

### 2. `add_delegate_key` signature in the docs does not match the package the docs point at

- With the documented (old) package, `add_delegate_key` is `(account, public_key: vector<u8>, sui_address: address, label, clock)`; the docs show `(account, registry, public_key, label, clock)`, which is the signature of the *current* package. The CLI fails with `Could not serialize argument of type Address at 2`. Same root cause as 1, but confusing on its own.

### 3. "durable upload … exceeded step budget" for memories well under the 64 KiB limit

- **Steps:** `remember_and_wait(text)` with `text` of ~50–60 KB (under `MAX_REMEMBER_TEXT_BYTES` = 64 KiB) on the mainnet relayer.
- **Expected:** stored, or rejected up front if too large.
- **Actual:** the job is accepted (202), polls for about a minute, then `MemWalRememberJobFailed: remember job failed: durable upload for job 59d52b44-45ef-44cd-9e14-9426e9210902 exceeded step budget`. Memories up to ~35 KB went through reliably (35 in a row, after we capped our episodes at 12,000 characters).
- **Suggestion:** document a practical size; reject oversized payloads synchronously; or let the durable upload scale its budget with size.

### 4. Python SDK: no way to read a memory by its blob ID or to list a namespace

- `recall` needs a query and returns the top-k by similarity. To read back "checkpoint X", Walden recalls the namespace with a high limit and filters by `blob_id`. `restore` is limited and has no cursor.
- **Suggestion:** `get(blob_id)` and `list(namespace, cursor)` in the SDK and relayer.

### 5. Python SDK: no account helpers

- `generateDelegateKey` / `addDelegateKey` / `createAccount` exist only in the TypeScript SDK. In Python we generated the key with PyNaCl and called the Move functions with `sui client call` (see the README). A Python equivalent, or a documented CLI recipe, would help.

### 6. Retention of relayer-written memories is undocumented

- The docs explain epochs and renewal for self-managed blobs, but not how long the hosted relayer keeps memories it writes, or whether it renews them. For archives we promise people a date; we can only state it for the Walrus blobs we pay for ourselves.

## Friction outside Walrus Memory

- **Sui public fullnodes:** `sui_getNormalizedMoveFunction` returns `Method not found. JSON-RPC on public fullnodes has been deprecated`; GraphQL works.
- **Walrus CLI 1.56 and Sui address balances:** testnet faucet SUI arrives as an *address balance*; `walrus get-wal` then fails with `could not find SUI coins with sufficient balance` until you split coins out of the address balance with a PTB.
- **Matrix media:** homeservers now require authenticated media (`/_matrix/client/v1/media`); `matrix-nio` 0.26's `download()` calls it without the access token (401). Walden downloads with its own HTTP request.

## Improvement ideas

1. `get(blob_id)` and `list(namespace)` in the SDK (4).
2. Publish the full deployment metadata (package, registry, network) at the relayer's `/config` (1).
3. Python account and delegate-key helpers (5).
4. A documented, enforced maximum memory size (3).
5. Documented retention and renewal for relayer-written memories (6).
6. A Python client-side-encryption flow (like the TypeScript manual flow) so privacy-sensitive apps can keep plaintext away from the relayer and still use semantic recall over embeddings they compute locally (e.g. with a local embedding model on Ollama).
