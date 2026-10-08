from __future__ import annotations
import asyncio, logging, re, time
from urllib.parse import unquote
from .config import Config
from .core.models import MatrixEventRecord
from .agent import WaldenAgent, THIRD_PERSON
from .matrix_archive import ArchiveCommands
from .session import SessionManager
from .core.facts import apply as apply_facts, facts_of, in_room, render
from .checkpoint import Checkpointer, FROST_PER_WAL, plan_import, apply_import, slice_payload
from typing import Any

log=logging.getLogger(__name__)

class MatrixService(ArchiveCommands):
    def __init__(self,cfg:Config,agent:WaldenAgent):
        self.cfg=cfg; self.agent=agent; self.client=None; self._saves=set()
        self.checkpointer=Checkpointer(cfg,agent.graph) if cfg.memory.checkpoint.enabled else None
        self.pending_deploys={}  # room_id -> checkpoint waiting for "yes"
        self.last_about={}  # room_id -> people the previous question to Walden was about
        self._archive_init()
        s=cfg.matrix.session
        self.sessions=SessionManager(s.idle_seconds,s.max_events,s.max_chars,self._session_closed,s.auto_commit)
    def _allowed(self,room_id,sender):
        c=self.cfg.matrix
        return (not c.allowed_rooms or room_id in c.allowed_rooms) and (not c.allowed_senders or sender in c.allowed_senders)
    def _should_reply(self,room,event,body):
        mode=self.cfg.matrix.reply_mode; prefix=self.cfg.matrix.command_prefix
        if mode=="all": return True
        if mode=="commands": return body.strip().startswith(prefix)
        return self._mentions_me(room,event,body)
    def _mentions_me(self,room,event,body):
        """Explicit mentions only: a Matrix mention/pill, the full user ID, or "@" + username or
        display name. The bare word "walden" in a sentence does not count."""
        me=self.cfg.matrix.user_id
        content=(getattr(event,"source",None) or {}).get("content",{})
        if me in ((content.get("m.mentions") or {}).get("user_ids") or []): return True
        links=[unquote(u) for u in re.findall(r"matrix\.to/#/([^\"'?\s<>/]+)",content.get("formatted_body") or "")]
        if me in links or me in body: return True
        handles={me[1:].split(":")[0],self._display_name(room,me)}
        return any(h and re.search(rf"@{re.escape(h)}(?!\w)",body,re.IGNORECASE) for h in handles)
    async def _send(self,room_id,text):
        content={"msgtype":"m.notice" if self.cfg.matrix.send_as_notice else "m.text","body":text}
        # A failed send (e.g. OlmUnverifiedDeviceError in an E2EE room) must not escape the
        # nio event callback, otherwise it propagates into sync_forever and kills the bot.
        try:
            r=await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=self.cfg.matrix.ignore_unverified_devices,
            )
        except Exception as e:
            log.error("Matrix send to %s failed: %s: %s",room_id,type(e).__name__,e)
            return None
        log.debug("Matrix send: %r",r)
        return getattr(r,"event_id",None)
    @staticmethod
    def _display_name(room,user_id):
        try: return room.user_name(user_id) or ""
        except Exception: return ""
    def _mentioned(self,room,event,body):
        """Matrix IDs the message refers to: Matrix mention metadata, matrix.to pills, and room
        members whose display name or username appears as a whole word. Excludes Walden and the sender."""
        content=(getattr(event,"source",None) or {}).get("content",{})
        found=list((content.get("m.mentions") or {}).get("user_ids") or [])
        links=[unquote(u) for u in re.findall(r"matrix\.to/#/([^\"'?\s<>/]+)",content.get("formatted_body") or "")]
        found+=[u for u in links if u.startswith("@")]
        for uid in getattr(room,"users",None) or {}:
            for word in {self._display_name(room,uid),uid[1:].split(":")[0]}:
                if len(word)>=2 and re.search(rf"(?<!\w){re.escape(word)}(?!\w)",body,re.IGNORECASE):
                    found.append(uid); break
        out=[]
        for uid in found:
            if uid not in out and uid not in (self.cfg.matrix.user_id,event.sender): out.append(uid)
        return out
    async def _command(self,room,event,body):
        p=self.cfg.matrix.command_prefix; rest=body[len(p):].strip()
        if rest in {"help",""}:
            await self._send(room.room_id,f"{p} help | close | memory | self | forget <fact id> | save | checkpoints | episodes [room] | load <what> [key] | verify [blob id] | "
                                       f"archive <room> <password> [hours] | archive status | archive cancel <room> | include | exclude | lineage | health")
            return True
        if rest=="close":
            episode=await self.sessions.close(room.room_id,"command",force_commit=True)
            if episode:
                await self._send(room.room_id,f"Session closed and committed: {episode}")
            else:
                await self._send(room.room_id,"Session closed; no durable episode was written.")
            return True
        if rest in ("memory","self"):
            # Save what was said since the last save first, so the listing includes the latest messages.
            session=self.sessions.sessions.get(room.room_id)
            if session: await self.agent.save_facts(session)
            g=self.agent.graph; room_ref=await g.ensure_room(room.room_id)
            ref=await (g.ensure_person(event.sender) if rest=="memory" else self.agent.self_ref())
            facts=in_room(facts_of((await g.resolve(ref)).get("memory")),room_ref)
            title="What I know about you in this room" if rest=="memory" else "What I know about myself in this room"
            await self._send(room.room_id,f"{title} (remove one with {p} forget <id>):\n"+render(facts,with_ids=True)); return True
        if rest.startswith("archive"):
            reply=await self._archive_command(room,event,rest[len("archive"):].split())
            if reply: await self._send(room.room_id,reply)
            return True
        if rest in ("include","include me","exclude","exclude me"):
            await self._send(room.room_id,await self._include_command(room,event,include=rest.startswith("include"))); return True
        if rest=="checkpoints":
            await self._send(room.room_id,await self._checkpoints_list(room,event)); return True
        if rest.startswith("episodes"):
            await self._send(room.room_id,await self._episodes_list(room,event,rest[len("episodes"):].strip())); return True
        if rest.startswith("load"):
            await self._send(room.room_id,await self._load_prepare(room,event,rest[len("load"):].split())); return True
        if rest in ("save","deploy"):  # "deploy" is the old name
            await self._send(room.room_id,await self._deploy_prepare(room,event)); return True
        if rest.startswith("verify"):
            await self._send(room.room_id,await self._verify(room,rest[len("verify"):].strip())); return True
        if rest.startswith("forget"):
            await self._send(room.room_id,await self._forget(room,event,rest[len("forget"):].strip())); return True
        if rest=="lineage":
            ref=await self.agent.graph.ensure_person(event.sender); line=await self.agent.graph.lineage(ref,10); await self._send(room.room_id,"\n".join(f"v{x['revision'].get('version')} {x['blob_id']}" for x in line)); return True
        if rest=="health":
            await self._send(room.room_id,"Walden is running."); return True
        return False
    async def _forget(self,room,event,fact_id):
        """Remove a fact about yourself, or one you told Walden about itself or the room. Only facts from this room."""
        if not fact_id: return f"Usage: {self.cfg.matrix.command_prefix} forget <fact id>"
        g=self.agent.graph; room_ref=await g.ensure_room(room.room_id); me=await g.ensure_person(event.sender)
        for ref,own in ((me,True),(await self.agent.self_ref(),False),(room_ref,False)):
            mem=(await g.resolve(ref)).get("memory") or {}
            fact=next((f for f in facts_of(mem) if f["id"]==fact_id and f.get("room")==room_ref),None)
            if fact is None: continue
            if not own and fact.get("said_by")!=me: return f"{fact_id} was not told by you, so you cannot remove it."
            new=apply_facts(mem,[{"op":"retract","id":fact_id}],room=room_ref,episode=None,at="",taken=set())
            await g.set_memory(ref,new,{"source":"matrix-command","reason":"forget","room":room_ref,"fact":fact})
            await self._note_memory_change(room.room_id,f"Walden forgot a fact: \"{fact['fact']}\". Do not use it any more.")
            return f"Forgotten: {fact['fact']}"
        return f"No fact {fact_id} about you in this room."
    # ---- on-chain checkpoints ---------------------------------------------------------------
    async def _session_closed(self,session,reason="session-end"):
        """Save the session's episode, then mirror the room's memory to Walrus Memory (in the background)."""
        episode=await self.agent.commit_session(session,reason)
        c=self.cfg.memory.checkpoint
        if self.checkpointer is not None and self.checkpointer.memwal and c.auto_save and c.memwal_key and c.memwal_account_id:
            task=asyncio.create_task(self._auto_save(session.room_id)); self._saves.add(task)
            task.add_done_callback(self._save_done)
        return episode
    async def _auto_save(self,room_id):
        """A checkpoint of the room's memory, unless it is the same as the room's latest one."""
        cp=self.checkpointer; g=self.agent.graph; room_ref=await g.ensure_room(room_id)
        room=(getattr(self.client,"rooms",None) or {}).get(room_id)
        people=await self._checkpoint_people(room,room_ref) if room is not None else {}
        bundle=await cp.build(room_ref,people,await self.agent.self_ref())
        latest=cp.index.latest(room_ref,cp.network)
        if latest and latest["content_sha256"]==bundle.header["content_sha256"]: return
        await cp.dry_run(bundle)                     # size check
        res=await cp.store(bundle)
        h=bundle.header
        cp.index.add(room_ref=room_ref,seq=h["seq"],blob_id=res["blob_id"],object_id=res.get("object_id") or "",
                     content_sha256=h["content_sha256"],network=cp.network,created_at=h["created_at"],namespace=res.get("namespace",""))
        log.info("Auto-saved %s as checkpoint #%s: %s",room_id,h["seq"],res["blob_id"])
    def _may_deploy(self,room,user)->bool:
        users=self.cfg.memory.checkpoint.deploy_users
        if users: return user in users
        try: return room.power_levels.get_user_level(user)>=50
        except Exception: return False
    async def _checkpoint_people(self,room,room_ref)->dict[str,str]:
        """Person ref -> display name for current members and everyone in this room's episodes."""
        g=self.agent.graph; people={}
        for uid in getattr(room,"users",None) or {}:
            if uid!=self.cfg.matrix.user_id: people[await g.ensure_person(uid)]=self._display_name(room,uid) or "member"
        for ep in await g.recent_episodes(room_ref,10_000):
            for w in ep.get("who") or []:
                if w.get("role")!="self": people.setdefault(w["person"],"former member")
        return people
    async def _deploy_prepare(self,room,event)->str:
        cp=self.checkpointer; c=self.cfg.memory.checkpoint
        if cp is None: return "On-chain checkpoints are disabled (memory.checkpoint.enabled)."
        if not self._may_deploy(room,event.sender): return "Only room moderators can save Walden's memory to Walrus."
        session=self.sessions.sessions.get(room.room_id)
        if session: await self.agent.save_facts(session)  # include the conversation so far
        g=self.agent.graph; room_ref=await g.ensure_room(room.room_id)
        bundle=await cp.build(room_ref,await self._checkpoint_people(room,room_ref),await self.agent.self_ref())
        try: dry=await cp.dry_run(bundle)
        except Exception as e: return f"Could not price the checkpoint on {cp.where}: {e}"
        try: bal=await cp.balance()
        except Exception: bal=None
        days=await cp.epoch_days()
        h=bundle.header; n=h["counts"]
        prev=f"links to checkpoint #{h['seq']-1} ({h['previous']})" if h["previous"] else "first checkpoint of this room"
        contents=(f"- Contents: {n['person_facts']} facts about {n['people']} people, {n['walden_facts']} about Walden, "
                  f"{n['room_facts']} about the room, {n['episode_summaries'] if 'episode_summaries' in n else n['episodes']} episode summaries.")
        if cp.memwal:
            self.pending_deploys[room.room_id]={"kind":"save","sender":event.sender,"bundle":bundle,"dry":dry,"expires":time.monotonic()+c.confirm_timeout_seconds}
            return "\n".join([
                f"Walden checkpoint #{h['seq']} for this room, ready for {cp.where}:",
                contents+" Walden encrypts it before it leaves this server; Walrus Memory then Seal-encrypts it again, "
                "so the relayer only ever sees ciphertext and counts.",
                f"- Size: {dry['size']/1024:.1f} KiB (Walrus Memory takes up to 64 KiB per memory)",
                f"- Stored as one memory of Walden's agent account {c.memwal_account_id or '(not configured)'}, in namespace {dry['namespace']}",
                "- Cost: storage is paid by the Walrus Memory relayer",
                f"- Chain: {prev}",
                f"Reply \"yes\" within {c.confirm_timeout_seconds//60} minutes to store it, or \"no\" to cancel.",
            ])
        wal=dry["storage_frost"]/FROST_PER_WAL
        if bal is None: wallet="unknown (could not query the wallet)"
        else:
            enough=bal.get("WAL",0)>=wal and bal.get("SUI",0)>0
            wallet=f"{bal.get('SUI',0):.4f} SUI, {bal.get('WAL',0):.4f} WAL" + ("" if enough else " (NOT enough: needs WAL for storage and SUI for gas)")
        self.pending_deploys[room.room_id]={"kind":"save","sender":event.sender,"bundle":bundle,"dry":dry,"expires":time.monotonic()+c.confirm_timeout_seconds}
        return "\n".join([
            f"Walden checkpoint #{h['seq']} for this room, ready for {cp.where}:",
            contents+" Encrypted; the public header shows only counts and hashes.",
            f"- Size: {dry['size']/1024:.1f} KiB ({dry['encoded_size']/2**20:.1f} MiB after Walrus erasure coding across storage nodes)",
            f"- Kept for: {c.epochs} epochs" + (f" (about {c.epochs*days:g} days)" if days else ""),
            f"- Cost: {wal:.6f} WAL for storage, plus a small write fee and Sui gas for the transactions",
            f"- Wallet: {wallet}",
            f"- Blob ID will be: {dry['blob_id']}",
            f"- Chain: {prev}",
            f"Reply \"yes\" within {c.confirm_timeout_seconds//60} minutes to store it, or \"no\" to cancel.",
        ])
    async def _deploy_confirm(self,room,event,pending):
        cp=self.checkpointer; c=self.cfg.memory.checkpoint; bundle=pending["bundle"]; h=bundle.header
        await self._send(room.room_id,f"Storing checkpoint #{h['seq']} on {cp.where}...")
        try: res=await cp.store(bundle)
        except Exception as e:
            log.exception("checkpoint store failed")
            return await self._send(room.room_id,f"Storing failed, nothing was saved: {e}")
        notes=[]
        if pending["dry"].get("blob_id") and res["blob_id"]!=pending["dry"]["blob_id"]: notes.append("Note: the blob ID differs from the preview.")
        if c.set_attributes and res.get("object_id") and not cp.memwal:
            try: await cp.set_attributes(res["object_id"],h)
            except Exception as e: notes.append(f"Note: tagging the Sui object failed ({e}); the blob itself is stored.")
        cp.index.add(room_ref=h["room"],seq=h["seq"],blob_id=res["blob_id"],object_id=res.get("object_id") or "",
                     content_sha256=h["content_sha256"],network=cp.network,created_at=h["created_at"],namespace=res.get("namespace",""))
        cost=("paid by the Walrus Memory relayer" if cp.memwal else
              f"{res['cost_frost']/FROST_PER_WAL:.6f} WAL" if isinstance(res.get("cost_frost"),(int,float)) else "see the explorer")
        lines=[f"Checkpoint #{h['seq']} is stored on {cp.where}.",
               f"- Blob ID: {res['blob_id']}",
               (f"- Walrus Memory agent (account): {c.memwal_account_id}, namespace {res.get('namespace')}" if cp.memwal else
                f"- Sui object: {res.get('object_id') or 'n/a'}" + (f", stored until epoch {res['end_epoch']}" if res.get("end_epoch") else "")),
               f"- Cost: {cost}",
               f"- Content hash: {h['content_sha256']}",
               *cp.links(res["blob_id"],res.get("object_id")),
               f"Check it any time with {self.cfg.matrix.command_prefix} verify",*notes]
        await self._send(room.room_id,"\n".join(lines))
    # ---- naming rooms and checkpoints -----------------------------------------------------------
    @staticmethod
    def _slug(name:str)->str:
        return re.sub(r"[^a-z0-9]+","-",(name or "").casefold()).strip("-")
    async def _rooms(self)->dict[str,dict[str,Any]]:
        """Rooms Walden is in: room ref -> {"id", "name", "label", "members"}. Labels are short room names
        ("walden"), made unique with a piece of the room ID when two rooms share a name."""
        out={}; seen={}
        for rid,r in (getattr(self.client,"rooms",None) or {}).items():
            name=getattr(r,"display_name",None) or rid
            label=self._slug(name) or self._slug(rid)
            if label in seen: label=f"{label}-{self._slug(rid.split(':')[0])[:6]}"
            seen[label]=rid
            out[await self.agent.graph.ensure_room(rid)]={"id":rid,"name":name,"label":label,"members":set(getattr(r,"users",{}) or {})}
        return out
    async def _source_room(self,user,rooms,target:str):
        """Find the room meant by "!id:server", "#alias:server" or a label; the user must be a member."""
        rid=None
        if target.startswith("#") and ":" in target:
            r=await self.client.room_resolve_alias(target); rid=getattr(r,"room_id",None)
            if not rid: return None,f"Unknown room alias {target}."
        for ref,info in rooms.items():
            if (rid and info["id"]==rid) or info["id"]==target or info["label"]==self._slug(target):
                if user not in info["members"]: return None,"You can only load memory from rooms you are a member of."
                return ref,None
        return None,f"I am not in a room called {target}, so I cannot check that you are a member there. See {self.cfg.matrix.command_prefix} checkpoints."
    async def _checkpoints_list(self,room,event)->str:
        cp=self.checkpointer; c=self.cfg.memory.checkpoint
        if cp is None: return "On-chain checkpoints are disabled (memory.checkpoint.enabled)."
        rooms=await self._rooms(); lines=[]
        for row in cp.index.all(cp.network):
            info=rooms.get(row["room_ref"])
            if not info or event.sender not in info["members"]: continue
            lines.append(f"- {info['label']}#{row['seq']}  ({info['name']}, {row['created_at']}, blob {row['blob_id']})")
        if not lines: return f"No checkpoints on {cp.where} from rooms you are in. Make one with {self.cfg.matrix.command_prefix} save"
        p=self.cfg.matrix.command_prefix
        return "\n".join([f"Checkpoints on {cp.where} from rooms you are in:",*lines,
                          f"Load one with {p} load <name>#<n> (or just <name> for the latest); list its episodes with {p} episodes <name>."])
    async def _read_checkpoint(self,room_ref,seq=None):
        cp=self.checkpointer; c=self.cfg.memory.checkpoint
        row=cp.index.get(room_ref,seq,cp.network) if seq else cp.index.latest(room_ref,cp.network)
        if not row: return None,("That room has no checkpoint "+(f"#{seq} " if seq else "")+f"on {cp.where}. "
                                 f"Run {self.cfg.matrix.command_prefix} save there first.")
        header,payload=cp.open(await cp.read(row["blob_id"]))
        return (row["blob_id"],header,payload),None
    async def _episodes_list(self,room,event,target)->str:
        if self.checkpointer is None: return "On-chain checkpoints are disabled (memory.checkpoint.enabled)."
        rooms=await self._rooms()
        ref,err=await self._source_room(event.sender,rooms,target or room.room_id)
        if err: return err
        try: got,err=await self._read_checkpoint(ref)
        except Exception as e: return f"Could not read the checkpoint: {e}"
        if err: return err
        blob,header,payload=got; label=rooms[ref]["label"]
        lines=[f"Episodes in {label}#{header['seq']} ({header['created_at']}):"]
        for i,ep in enumerate(payload.get("episodes") or [],1):
            end=(ep.get("when") or {}).get("end_ms")
            day=time.strftime("%Y-%m-%d",time.gmtime(end/1000)) if end else "?"
            lines.append(f"- {label}/e{i} ({day}): {(ep.get('summary') or '')[:140]}")
        lines.append(f"Load one episode with {self.cfg.matrix.command_prefix} load {label}/e<n>")
        return "\n".join(lines)
    async def _load_sources(self,room,event,target,key):
        """Resolve a load target into [(blob id, header, payload, partial)], checking that the user may read it."""
        rooms=await self._rooms(); user=event.sender; g=self.agent.graph
        if target.startswith("@"):
            if target!=user: return None,"You can only load what Walden knows about you, not about someone else."
            me=await g.ensure_person(user); out=[]
            for ref,info in rooms.items():
                if user not in info["members"] or info["id"]==room.room_id: continue
                got,_=await self._read_checkpoint(ref)
                if got: out.append((got[0],got[1],slice_payload(got[2],person=me),True))
            return (out,None) if out else (None,"None of the other rooms you share with me has a checkpoint yet.")
        m=re.fullmatch(r"(.+?)(?:#(\d+)|/e(\d+))?",target)
        looks_like_room=target.startswith(("!","#")) or any(i["label"]==self._slug(m.group(1)) for i in rooms.values())
        if looks_like_room and not (target.startswith("#") and ":" in target and m.group(2)):
            name=target if target.startswith(("!","#")) else m.group(1)
            ref,err=await self._source_room(user,rooms,name)
            if err: return None,err
            got,err=await self._read_checkpoint(ref,int(m.group(2)) if m.group(2) and not target.startswith(("!","#")) else None)
            if err: return None,err
            blob,header,payload=got
            if m.group(3) and not target.startswith(("!","#")):
                eps=payload.get("episodes") or []; n=int(m.group(3))
                if not 1<=n<=len(eps): return None,f"That checkpoint has episodes e1 to e{len(eps)}."
                return [(blob,header,slice_payload(payload,episode=eps[n-1].get("ref")),True)],None
            return [(blob,header,payload,False)],None
        # An archive manifest: the password is the permission. Earlier archives of the room come along.
        if self.archives is not None and (key or self.archives.by_manifest(target)):
            if not key: return None,f"That is an archive. Load it with {self.cfg.matrix.command_prefix} load {target} <password>."
            chain=[]; blob=target
            try:
                while blob:
                    hdr,payload=await self.archiver.load_payload(blob,key)
                    chain.append((blob,hdr,payload,True)); blob=hdr.get("previous_manifest")
            except ValueError as e:
                if chain: pass                      # an older archive with another password: load what we have
                elif "not a Walden archive" in str(e) or "no archive" in str(e): chain=[]
                else: return None,f"Could not load the archive: {e}"
            if chain: return list(reversed(chain)),None
        # A blob ID. With Walden's own key, the checkpoint's room must be one the user is in.
        header,payload=self.checkpointer.open(await self.checkpointer.read(target),key)
        if not key:
            info=rooms.get(header.get("room"))
            if info is None: return None,"I am no longer in the room that checkpoint comes from, so I cannot check that you were a member there."
            if user not in info["members"]: return None,"You can only load memory from rooms you are a member of."
        return [(target,header,payload,False)],None
    async def _load_prepare(self,room,event,args)->str:
        """!walden load <blob id | room | name[#n] | name/e<n> | @you> [key]: read checkpoint(s) from Walrus and show what they would change here."""
        cp=self.checkpointer; c=self.cfg.memory.checkpoint; p=self.cfg.matrix.command_prefix
        if cp is None: return "On-chain checkpoints are disabled (memory.checkpoint.enabled)."
        if not args: return (f"Usage: {p} load <what> [key], where <what> is a room name from {p} checkpoints (walden, walden#2), "
                             f"an episode (walden/e3), a room ID or #alias, your own Matrix ID, or a Walrus blob ID. "
                             "The key is only needed for a checkpoint made by another Walden.")
        if not self._may_deploy(room,event.sender): return "Only room moderators can load memory into this room."
        target=args[0]; key=args[1] if len(args)>1 else None; note=""
        if key:
            # The key is now in the room's history. Remove the message if Walden is allowed to.
            try:
                r=await self.client.room_redact(room.room_id,event.event_id,reason="contained an encryption key")
                note="\nI deleted your message, because it contained an encryption key." if getattr(r,"event_id",None) else ""
            except Exception: pass
            note=note or "\nWarning: your message contains an encryption key and I could not delete it; everyone in this room can read it."
        try: sources,err=await self._load_sources(room,event,target,key)
        except Exception as e: return f"Could not load {target} from {cp.where}: {e}{note}"
        if err: return err+note
        g=self.agent.graph; room_ref=await g.ensure_room(room.room_id)
        members=await self._checkpoint_people(room,room_ref); me=await self.agent.self_ref()
        plans=[await plan_import(cp,blob,header,payload,target_room=room_ref,members=members,self_ref=me,partial=partial)
               for blob,header,payload,partial in sources]
        changes={}; labels={}
        for plan in plans:
            labels.update(plan.labels)
            for r,v in plan.facts.items(): changes.setdefault(r,[0,0,0])[0]+=len(v)
            for r,v in (plan.updates or {}).items(): changes.setdefault(r,[0,0,0])[1]+=len(v)
            for r,v in (plan.removals or {}).items(): changes.setdefault(r,[0,0,0])[2]+=len(v)
        episodes=sum(len(pl.episodes) for pl in plans)
        if not changes and not episodes: return f"{target} has nothing new for this room."+note
        self.pending_deploys[room.room_id]={"kind":"load","sender":event.sender,"plans":plans,"room_ref":room_ref,
                                            "expires":time.monotonic()+c.confirm_timeout_seconds}
        rooms=await self._rooms(); lines=[]
        for (blob,header,payload,partial),plan in zip(sources,plans):
            src=(f"archive of {header.get('room_name') or 'a room'}" if header.get("archive") else
                 rooms.get(header.get("room"),{}).get("label") or ("this room" if header.get("room")==room_ref else "another room"))
            what=" (part)" if partial and not header.get("archive") else ""
            lines.append(f"{src}#{header.get('seq')}{what}, made {header.get('created_at')}: downloaded from {cp.where}, decrypted, content hash ok.")
            if plan.older:
                lines.append(f"  Facts from that room were already loaded from a newer checkpoint (#{plan.synced_from}): this older one only adds what is missing.")
            elif plan.synced_from is not None:
                lines.append(f"  Facts from that room were loaded before (#{plan.synced_from}): this load syncs them"
                             +(", updating reworded facts." if partial else ", updating reworded facts and removing those no longer there."))
        lines.append("Loading would change this room's memory:")
        for r,(a,u,d) in changes.items():
            parts=[x for x in (f"{a} new" if a else "",f"{u} updated" if u else "",f"{d} removed" if d else "") if x]
            lines.append(f"- {labels.get(r,'someone')}: {', '.join(parts)} facts")
        lines.append(f"- {episodes} past episodes")
        dup=sum(pl.duplicates for pl in plans); skipped=sorted({n for pl in plans for n in pl.skipped_people})
        if dup: lines.append(f"Already known here, skipped: {dup} facts.")
        if skipped: lines.append(f"Not matched to anyone here, skipped: facts about {', '.join(skipped)}.")
        lines.append("Everyone in this room will be able to see these facts. "
                     f"Reply \"yes\" within {c.confirm_timeout_seconds//60} minutes to load them, or \"no\" to cancel.")
        return "\n".join(lines)+note
    YES={"yes","y","ok","okay","sure","ja","jo","confirm","confirmed","go","do it"}
    NO={"no","n","nope","nein","cancel","stop","abort"}
    def _confirmation(self,body:str):
        """True/False for a yes/no answer to a pending save or load ("@walden yes please", "Yes!", "nein"), else None."""
        t=re.sub(r"^\s*@?\S*walden\S*[:,]?\s*","",body.strip(),flags=re.IGNORECASE).casefold()
        words=re.findall(r"[a-zäöü]+",t)
        if not words or len(words)>4: return None
        first=" ".join(words[:2]) if " ".join(words[:2]) in self.YES else words[0]
        if first in self.YES: return True
        if first in self.NO: return False
        return None
    async def _note_memory_change(self,room_id,text):
        """Tell the conversation that memory changed, so earlier replies are not taken as current."""
        s=self.sessions.sessions.get(room_id)
        if s is None: return
        s.add(MatrixEventRecord(event_id=f"walden-note-{time.time_ns()}",room_id=room_id,sender="system",
                                timestamp_ms=int(time.time()*1000),body=text,role="system"))
    async def _load_confirm(self,room,pending):
        total={"added":0,"updated":0,"removed":0}; episodes=0; names=[]
        try:
            for plan in pending["plans"]:
                n=await apply_import(self.checkpointer,plan,pending["room_ref"])
                for k in total: total[k]+=n[k]
                episodes+=len(plan.episodes); names.append(f"#{plan.header.get('seq')} ({plan.blob_id})")
        except Exception as e:
            log.exception("checkpoint load failed")
            return await self._send(room.room_id,f"Loading failed: {e}")
        summary=f"{total['added']} facts added, {total['updated']} updated, {total['removed']} removed, {episodes} episodes added"
        await self._note_memory_change(room.room_id,f"Memory was loaded from checkpoint {', '.join(names)}: {summary}. "
            "Earlier answers that said Walden did not know something, or that used removed facts, may be outdated.")
        await self._send(room.room_id,f"Loaded checkpoint {', '.join(names)}: {summary}. "
                                      "Each fact keeps who said it and records where it came from. "
                                      f"See them with {self.cfg.matrix.command_prefix} memory, {self.cfg.matrix.command_prefix} self, or just ask me.")
    async def _verify(self,room,blob_id)->str:
        cp=self.checkpointer; c=self.cfg.memory.checkpoint
        if cp is None: return "On-chain checkpoints are disabled (memory.checkpoint.enabled)."
        room_ref=await self.agent.graph.ensure_room(room.room_id)
        if not blob_id:
            latest=cp.index.latest(room_ref,cp.network)
            if not latest: return f"This room has no checkpoint on {cp.where} yet. Create one with {self.cfg.matrix.command_prefix} save"
            blob_id=latest["blob_id"]
        try: lines=await cp.verify(blob_id,room_ref)
        except Exception as e: return f"Verification FAILED for {blob_id}: {e}"
        return "\n".join([f"Verified from {cp.where}: downloaded, decrypted and hash-checked.",*lines,
                          "The chain is intact." if len(lines)>1 else "This is the first checkpoint of the room."])
    async def on_message(self,room,event):
        # Nothing that goes wrong while handling one message may reach nio's sync loop: an exception
        # there stops the bot (a failed checkpoint confirmation did exactly that).
        try: await self._on_message(room,event)
        except Exception as e:
            log.exception("Handling a message failed")
            try: await self._send(room.room_id,f"Walden ran into an internal error ({type(e).__name__}); the bot keeps running.")
            except Exception: pass
    async def _on_message(self,room,event):
        # nio filters RoomMessageText for us. Ignore our own messages and notices from bots if desired.
        if event.sender==self.cfg.matrix.user_id or not self._allowed(room.room_id,event.sender): return
        body=event.body or ""
        pending=self.pending_deploys.get(room.room_id)
        answer=self._confirmation(body) if pending and event.sender==pending["sender"] else None
        if answer is not None:
            del self.pending_deploys[room.room_id]
            if answer is False:
                await self._send(room.room_id,{"save":"Cancelled. Nothing was stored.","load":"Cancelled. Nothing was loaded.",
                                               "archive":"Cancelled. Nothing was scheduled or stored."}[pending["kind"]])
            elif time.monotonic()>pending["expires"]: await self._send(room.room_id,f"That {pending['kind']} expired. Run {self.cfg.matrix.command_prefix} {pending['kind']} again.")
            elif pending["kind"]=="load": await self._load_confirm(room,pending)
            elif pending["kind"]=="archive": await self._archive_confirm(room,pending)
            else: await self._deploy_confirm(room,event,pending)
            return
        if body.startswith(self.cfg.matrix.command_prefix):
            if await self._command(room,event,body): return
        rec=MatrixEventRecord(
            event_id=event.event_id,
            room_id=room.room_id,
            sender=event.sender,
            timestamp_ms=int(getattr(event,"server_timestamp",0) or time.time()*1000),
            body=body,
            role="human",
            sender_name=self._display_name(room,event.sender),
        )
        should_reply=self._should_reply(room,event,body)
        # Keep a human turn and Walden's answer in the same episode. Limit checks happen
        # after the complete pair is buffered, avoiding a boundary between question and answer.
        await self.sessions.add(rec,check_limits=not should_reply)
        if should_reply:
            try:
                session=self.sessions.sessions.get(room.room_id)
                recent=session.recent(
                    self.cfg.matrix.session.prompt_recent_events,
                    self.cfg.matrix.session.prompt_recent_chars,
                ) if session else [rec]
                mentioned=self._mentioned(room,event,body); carried=False
                if not mentioned and THIRD_PERSON.search(body) and self.last_about.get(room.room_id):
                    # "and what did he learn there?": "he" is whoever the previous question was about.
                    mentioned=[u for u in self.last_about[room.room_id] if u!=event.sender]; carried=bool(mentioned)
                self.last_about[room.room_id]=mentioned
                names={uid:self._display_name(room,uid) for uid in mentioned}
                names[event.sender]=rec.sender_name
                answer=await self.agent.respond(rec,mentioned,recent,{k:v for k,v in names.items() if v},carried=carried)
                event_id=await self._send(room.room_id,answer)
                bot_rec=MatrixEventRecord(
                    event_id=event_id or f"walden-local-{time.time_ns()}",
                    room_id=room.room_id,
                    sender=self.cfg.matrix.user_id or self.cfg.agent.name,
                    timestamp_ms=int(time.time()*1000),
                    body=answer,
                    role="assistant",
                )
                if self.cfg.matrix.session.include_assistant_turns:
                    await self.sessions.add(bot_rec,check_limits=False)
            except Exception as e:
                log.exception("response failed")
                await self._send(room.room_id,f"Walden encountered an internal error: {type(e).__name__}")
            finally:
                try: await self.sessions.check_limits(room.room_id)
                except Exception: log.exception("session limit check failed")
        self._maybe_save(room.room_id,replied=should_reply)
    def _maybe_save(self,room_id,replied):
        """Save facts in the background after each exchange (Walden replied), or once save_every
        messages piled up without a reply. The reply is never delayed by it."""
        n=self.cfg.matrix.session.save_every; s=self.sessions.sessions.get(room_id)
        if n<=0 or s is None or len(s.events)<=s.saved: return
        if replied or len(s.events)-s.saved>=n:
            task=asyncio.create_task(self.agent.save_facts(s)); self._saves.add(task)
            task.add_done_callback(self._save_done)
    def _save_done(self,task):
        self._saves.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Saving facts failed",exc_info=task.exception())
    async def on_invite(self,room,event):
        if self.cfg.matrix.auto_join_invites and event.membership=="invite" and event.state_key==self.cfg.matrix.user_id:
            log.info("Joining invited room %s",room.room_id); await self.client.join(room.room_id)
    async def run(self):
        try:
            from nio import AsyncClient, AsyncClientConfig, RoomMessageText, InviteMemberEvent, LoginResponse, ToDeviceEvent
        except ImportError as e:
            raise RuntimeError("Matrix support requires: pip install 'walden-agent[matrix]'") from e
        c=self.cfg.matrix
        nio_cfg=AsyncClientConfig(encryption_enabled=c.encryption,store_sync_tokens=True)
        self.client=AsyncClient(c.homeserver,c.user_id,device_id=c.device_id or "",store_path=c.store_path,config=nio_cfg)
        if c.access_token:
            self.client.access_token=c.access_token; self.client.user_id=c.user_id
            if c.device_id: self.client.device_id=c.device_id
            # load_store is required for E2EE state when an access token is injected.
            try: self.client.load_store()
            except Exception: log.debug("No existing nio store yet",exc_info=True)
        elif c.password:
            r=await self.client.login(c.password,device_name=self.cfg.agent.name)
            if not isinstance(r,LoginResponse): raise RuntimeError(f"Matrix login failed: {r}")
        else: raise ValueError("matrix.access_token or matrix.password is required")
        if c.display_name:
            try:
                cur=await self.client.get_displayname(c.user_id)
                if getattr(cur,"displayname",None)!=c.display_name:
                    await self.client.set_displayname(c.display_name); log.info("Display name set to %s",c.display_name)
            except Exception: log.warning("Could not set the display name",exc_info=True)
        self.client.add_event_callback(self.on_message,RoomMessageText)
        self.client.add_event_callback(self.on_invite,InviteMemberEvent)
        if c.encryption:
            from .matrix_verification import SasVerifier
            verifier=SasVerifier(self.client,c.verification_users or [c.user_id])
            self.client.add_to_device_callback(verifier.on_to_device,ToDeviceEvent)
        await self.sessions.start()
        if self.archives is not None: self._scheduler=asyncio.create_task(self._archive_scheduler())
        try: await self.client.sync_forever(timeout=c.sync_timeout_ms,full_state=True)
        finally:
            await self.sessions.stop(); await self.client.close()
