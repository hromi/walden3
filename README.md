# Walden3

[![ci](https://github.com/hromi/walden3/actions/workflows/ci.yml/badge.svg)](https://github.com/hromi/walden3/actions/workflows/ci.yml)

**Walden3 is a Matrix chatbot that remembers people, and whose memory lives on Walrus.** It takes part in group rooms, learns facts from what people say (each with who said it, when, and in which room), keeps an episode summary of every conversation, and mirrors each room's memory to **Walrus Memory on Sui mainnet**. That memory can be verified, carried into another room, or turned into a permanent, consent-based archive of a whole conversation.

It runs on a **local open-weight model** (IBM Granite 4.0 H-Small via Ollama), so no conversation is sent to a model provider.

> Walden is derived from *Frank*, a sustainable AI agent (frank.udk.ai); the name plays on *Walrus* and on Thoreau's *Walden* and Skinner's *Walden Two*. It was built at the Universität der Künste Berlin for teaching and research, and for the book project *Walden 3* on education in times of AI.

| | |
|---|---|
| Channel | Matrix (any client, e.g. Element): public demo room [`#walden3-demo:udk.ai`](https://matrix.to/#/#walden3-demo:udk.ai), or invite `@walden:udk.ai` (display name **walden3**) into your own room |
| LLM / runtime | **IBM Granite 4.0 H-Small** (`granite4:small-h`, 32B hybrid Mamba MoE, 9B active) on **Ollama**, local GPU |
| Walrus Memory agent (account) ID | `0x1979de15948d9fb7994ec9a78e313e7a454818050e2853142e81f13c73282094` (mainnet) |
| Files of archived rooms | plain Walrus blobs on mainnet |
| Whitepaper | [`docs/research/walrus_memory_graphs.pdf`](docs/research/walrus_memory_graphs.pdf) |
| Evidence of real use | [`docs/EVIDENCE.md`](docs/EVIDENCE.md) |
| Walrus Memory friction and feedback | [`docs/WALRUS_MEMORY_FEEDBACK.md`](docs/WALRUS_MEMORY_FEEDBACK.md) |

## What it does, and for whom

Most chatbots forget everything when a conversation ends, and the ones that remember keep that memory inside a vendor's database. Walden3 is for **groups that talk over weeks and months**: a class, a research team, a community. It remembers what each person told it, scoped to the room where they said it, and the memory belongs to the group: it is encrypted, stored on Walrus, verifiable, and portable.

Before and after memory, same model, same question (real output, see [`docs/EVIDENCE.md`](docs/EVIDENCE.md)):

> **DDH:** @walden3 how many daughters do I have and what is my dog called?
> **without memory:** You have two daughters and your dog is called Max. *(invented)*
> **with memory:** You have three daughters and your dog is named Juni.

## How memory works

- **Facts with provenance.** After every exchange Walden extracts durable facts (`add` / `update` / `retract`) about each person, about itself and about the room. Every fact records who said it, when, in which room and which conversation (episode). A person's memory holds only what they said about themselves.
- **Facts stay in their room.** Only facts learned in the current room reach the prompt; this is enforced in code.
- **Long texts get a deep pass**: split into parts, then the model is asked repeatedly which facts are still missing ("gleaning"); duplicates are caught by comparing content words.
- **Episodes**: each conversation is summarised; recent episodes go into the prompt.
- **Walrus Memory on mainnet**:
  - whenever a session ends, Walden **mirrors the room's memory** to Walrus Memory as a checkpoint (only if it changed);
  - `!walden save` does the same on demand, with an overview first;
  - each checkpoint is encrypted by Walden before it leaves the server (the relayer only sees ciphertext and counts), links to the previous one, and records the hash of Walden's value kernel;
  - `!walden verify` downloads a checkpoint from Walrus, decrypts it and checks its hash and the chain;
  - `!walden load` brings a checkpoint (or a whole archive) into another room, keeping who said what and where it came from; reloading from the same room **syncs** (reworded facts updated, forgotten ones removed).
- **Archives** (`!walden archive`): a whole room, opt-in only, as encrypted episodes, participant memories and a manifest in Walrus Memory, plus files as Walrus blobs; see [`docs/ARCHIVE.md`](docs/ARCHIVE.md).

The local store (encrypted files + SQLite) is the working cache that makes answers fast; Walrus Memory is the durable, verifiable copy. See the whitepaper for the full design and [`docs/CHECKPOINTS.md`](docs/CHECKPOINTS.md) for the formats.

## Commands

```text
@walden3 <anything>            talk (mentions mode: only explicit mentions get a reply)
!walden memory / self          what Walden knows about you / itself in this room (with fact ids)
!walden forget <fact id>       remove a fact about you
!walden save                   store this room's memory in Walrus Memory (overview, then "yes")
!walden checkpoints            checkpoints of rooms you are in
!walden episodes [room]        episodes of a room's latest checkpoint (walden/e1, ...)
!walden load <what> [key]      bring memory here: a room name (walden, walden#2), an episode (walden/e3),
                               a room ID or #alias, your own Matrix ID, a blob ID, or an archive + password
!walden verify [blob id]       check a checkpoint and its chain from Walrus
!walden archive <room> <password> [hours]   archive a room (opt-in: "!walden include" in that room)
!walden archive status | retry | cancel
!walden close                  end the current session (it is then saved and mirrored)
!walden help
```

## Setup

You need: Linux with Python 3.11+, a GPU for the model (Granite 4.0 H-Small needs ~20 GB VRAM; `granite4:micro-h` runs on less), a Matrix account for the bot, and for Walrus Memory the `sui` CLI with a little SUI on mainnet. The `walrus` CLI is only needed for archive files and the optional plain-Walrus backend.

### 1. Install

```bash
git clone https://github.com/hromi/walden3 && cd walden3
python -m venv .venv && . .venv/bin/activate
pip install -e '.[all,dev]'
pytest -q                     # 110+ tests, no network needed
```

### 2. Model

```bash
ollama pull granite4:small-h
OLLAMA_HOST=127.0.0.1:11435 ollama serve      # the port config.yaml uses
```

Any OpenAI-compatible endpoint works (`llm.base_url`, `llm.model` in `config.yaml`).

### 3. Secrets

```bash
cp .env.example .env && chmod 600 .env
python -c "import secrets;print(secrets.token_hex(32))"   # once for each key
```

Fill in `WALDEN_IDENTITY_HMAC_KEY`, `WALDEN_MEMORY_ENCRYPTION_KEY` (**back it up**: without it all memory is unreadable), and `WALDEN_MATRIX_PASSWORD`.

### 4. Walrus Memory account (command line)

You can also create one in the Playground at <https://memory.walrus.xyz>. From the command line, with the `sui` CLI on mainnet:

```bash
# an owner address for the account, funded with a little SUI
sui client new-address ed25519 memwalOwner --json > ~/.sui/sui_config/memwal-owner.recovery.json
sui client ptb --split-coins gas "[100000000]" --assign c --transfer-objects "[c]" @<owner address> --gas-budget 10000000

# the package the mainnet relayer actually uses: GET https://relayer.memory.walrus.xyz/config
P=0xe7c16fbea0560e7057e2bf7422feaa4fb313749fc69c9e9092fac7a33b81d7f5
R=0x8bf82c9e09e36b8d1c38298f68b7cb68e7b8762887e7592add9986d5e9cf199f   # its AccountRegistry
sui client call --package $P --module account --function create_account --args $R 0x6 \
    --sender <owner address> --gas-budget 20000000 --json       # -> the MemWalAccount object = MEMWAL_ACCOUNT_ID

# a delegate key for the bot (32-byte Ed25519 seed, hex) and its public key as a byte list
python -c "import nacl.signing as s;k=s.SigningKey.generate();print(bytes(k).hex());print(list(bytes(k.verify_key)))"
sui client call --package $P --module account --function add_delegate_key \
    --args <account id> $R "[<public key bytes>]" walden-bot 0x6 --sender <owner address> --gas-budget 20000000
```

Put the account ID and the delegate seed into `.env` as `MEMWAL_ACCOUNT_ID` and `MEMWAL_PRIVATE_KEY`. Walden only ever holds the delegate key; the owner can revoke it with `account::remove_delegate_key`. (The package/registry IDs in the Walrus Memory docs belonged to an older deployment when this was written; see [`docs/WALRUS_MEMORY_FEEDBACK.md`](docs/WALRUS_MEMORY_FEEDBACK.md).)

### 5. Configure and run

Edit [`config.yaml`](config.yaml): `matrix.homeserver`, `matrix.user_id`, `matrix.device_id` (keep it fixed, it holds the bot's encryption keys), `llm`, and `memory.checkpoint` (Walrus Memory is the default backend). Then:

```bash
set -a; . ./.env; set +a
walden -c config.yaml matrix
```

Invite the bot into a room and write `@walden3 hello`.

### 6. Run it as a service (starts at boot, restarts after crashes)

[`deploy/systemd/`](deploy/systemd) has two systemd **user** units: `walden-ollama.service` (Ollama on port 11435) and `walden.service` (the bot; it waits for the model, reads `.env`, stops with SIGINT so open sessions are saved, and restarts 15 s after a crash). Replace `/path/to/walden3` with your checkout, then:

```bash
cp deploy/systemd/*.service ~/.config/systemd/user/
loginctl enable-linger "$USER"          # start user services at boot, without logging in
systemctl --user daemon-reload
systemctl --user enable --now walden-ollama.service walden.service
systemctl --user restart walden         # after changing code or config
journalctl --user -u walden -f          # or: tail -f logs/walden.log
```

`start-walden.sh` is the older way (tmux); do not run both.

## Repository map

| | |
|---|---|
| `walden/agent.py` | replies (prompt, pronoun handling, repeat filter) and saving facts per exchange |
| `walden/consolidation.py` | fact extraction (sections per person, deep pass with gleaning) |
| `walden/core/facts.py` | facts with provenance, duplicate detection |
| `walden/context.py` | which memory goes into a prompt (room-scoped, relevance-ranked under a budget) |
| `walden/checkpoint.py` | checkpoints on Walrus Memory / Walrus, verify, load and sync |
| `walden/archive.py`, `walden/matrix_archive.py` | room archives |
| `walden/matrix_service.py` | the Matrix bot and its commands |
| `walden/persona.py` | Walden's immutable value kernel |
| `tests/` | 110+ tests with fake Matrix, fake Walrus Memory and fake models |

## Licence

MIT, see [`LICENSE`](LICENSE).
