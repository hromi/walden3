# Submission: Walrus Sessions "Chatbots That Remember"

Deadline: **9 October 2026, 14:00 UTC**. Form: <https://airtable.com/appoDAKpC74UOqoDa/shro5iVzzjoWfZlPK> (and register the project on DeepSurge).

## Form fields

| Field | Answer |
|---|---|
| Project name | **Walden3** |
| Chatbot description | Walden3 is a Matrix chatbot for groups that talk over weeks and months (classes, research groups, communities). It remembers people room by room: it learns facts from what people say, each with who said it, when and where, keeps episode summaries of every conversation, and mirrors each room's memory to Walrus Memory on Sui mainnet as encrypted, hash-chained, verifiable checkpoints that can be loaded into other rooms. Whole rooms can be archived with per-person opt-in. It solves the two problems of chatbot memory in groups: bots that forget (or invent) what people told them, and memory locked in a vendor's database. Runs on a local open-weight model, so no conversation goes to a model provider. |
| Primary contact | Daniel D. Hromada, d@udk.ai |
| GitHub account | hromi |
| Public repository | <https://github.com/hromi/walden3> |
| LLM | **IBM Granite 4.0 H-Small** (`granite4:small-h`, 32B hybrid Mamba-2/transformer MoE, 9B active), open weights, **self-hosted via Ollama** on an NVIDIA A40. Category: *Beyond the Big Two*. |
| Channel / live link | Matrix, public demo room: <https://matrix.to/#/#walden3-demo:udk.ai> (judges' account `@walrus-jury:udk.ai`, moderator there; give the password only in the form, never in the repo). Or invite `@walden:udk.ai` into any room. |
| Walrus Memory agent ID | `0x1979de15948d9fb7994ec9a78e313e7a454818050e2853142e81f13c73282094` |
| Blob count | **39** on 8 Oct 2026, 22:00 UTC (re-check before submitting: see below) |
| Dedicated wallet created for Sessions | `0xbe54a8cf260e068dea6c4ce23e91c430af6cbd10791289e0c2d9e64b359473d5` (owner of the Walrus Memory account, created for this hackathon) |
| Article | Medium link *(after publishing `docs/ARTICLE.md` / `docs/ARTICLE.html`)* |
| Whitepaper | `docs/research/walrus_memory_graphs.pdf` in the repository |
| Integration friction (Beyond the Big Two) | `docs/WALRUS_MEMORY_FEEDBACK.md` |

Re-check the blob count just before submitting (automatic checkpoints keep adding to it):

```bash
cd walden-0.2.0 && set -a && . ./.env && set +a && .venv/bin/python -c "
import asyncio
from walden.config import load_config
from walden.checkpoint import Checkpointer
async def m():
    mw=Checkpointer(load_config('config.yaml'),None)._memwal_client(); ns=await mw.list_namespaces()
    print(sum(n.memory_count for n in ns.namespaces))
asyncio.run(m())"
```

## Walrus Memory feedback form

- **Bug / friction:** the mainnet package and registry IDs in the docs belong to an older deployment; an account created there gets 401 from the hosted relayer, whose `/config` shows a different package (`0xe7c16fbe…`), and whose registry we had to find via GraphQL. Also: memories of 50–60 KB (under the 64 KiB limit) fail with "durable upload … exceeded step budget".
- **Improvement idea:** `get(blob_id)` and `list(namespace)` in the SDK; publish `registryId` at the relayer's `/config`; Python account and delegate-key helpers; documented retention for relayer-written memories.
- **GitHub issues:** file issues 1–6 from `docs/WALRUS_MEMORY_FEEDBACK.md` at <https://github.com/MystenLabs/MemWal/issues> (each has steps, expected, actual, environment). They also count for the Bug Bounty.

## Post on X (under the session announcement, tag and hashtag required)

> My chatbot invented a dog called Max. Then I gave it a memory on @WalrusProtocol.
>
> Walden3: a Matrix bot on a local open model (IBM Granite 4.0 via Ollama) that remembers people room by room and keeps every room's memory on Walrus Memory: encrypted, hash-chained, verifiable, loadable into other rooms.
>
> Build log: <article link> #WalrusMemory

## Promo post (third-party community, e.g. r/LocalLLaMA)

Title: *A local Granite 4.0 chatbot that remembers people, with its memory on decentralized storage: lessons from making a 9B-active model a reliable memory writer*

> I built Walden3, a Matrix group chatbot that runs IBM Granite 4.0 H-Small locally via Ollama and remembers what people tell it (who said what, when, in which room). The memory is mirrored to Walrus Memory (decentralized storage), encrypted before it leaves the server.
>
> Most of the work was getting a small model to write memory reliably: one section per person with a strict JSON schema (Ollama enforces it), reading long messages in ~800-character parts, asking "which facts are still missing?" until the answer is empty, and catching duplicates and pronoun mix-ups in code. A 3,756-character self-description went from 2 extracted facts to 14.
>
> Write-up with before/after and code: <article link>. Source: https://github.com/hromi/walden3.

## Your checklist

- [x] Publish the GitHub repository: https://github.com/hromi/walden3
- [ ] Publish the article on Medium (paste `docs/ARTICLE.html`, add 1–2 real Element screenshots), put the repo link in it
- [ ] Register on DeepSurge and submit the Airtable form (fields above)
- [ ] Walrus Memory feedback form; GitHub issues at MemWal
- [ ] Join the Walrus Discord
- [ ] Post on X under the session announcement, tagging @WalrusProtocol with #WalrusMemory
- [ ] Promo post in a third-party community (e.g. r/LocalLLaMA)
- [x] Public demo room for the judges: https://matrix.to/#/#walden3-demo:udk.ai
