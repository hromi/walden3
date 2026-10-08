# Archiving a room

```text
!walden archive <room ID or #alias> <password> [notice hours]   (run from any room where you are a moderator)
!walden archive status
!walden archive cancel <room>
!walden include / !walden exclude                              (in the room being archived, during the notice period)
!walden load <archive ID> <password>                            (in any room: brings the archive in, then you can talk about it)
```

## What happens

1. **Your command is deleted** right away, because it contains the password. The password never leaves the server; an archive key is derived from it with scrypt (salt stored in every archive header).
2. **Walden joins the room** (it must be invited, or the room must be public) and checks that you are a moderator there.
3. **It reads the history** since the last archive of that room (or everything), downloads the files (up to `max_file_mb`), and prices them on Walrus mainnet with a dry run. In encrypted rooms, messages sent before Walden joined cannot be decrypted; the overview says how many.
4. **Overview and "yes"** in the room where you asked: messages, time span, participants, files and their cost, the wallet's balance. These are maximums: only people who opt in will be archived.
5. **Opt-in period** (default 24 hours): Walden introduces itself in the room and explains that whoever replies `!walden include` within that time will have the traces of their presence (their messages, files, and what Walden learns about them) stored in retrievable, encrypted form on Walrus, with the date until which the files are paid for, and that everyone included can get the password on request by direct message to the person who asked for the archive. **Only people who opt in are archived**; everyone else's messages and files are left out, and so are Walden's replies to them (they may repeat what those people said). `!walden exclude` withdraws before the deadline. The job survives restarts; until it runs, the archive key is kept encrypted with Walden's own key, and deleted afterwards. If nobody opts in, nothing is stored.
6. **Archiving**, when the notice period ends:
   - each file becomes an encrypted Walrus blob on mainnet;
   - the conversation is split into **episodes** (by pauses and size) and read with the same extraction as live memory, on a scratch store so nothing reaches Walden's working memory: a summary per episode and the facts about each person;
   - stored in **Walrus Memory** (namespace `walden-archive-<room>`), all encrypted with the archive key: one memory per episode (summary, topics, participants, full transcript, file references), one per participant (their facts, each pointing at its episode), and a **manifest** tying everything together.
7. **Closing message** in the room: the archive ID, its Walrus Memory and Walrus links (manifest, files), how to load it, and who to ask for the password; then "Mesdames, Messieurs, je vous prie d'accepter mes salutations les plus distinguées. Au revoir !", and Walden **leaves**. You get the same ID where you asked.

## Linking

- Every episode links to the previous episode, every participant memory to that person's memory in the previous archive, every manifest to the previous manifest.
- **Re-archiving** a room continues after the last archived message, so the archives of a room form one chain.
- Facts point at the episode they come from; the manifest lists episodes, participants and files.

## Loading and talking

`!walden load <manifest ID> <password>` loads that archive **and all earlier archives of the same room** into the current room: participants' facts (matched to people here by Walden's pseudonymous references, else by display name) and the episodes, with the usual overview and "yes". Afterwards Walden answers questions about the archived room and its people from that memory. Loading never removes anything, because each archive only holds what was new since the previous one.

## Why opt-in

Walrus storage cannot be deleted, so the archive keeps personal data for as long as it is paid for. Under the GDPR that needs a lawful basis for each person; an explicit, informed `!walden include` is consent, while not objecting to a notice is not. Opt-in also lets each person decide for themselves, with the password available to them.
