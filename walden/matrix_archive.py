"""Matrix side of "!walden archive": joining the room, reading its history and files, the notice and
opt-out window, the scheduled run, the closing message, and leaving. See walden/archive.py for what is stored.

Everything slow runs in background tasks: command handlers are called from inside nio's sync loop, so
waiting there (for a join to show up in a sync, or for an hour-long archive) would stall the bot."""
from __future__ import annotations
import asyncio, logging, re, time, uuid
from typing import Any
from .archive import Archiver, ArchiveIndex, Message, derive_key, new_salt
from .checkpoint import FROST_PER_WAL
from .core.crypto import Cipher

log=logging.getLogger(__name__)

# Walden's own command output and archive notices are not part of what a room talked about.
FAREWELL="Mesdames, Messieurs, je vous prie d'accepter mes salutations les plus distinguées. Au revoir !"

WALDEN_NOISE=re.compile(r"^(What I know about|Walden checkpoint #|Checkpoint #|Loaded checkpoint|Storing checkpoint|Walden will archive|Hello, I am Walden|Mesdames, Messieurs|"
                        r"The memory of this room|Usage:|Only room moderators|You are |Walden ran into|Verified from|Checkpoints on|Episodes in|"
                        r"\{|walden#\d|Cancelled|Forgotten:|No fact )")

class ArchiveCommands:
    """Mixed into MatrixService."""
    def _archive_init(self):
        self._archives=None; self._archive_tasks=set(); self._scheduler=None
    @property
    def archives(self)->ArchiveIndex|None:
        """Created on first use, so that merely constructing the service touches no database."""
        if not self.cfg.archive.enabled: return None
        if self._archives is None: self._archives=ArchiveIndex(self.cfg.memory.sui.local_db)
        return self._archives
    @property
    def archiver(self)->Archiver:
        return Archiver(self.cfg,self.agent,self.checkpointer,self.archives)
    def _spawn(self,coro,what:str,room_id:str|None=None):
        task=asyncio.create_task(coro); self._archive_tasks.add(task)
        def done(t):
            self._archive_tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("%s failed",what,exc_info=t.exception())
                if room_id: asyncio.create_task(self._send(room_id,f"{what} failed: {t.exception()}"))
        task.add_done_callback(done)
    def _master(self)->Cipher: return Cipher(self.cfg.memory.encryption_key,True)

    # ---- commands ---------------------------------------------------------------------------------------
    async def _archive_command(self,room,event,args:list[str])->str|None:
        p=self.cfg.matrix.command_prefix
        if self.archives is None or self.checkpointer is None: return "Archiving is disabled (archive.enabled / memory.checkpoint.enabled)."
        if args and args[0]=="status": return self._archive_status(event.sender)
        if args and args[0]=="cancel": return await self._archive_cancel(event.sender,args[1:])
        if args and args[0]=="retry": return await self._archive_retry(event.sender,args[1:],room,event)
        if len(args)<2: return (f"Usage: {p} archive <room name, ID or #alias> <password> [notice hours]. The password encrypts the archive; "
                                f"you need it to load the archive later. Also: {p} archive status, {p} archive cancel <room>.")
        if self.cfg.archive.require_moderator and not self._may_deploy(room,event.sender): return "Only room moderators can archive rooms."
        target,password=args[0],args[1]
        try: hours=float(args[2]) if len(args)>2 else self.cfg.archive.notice_hours
        except ValueError: return "The notice time must be a number of hours."
        # The message contains the password: remove it from the room right away.
        note=""
        try:
            r=await self.client.room_redact(room.room_id,event.event_id,reason="contained an archive password")
            note="I deleted your message, because it contained the password. " if getattr(r,"event_id",None) else ""
        except Exception: pass
        if not note:
            others=[u for u in (getattr(room,"users",None) or {}) if u not in (event.sender,self.cfg.matrix.user_id)]
            note=("(Only you and I can see your message, which contains the password.) " if not others else
                  "Warning: I could not delete your message, so everyone in this room can read the password. Consider a different one. ")
        salt=new_salt(); key=derive_key(password,salt); del password
        self._spawn(self._archive_prepare(room.room_id,event.sender,target,key,salt,hours),"Preparing the archive",room.room_id)
        return note+f"Joining {target} and reading its history; I will report here with an overview before anything is stored."

    def _archive_status(self,user)->str:
        rows=[j for j in self.archives.jobs() if j["state"] in ("scheduled","running","failed") and j["requester"]==user and
              (j["state"]!="failed" or j["key_enc"])]
        if not rows: return "You have no archive waiting, running or to retry."
        p=self.cfg.matrix.command_prefix
        return "\n".join(f"- {j['room_name'] or j['room_id']}: {j['state']}"+
                         (f", starts {time.strftime('%Y-%m-%d %H:%M UTC',time.gmtime(j['deadline']))}" if j["state"]=="scheduled" else "")+
                         f", {len(j['included'])} people included"+
                         (f": {j['error']} ({p} archive retry, or {p} archive cancel {j['room_name'] or j['room_id']})" if j["state"]=="failed" else "")
                         for j in rows)

    @staticmethod
    def _job_matches(job,arg:str|None)->bool:
        """A job named by room ID, its full name, or a part of it ("UdK2300" for "UdK2300 / Klasse Leben public")."""
        if not arg: return True
        norm=lambda x:re.sub(r"[^a-z0-9]+","",(x or "").casefold())
        return arg==job["room_id"] or (bool(norm(arg)) and norm(arg) in norm(job["room_name"]))

    async def _archive_retry(self,user,args,room=None,event=None)->str:
        """Run a failed archive again, right away: the people who opted in already agreed.
        "archive retry <room> <password>" also revives a job whose key was not kept."""
        password=args[1] if len(args)>1 else None
        rows=[j for j in self.archives.jobs("failed") if j["requester"]==user and self._job_matches(j,args[0] if args else None)]
        note=""
        if password and room is not None and event is not None:
            try:
                r=await self.client.room_redact(room.room_id,event.event_id,reason="contained an archive password")
                note="I deleted your message, because it contained the password. " if getattr(r,"event_id",None) else ""
            except Exception: pass
        if password:
            for j in rows:
                if not j["key_enc"]: j["key_enc"]=self._master().encrypt(derive_key(password,bytes.fromhex(j["salt"])).encode()).decode()
            del password
        ready=[j for j in rows if j["key_enc"]]
        if not ready:
            return note+("No failed archive of yours to retry." if not rows else
                         f"That archive's key was not kept; retry with {self.cfg.matrix.command_prefix} archive retry <room> <password>.")
        for j in ready:
            j.update(state="scheduled",deadline=time.time(),error=None); self.archives.put_job(j)
        return note+f"Retrying within a minute: {', '.join(j['room_name'] or j['room_id'] for j in ready)}."

    async def _archive_cancel(self,user,args)->str:
        rows=[j for j in self.archives.jobs() if j["state"] in ("scheduled","failed") and j["requester"]==user
              and self._job_matches(j,args[0] if args else None)]
        if not rows: return "No scheduled archive of yours to cancel."
        for j in rows:
            j.update(state="cancelled",key_enc=None); self.archives.put_job(j)
            await self._send(j["room_id"],"The archiving of this room has been cancelled. Nothing was stored.")
            await self._leave_if_only_for_archive(j["room_id"])
        return f"Cancelled: {', '.join(j['room_name'] or j['room_id'] for j in rows)}."

    async def _include_command(self,room,event,include:bool)->str:
        """Opt-in: only people who say "!walden include" before the deadline are archived."""
        jobs=[j for j in self.archives.jobs("scheduled",room.room_id)] if self.archives else []
        if not jobs: return "This room is not about to be archived."
        for j in jobs:
            (j["included"].add if include else j["included"].discard)(event.sender); self.archives.put_job(j)
        when=time.strftime('%Y-%m-%d %H:%M UTC',time.gmtime(jobs[0]["deadline"]))
        p=self.cfg.matrix.command_prefix
        return (f"Thank you. Your messages, files and what I learn about you will be part of the archive made on {when}. "
                f"Changed your mind before then? {p} exclude" if include else
                f"Understood: nothing of yours will be in the archive made on {when}.")

    # ---- preparing: join, read, price, ask ------------------------------------------------------------
    async def _join(self,target:str)->str:
        rid=next((r for r in self.client.rooms if r==target),None)
        if rid is None and not target.startswith(("!","#")):
            # A room Walden is already in, by its name ("UdK2300"), case and spacing ignored.
            norm=lambda x:re.sub(r"[^a-z0-9]+","",(x or "").casefold())
            hits=[r for r,room in self.client.rooms.items() if norm(getattr(room,"display_name",None))==norm(target)]
            if len(hits)>1: raise RuntimeError(f"I am in several rooms called {target}; use its room ID instead.")
            if not hits: raise RuntimeError(f"I am not in a room called {target}. Invite me there, or give its room ID or #alias.")
            rid=hits[0]
        if rid is None and target.startswith("#"):
            r=await self.client.room_resolve_alias(target); rid=getattr(r,"room_id",None)
            if rid in self.client.rooms: return rid
        if rid is None or rid not in self.client.rooms:
            r=await self.client.join(target)
            rid=getattr(r,"room_id",None)
            if not rid: raise RuntimeError(f"I could not join {target}: {getattr(r,'message',r)}. Invite me, or make sure the room is public.")
        for _ in range(60):                       # the room shows up with its members after the next sync
            if rid in self.client.rooms and self.client.rooms[rid].users: return rid
            await asyncio.sleep(1)
        raise RuntimeError("I joined, but the room's members did not arrive; try again in a minute.")

    async def _history(self,room_id:str,after_event:str|None,only:set[str]|None,with_files:bool=True)->tuple[list[Message],dict[str,int]]:
        """Messages after `after_event` (all if None), oldest first, with file contents downloaded.
        only: archive just these people's messages, and Walden's replies to them (None = everyone, for the overview).
        Walden's other replies are left out: they may repeat what people who are not included said."""
        from nio import (MegolmEvent,RoomMessageText,RoomMessageNotice,RoomMessageEmote,RoomMessageMedia,RoomEncryptedMedia,MessageDirection)
        from nio.crypto.attachments import decrypt_attachment
        room=self.client.rooms[room_id]; me=self.cfg.matrix.user_id
        token=self.client.next_batch; events=[]; stats={"undecryptable":0,"skipped_files":0,"excluded":0}
        while token:
            r=await self._messages_page(room_id,token,MessageDirection.back)
            if not getattr(r,"chunk",None): break
            stop=False
            for e in r.chunk:
                if after_event and e.event_id==after_event: stop=True; break
                if isinstance(e,MegolmEvent):
                    try: e=self.client.decrypt_event(e)
                    except Exception: stats["undecryptable"]+=1; continue
                events.append(e)
            if stop: break
            token=r.end
        msgs=[]; max_bytes=int(self.cfg.archive.max_file_mb*2**20); last_human=None
        for e in reversed(events):
            if not isinstance(e,(RoomMessageText,RoomMessageNotice,RoomMessageEmote,RoomMessageMedia,RoomEncryptedMedia)): continue
            is_walden=e.sender==me
            if not is_walden: last_human=e.sender
            if only is not None and ((not is_walden and e.sender not in only) or (is_walden and last_human not in only)):
                stats["excluded"]+=1; continue
            body=getattr(e,"body","") or ""
            if (is_walden and WALDEN_NOISE.match(body)) or (not is_walden and body.strip().lower().startswith(self.cfg.matrix.command_prefix)): continue
            m=Message(e.event_id,e.sender,"" if is_walden else (room.user_name(e.sender) or e.sender),e.server_timestamp,body,is_walden)
            if isinstance(e,(RoomMessageMedia,RoomEncryptedMedia)):
                info=(e.source.get("content") or {}).get("info") or {}
                m.file={"name":body,"mimetype":info.get("mimetype") or getattr(e,"mimetype",None),"size":info.get("size")}
                if with_files and (info.get("size") or 0)<=max_bytes:
                    try:
                        data=await self._download(e.url)
                        if isinstance(e,RoomEncryptedMedia): data=decrypt_attachment(data,e.key["k"],e.hashes["sha256"],e.iv)
                        if len(data)<=max_bytes:
                            m.data=data; m.file.update(size=len(data),sha256=__import__("hashlib").sha256(data).hexdigest())
                        else: stats["skipped_files"]+=1
                    except Exception:
                        log.warning("Could not download %s",e.url,exc_info=True); stats["skipped_files"]+=1
                else: stats["skipped_files"]+=1
            msgs.append(m)
        return msgs,stats

    async def _download(self,mxc:str)->bytes:
        """A file from the content repository. Homeservers now require authenticated media
        (/_matrix/client/v1/media with the access token), which nio 0.26's download() does not send."""
        import httpx
        server,_,media_id=mxc.removeprefix("mxc://").partition("/")
        url=f"{self.client.homeserver.rstrip('/')}/_matrix/client/v1/media/download/{server}/{media_id}"
        async with httpx.AsyncClient(timeout=120,follow_redirects=True) as h:
            r=await h.get(url,headers={"Authorization":f"Bearer {self.client.access_token}"},params={"allow_redirect":"true"})
        if r.status_code!=200: raise RuntimeError(f"download {mxc}: HTTP {r.status_code} {r.text[:120]}")
        return r.content

    async def _messages_page(self,room_id,token,direction):
        """One page of history. Homeservers (or proxies in front of them) sometimes cut off big responses,
        especially while fetching a federated room's older history: retry with smaller pages."""
        last=None
        for limit in (100,100,30,30,10,10):
            try:
                r=await self.client.room_messages(room_id,start=token,direction=direction,limit=limit)
                if getattr(r,"chunk",None) is not None: return r
                last=RuntimeError(getattr(r,"message",None) or str(r))
            except Exception as e:
                last=e
            log.warning("Reading history of %s failed (page of %d), retrying smaller: %s",room_id,limit,last)
            await asyncio.sleep(2)
        raise RuntimeError(f"the homeserver keeps failing to send this room's history ({last})")

    async def _archive_prepare(self,requester_room:str,requester:str,target:str,key:str,salt:bytes,hours:float):
        was_member=target in self.client.rooms
        rid=await self._join(target)
        troom=self.client.rooms[rid]; name=getattr(troom,"display_name",None) or rid
        try: level=troom.power_levels.get_user_level(requester)
        except Exception: level=0
        # You must be a member of the room (and, if configured, a moderator). Everyone else is protected anyway:
        # only people who opt in with "!walden include" are archived.
        if requester not in troom.users or (self.cfg.archive.require_moderator and level<50):
            if not was_member: await self.client.room_leave(rid)
            return await self._send(requester_room,f"You need to be a {'moderator' if self.cfg.archive.require_moderator else 'member'} of {name} to archive it.")
        if any(j for j in self.archives.jobs("scheduled",rid)):
            return await self._send(requester_room,f"{name} is already scheduled to be archived. See {self.cfg.matrix.command_prefix} archive status.")
        room_ref=await self.agent.graph.ensure_room(rid); prev=self.archives.latest(room_ref)
        msgs,stats=await self._history(rid,prev["last_event_id"] if prev else None,None)
        if not msgs:
            return await self._send(requester_room,f"Nothing new to archive in {name}"+(f" since archive #{prev['seq']}." if prev else ".")
                                    +(f" ({stats['undecryptable']} messages I cannot decrypt.)" if stats["undecryptable"] else ""))
        frost=await self.archiver.price_files(msgs)
        try: bal=await self.checkpointer.balance_on(self.cfg.archive.sui_env)
        except Exception: bal=None
        people=sorted({m.name for m in msgs if not m.is_walden})
        files=[m for m in msgs if m.data is not None]
        wal=frost/FROST_PER_WAL
        wallet="unknown" if bal is None else f"{bal.get('SUI',0):.3f} SUI, {bal.get('WAL',0):.3f} WAL"+("" if bal.get("WAL",0)>=wal else " (NOT enough WAL)")
        key_enc=self._master().encrypt(key.encode()).decode()
        job={"id":uuid.uuid4().hex,"room_id":rid,"room_name":name,"requester":requester,"requester_room":requester_room,
             "deadline":None,"state":"proposed","included":set(),"key_enc":key_enc,"salt":salt.hex(),"estimate_frost":frost,
             "created_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
        self.pending_deploys[requester_room]={"kind":"archive","sender":requester,"job":job,"hours":hours,
                                              "expires":time.monotonic()+self.cfg.memory.checkpoint.confirm_timeout_seconds}
        span=f"{time.strftime('%Y-%m-%d',time.gmtime(msgs[0].ts/1000))} to {time.strftime('%Y-%m-%d',time.gmtime(msgs[-1].ts/1000))}"
        await self._send(requester_room,"\n".join([
            f"Archive {'#'+str(prev['seq']+1)+' of ' if prev else ''}{name}, ready to schedule:",
            f"- {len(msgs)} messages from {span}"+(f", continuing after archive #{prev['seq']}" if prev else "")+f", by {len(people)} people: {', '.join(people)}",
            f"- {len(files)} files ({sum(len(m.data) for m in files)/2**20:.1f} MiB) as encrypted Walrus {self.cfg.archive.walrus_context} blobs, "
            f"kept {self.cfg.archive.epochs} epochs: about {wal:.4f} WAL plus SUI gas. Wallet: {wallet}"
            +(f" ({stats['skipped_files']} files too big or unavailable: listed, not stored)" if stats["skipped_files"] else ""),
            "- Episodes, what Walden learns about each person, and the overview go to Walrus Memory (paid by its relayer), "
            "all encrypted with your password.",
            *( [f"- {stats['undecryptable']} messages I cannot decrypt (sent before I joined) will be missing."] if stats["undecryptable"] else []),
            f"- Opt-in: I introduce myself in {name} and wait {hours:g} hours. Only people who reply "
            f"{self.cfg.matrix.command_prefix} include are archived (their messages, files, and what I learn about them); "
            "the numbers above are the most it can be. Then I archive, post the IDs there, and leave.",
            f"Reply \"yes\" within {self.cfg.memory.checkpoint.confirm_timeout_seconds//60} minutes to schedule it, or \"no\" to cancel."]))

    async def _archive_confirm(self,room,pending):
        job=pending["job"]; hours=pending["hours"]
        job.update(state="scheduled",deadline=time.time()+hours*3600); self.archives.put_job(job)
        when=time.strftime('%Y-%m-%d %H:%M UTC',time.gmtime(job["deadline"]))
        requester_name=self._display_name(self.client.rooms.get(job["room_id"]),job["requester"]) or job["requester"]
        p=self.cfg.matrix.command_prefix
        await self._send(job["room_id"],
            "Hello, I am Walden, an AI agent derived from Frank (frank.udk.ai). I remember conversations, and I can keep them "
            "on Walrus, a decentralized storage network.\n"
            f"{requester_name} has asked me to archive this room. If you reply {p} include within the next {hours:g} hours "
            f"(until {when}), the traces of your presence here (your messages, your files, and what I learn about you) will be "
            f"stored in retrievable, encrypted form on Walrus{await self._kept_until()}. Nothing of anyone who does not reply "
            f"will be stored. Everyone included can have the password to decrypt it on request: send a direct message to "
            f"{requester_name} ({job['requester']}). To withdraw before {when}: {p} exclude")
        await self._send(room.room_id,f"Scheduled: {job['room_name']} will be archived on {when}. The notice is posted there. "
                                      f"{self.cfg.matrix.command_prefix} archive status / {self.cfg.matrix.command_prefix} archive cancel {job['room_id']}")

    async def _kept_until(self)->str:
        """How long the archive's files are paid for (Walrus Memory's own retention is the relayer's choice)."""
        days=await self.checkpointer.epoch_days_on(self.cfg.archive.walrus_context)
        if not days: return ""
        until=time.strftime('%Y-%m-%d',time.gmtime(time.time()+self.cfg.archive.epochs*days*86400))
        return f" (files paid for until {until}; the rest is kept by Walrus Memory)"

    # ---- running ------------------------------------------------------------------------------------------
    async def _archive_scheduler(self):
        for j in self.archives.jobs("running"):        # interrupted by a restart: run again
            j["state"]="scheduled"; self.archives.put_job(j)
        while True:
            try:
                for j in self.archives.jobs("scheduled"):
                    if j["deadline"] and j["deadline"]<=time.time():
                        j["state"]="running"; self.archives.put_job(j)
                        self._spawn(self._archive_run(j),f"Archiving {j['room_name'] or j['room_id']}",j["requester_room"])
            except Exception: log.exception("archive scheduler")
            await asyncio.sleep(60)

    async def _archive_run(self,job:dict[str,Any]):
        rid=job["room_id"]; name=job["room_name"] or rid; report=job["requester_room"]
        async def say(text): await self._send(report,f"[{name}] {text}")
        try:
            if rid not in self.client.rooms: await self._join(rid)
            key=self._master().decrypt(job["key_enc"].encode()).decode()
            room_ref=await self.agent.graph.ensure_room(rid); prev=self.archives.latest(room_ref)
            if not job["included"]:
                await self._send(rid,"Nobody asked to be included, so nothing from this room has been stored.")
                await self._send(rid,FAREWELL); await self.client.room_leave(rid)
                job.update(state="done",key_enc=None); self.archives.put_job(job)
                return await self._send(report,f"[{name}] Nobody opted in, so nothing was archived. I said goodbye and left.")
            msgs,stats=await self._history(rid,prev["last_event_id"] if prev else None,job["included"])
            if not msgs: raise RuntimeError("the people who opted in have no new messages to archive")
            frost=await self.archiver.price_files(msgs)
            if frost>2*max(job["estimate_frost"] or 0,10_000_000):
                raise RuntimeError(f"files now cost {frost/FROST_PER_WAL:.4f} WAL, more than twice the confirmed estimate; run the archive again to confirm")
            res=await self.archiver.run(room_id=rid,room_name=name,messages=msgs,password_key=key,salt=bytes.fromhex(job["salt"]),progress=say)
            p=self.cfg.matrix.command_prefix; cc=self.cfg.memory.checkpoint
            lines=[f"The memory of this room has been archived as {res.manifest_blob} (archive #{res.seq}: {res.episodes} episodes, "
                   f"{res.people} people who opted in, {res.facts} facts, {res.files} files).",
                   f"- Walrus Memory: {res.episodes+res.people+1} encrypted memories of agent {cc.memwal_account_id}: "
                   +cc.memwal_blob_url.format(blob_id=res.manifest_blob),
                   *[f"- File on Walrus: {f.get('name')}: https://walruscan.com/{self.cfg.archive.walrus_context}/blob/{f.get('blob_id')}"
                     for f in res.file_refs[:10]],
                   *([f"- … and {len(res.file_refs)-10} more files"] if len(res.file_refs)>10 else []),
                   f"Load it in another room with {p} load {res.manifest_blob} <password>; the password is available from "
                   f"{self._display_name(self.client.rooms.get(rid),job['requester']) or job['requester']} on request."]
            await self._send(rid,"\n".join(lines))
            await self._send(rid,FAREWELL)
            await self.client.room_leave(rid)
            job.update(state="done",key_enc=None); self.archives.put_job(job)
            await self._send(report,f"Archived {name} as {res.manifest_blob}: archive #{res.seq}, {res.episodes} episodes, {res.people} people who opted in, "
                                    f"{res.facts} facts, {res.files} files. I posted the IDs there, said goodbye and left. "
                                    f"Load it here with {p} load {res.manifest_blob} <password>.")
        except Exception as e:
            log.exception("archive run failed")
            # The key and the opt-ins are kept, so it can be retried without a new notice period.
            job.update(state="failed",error=str(e)[:300]); self.archives.put_job(job)
            p=self.cfg.matrix.command_prefix
            await self._send(report,f"[{name}] Archiving failed: {e}\nRetry with {p} archive retry (the people who opted in stay included), "
                                    f"or give up with {p} archive cancel {name}.")

    async def _leave_if_only_for_archive(self,room_id):
        # Walden stays in rooms it was part of; nothing to do here yet.
        return None
