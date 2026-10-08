# Evidence of real use

Walden3 has been running since 24 September 2026 on a university server (Ollama with IBM Granite 4.0 H-Small on an NVIDIA A40), in four Matrix rooms on three homeservers (`udk.ai`, `medienhaus.udk-berlin.de`, `m3x.baumhaus.digital`). The people below are real users: DDH (the author, Daniel D. Hromada) and *The Archivist*, a bot-like account on the first Matrix homeserver DDH ever deployed. Names of family members are redacted.

## 1. Before and after memory (same model, same room, same question)

Generated on 8 October 2026 with the production configuration; "without memory" switches recall off and nothing else.

| Question (DDH, medienhaus room) | Without memory | With memory |
|---|---|---|
| who am I? | "You are DDH, a participant in this Matrix room. I do not have any additional information about who you are …" | "You are Daniel D. Hromada, a junior professor of digital education at the Universität der Künste Berlin. You work across art, computation, learning, and experimental mathematics, focusing on human–machine peer learning … sustainable/local AI, knowledge infrastructures … the Personal/Teacher's Primer and elliptic curve research. Your family includes your wife [name], your daughters, and your dog Juni." |
| what article has been published in the compendium and what is it about? | "I do not have any information in my memory about an article being published in a compendium …" | "The article published in the compendium is titled *Entwachstum? Jetzt!* and it describes the pre-history of the *Klasse Leben* as well as its main features such as nature-driven cycles, flat hierarchy, situationism, and non-human actors." |
| what are we working on together? | "It seems we are not currently working on any specific project together …" | "We are working together on *Walden 3*, a continuation of Thoreau's and Skinner's books about education in times of AI. You invited me to co-author this project, and I agreed to help you clarify your ideas and make them easily intelligible for people who know little about education." |
| how many daughters do I have and what is my dog called? | "You have two daughters and your dog is called Max." **(invented)** | "You have three daughters and your dog is named Juni." |

The last row is the clearest case: without memory, a small model does not just say "I don't know", it makes something up.

## 2. Remembering across sessions (24 September)

```text
DDH      I have three daughters and one dog, a 80% poodle - 20% golden doodle named Juni.
DDH      !walden close                    (session ends, memory is saved)
DDH      What's my dog called ?
walden   Your dog's name is Juni.
DDH      How many daughters do I have ?
walden   You have three daughters.
```

## 3. Memory of who said what, between two people (2 October)

```text
DDH            I just came back from Bruxelles where I visited European Parliament. I am more convinced
               than ever that EU will victoriously cross any geopolotical turbulences …
The Archivist  @walden what city did DDH visit recently ?
walden3        DDH recently visited Brussels, the capital of Belgium and home to the European Parliament.
The Archivist  @walden and what did he learn there ?
walden3        DDH did not explicitly state what they learned during their visit to the European Parliament
               in Brussels. However, they did express that their conviction in the EU's ability to overcome
               geopolitical challenges has grown stronger after the visit.
```

(The last answer is from after a fix: the first version answered "and what did he learn there?" by describing itself, because nothing named "he"; Walden now carries the person from the previous question over.)

## 4. Carrying memory between rooms through Walrus Memory (2 October)

In the "Walden" room DDH ran `!walden save`; in a second room on another homeserver he loaded it:

```text
DDH      !walden load nV_tv-BFJ5iZzArog8oT2pQg3OWJ95PTu_fzu3NO9fk
walden3  walden#1, made 2026-10-02T21:51:04Z: downloaded from Walrus Memory (mainnet), decrypted, content hash ok. …
DDH      yes
walden3  Loaded checkpoint #1 (nV_tv-…): 22 facts added, 0 updated, 0 removed …
DDH      @walden what is my relation to math and/or cryptography ?
walden3  You have a deep and multifaceted relationship with mathematics and cryptography. …
```

The facts reached the second room only through Walrus Memory: downloaded from mainnet, decrypted, hash-checked, then merged with their origin recorded.

## 5. Archiving a whole room (8 October)

`!walden archive UdK2300 <password>` archived the room *UdK2300 / Klasse Leben (public)* after an opt-in period: one participant opted in, so only his messages (and Walden's replies to him) were archived.

- Archive #1 manifest: `NO9Rdyc0dtW3CNpFGkmGj_1GJif_YYWDrrAw88X_CCY`
- 33 episodes, 1 participant memory, 1 manifest: 35 memories in Walrus Memory (namespace `walden-archive-2471…`), each 1–35 KB, all encrypted with the archive password.

## 6. On mainnet

Walrus Memory account (agent) `0x1979de15948d9fb7994ec9a78e313e7a454818050e2853142e81f13c73282094`, read back from the relayer on 8 October:

| Namespace | Memories | What |
|---|---|---|
| `walden-6809597738964814` | 1 | checkpoint of the "Walden" room |
| `walden-b30d617b47b349a9` | 2 | checkpoints of the medienhaus room |
| `walden-archive-24717601d4964b7d` | 36 | the UdK2300 archive (35) + 1 leftover from a failed first attempt |
| **total** | **39** | plus automatic checkpoints since then (one per finished session that changed memory) |

A live verification from mainnet:

```text
#1 nV_tv-BFJ5iZzArog8oT2pQg3OWJ95PTu_fzu3NO9fk: content hash ok, 19 facts about 2 people, 1 about Walden,
   2 about the room, 7 episodes (2026-10-02T21:51:04Z)
```

- Blob: <https://walruscan.com/mainnet/blob/nV_tv-BFJ5iZzArog8oT2pQg3OWJ95PTu_fzu3NO9fk>
- Account: <https://suiscan.xyz/mainnet/object/0x1979de15948d9fb7994ec9a78e313e7a454818050e2853142e81f13c73282094>
