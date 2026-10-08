"""Archiving a whole Matrix room into Walrus Memory ("!walden archive ROOM PASSWORD").

What an archive of one room consists of, all encrypted with a key derived from the password:

- one Walrus Memory memory per **episode** (a stretch of conversation): its summary, topics, participants and
  full transcript, linked to the previous episode (also across earlier archives of the room);
- one Walrus Memory memory per **participant**: the facts learned about them, each pointing at its episode,
  linked to that participant's memory in the previous archive;
- **files** (images, audio, documents) as plain Walrus blobs on mainnet, each encrypted;
- a **manifest** memory tying it all together: episodes, participants, files, where this archive starts
  and ends in the room's history, and the previous archive's manifest.

Re-archiving a room continues after the last archived message and links back, so a room's archives form one
chain. Facts and episode summaries come from the same extraction Walden uses for live conversation, run on a
scratch memory so nothing leaks into Walden's working memory; loading an archive ("!walden load <manifest>
<password>") brings them into a room like a checkpoint.
"""
from __future__ import annotations
import base64, copy, hashlib, json, logging, os, secrets, sqlite3, tempfile, time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from .config import Config
from .core.crypto import Cipher
from .core.facts import facts_of, in_room
from .core.models import MatrixEventRecord

log=logging.getLogger(__name__)

MAGIC=b"WALDEN-ARCHIVE/1\n"
KDF={"alg":"scrypt","n":2**15,"r":8,"p":1}
MAX_MEMORY_BYTES=60*1024   # Walrus Memory takes 64 KiB per memory; leave room for the header

def derive_key(password:str,salt:bytes,kdf:dict=KDF)->str:
    """Archive key from the password (hex, as Cipher expects). The salt is public, in every header."""
    return hashlib.scrypt(password.encode("utf-8"),salt=salt,n=kdf["n"],r=kdf["r"],p=kdf["p"],maxmem=2**27,dklen=32).hex()

def sha256(b:bytes)->str: return hashlib.sha256(b).hexdigest()

def encode(header:dict[str,Any],plain:bytes,cipher:Cipher)->bytes:
    header={**header,"content_sha256":sha256(plain)}
    return MAGIC+json.dumps(header,ensure_ascii=False,sort_keys=True).encode()+b"\n"+cipher.encrypt(plain)

def decode(blob:bytes)->tuple[dict[str,Any],bytes]:
    if not blob.startswith(MAGIC): raise ValueError("not a Walden archive")
    head,_,envelope=blob[len(MAGIC):].partition(b"\n")
    return json.loads(head),envelope

def open_piece(blob:bytes,password:str)->tuple[dict[str,Any],dict[str,Any]]:
    header,envelope=decode(blob)
    k=header.get("kdf") or {}
    key=derive_key(password,bytes.fromhex(k["salt"]),{**KDF,**{x:k[x] for x in ("n","r","p") if x in k}})
    try: plain=Cipher(key,True).decrypt(envelope)
    except Exception as e: raise ValueError("cannot decrypt the archive: wrong password") from e
    if sha256(plain)!=header.get("content_sha256"): raise ValueError("archive content does not match its hash")
    return header,json.loads(plain)

# ---- local bookkeeping ------------------------------------------------------------------------------
class ArchiveIndex:
    """Archives made (per room, in order) and archive jobs waiting for their notice period to end."""
    def __init__(self,db):
        self.db=db
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS archives(room_ref TEXT,seq INTEGER,manifest_blob TEXT,namespace TEXT,"
                      "last_event_id TEXT,last_ts INTEGER,created_at TEXT,episodes TEXT,people TEXT,PRIMARY KEY(room_ref,seq))")
            c.execute("CREATE TABLE IF NOT EXISTS archive_jobs(id TEXT PRIMARY KEY,room_id TEXT,room_name TEXT,requester TEXT,"
                      "requester_room TEXT,deadline REAL,state TEXT,excluded TEXT,key_enc TEXT,salt TEXT,estimate_frost INTEGER,"
                      "created_at TEXT,error TEXT)")
            # Opt-in: who said "!walden include". (Older versions stored opt-outs in "excluded".)
            if "included" not in [r[1] for r in c.execute("PRAGMA table_info(archive_jobs)")]:
                c.execute("ALTER TABLE archive_jobs ADD COLUMN included TEXT DEFAULT '[]'")
    def _rows(self,sql,args=()):
        with sqlite3.connect(self.db) as c:
            c.row_factory=sqlite3.Row
            return [dict(r) for r in c.execute(sql,args)]
    def latest(self,room_ref)->dict[str,Any]|None:
        r=self._rows("SELECT * FROM archives WHERE room_ref=? ORDER BY seq DESC LIMIT 1",(room_ref,))
        if not r: return None
        r[0]["episodes"]=json.loads(r[0]["episodes"] or "[]"); r[0]["people"]=json.loads(r[0]["people"] or "{}")
        return r[0]
    def by_manifest(self,blob_id)->dict[str,Any]|None:
        r=self._rows("SELECT * FROM archives WHERE manifest_blob=?",(blob_id,))
        return r[0] if r else None
    def add(self,**row):
        row={**row,"episodes":json.dumps(row.get("episodes") or []),"people":json.dumps(row.get("people") or {})}
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO archives VALUES(:room_ref,:seq,:manifest_blob,:namespace,:last_event_id,:last_ts,:created_at,:episodes,:people)",row)
    def put_job(self,job:dict[str,Any]):
        job={**job,"included":json.dumps(sorted(job.get("included") or []))}
        cols=["id","room_id","room_name","requester","requester_room","deadline","state","included","key_enc","salt","estimate_frost","created_at","error"]
        with sqlite3.connect(self.db) as c:
            c.execute(f"INSERT OR REPLACE INTO archive_jobs({','.join(cols)}) VALUES({','.join(':'+x for x in cols)})",{x:job.get(x) for x in cols})
    def jobs(self,state:str|None=None,room_id:str|None=None)->list[dict[str,Any]]:
        sql="SELECT * FROM archive_jobs WHERE 1=1"+(" AND state=?" if state else "")+(" AND room_id=?" if room_id else "")
        rows=self._rows(sql,tuple(x for x in (state,room_id) if x))
        for r in rows: r["included"]=set(json.loads(r.get("included") or "[]")); r.pop("excluded",None)
        return rows

# ---- the room's history -------------------------------------------------------------------------------
@dataclass
class Message:
    event_id:str
    sender:str
    name:str
    ts:int
    body:str
    is_walden:bool=False
    file:dict[str,Any]|None=None          # {"name","mimetype","size","sha256"} + later "blob_id","object_id"
    data:bytes|None=field(default=None,repr=False)   # file content, before upload

def segment(messages:list[Message],max_messages:int=80,max_chars:int=12_000,gap_ms:int=6*3600_000)->list[list[Message]]:
    """Episodes: a new one starts after a long pause, or when one would grow too big for a single memory."""
    out=[]; cur=[]; chars=0
    for m in messages:
        n=len(m.body)+200
        if cur and (len(cur)>=max_messages or chars+n>max_chars or m.ts-cur[-1].ts>gap_ms):
            out.append(cur); cur=[]; chars=0
        cur.append(m); chars+=n
    if cur: out.append(cur)
    return out

def transcript_line(m:Message)->dict[str,Any]:
    d={"event_id":m.event_id,"sender":m.name or m.sender,"ts":m.ts,"text":m.body}
    if m.file: d["file"]={k:m.file[k] for k in ("name","mimetype","size","sha256","blob_id","object_id") if k in m.file}
    return d

# ---- the pipeline ---------------------------------------------------------------------------------------
@dataclass
class ArchiveResult:
    manifest_blob:str
    seq:int
    episodes:int
    people:int
    files:int
    facts:int
    file_refs:list=field(default_factory=list)   # [{"name","blob_id","object_id",...}]

class Archiver:
    def __init__(self,cfg:Config,agent,checkpointer,index:ArchiveIndex):
        self.cfg=cfg; self.agent=agent; self.cp=checkpointer; self.index=index; self.a=cfg.archive

    def namespace(self,room_ref:str)->str:
        return f"{self.cfg.memory.checkpoint.memwal_namespace}-archive-{room_ref.rsplit('/',1)[-1].removeprefix('0x')[:16]}"

    async def _remember(self,text:bytes,namespace:str,retries:tuple=(15,45))->str:
        """One memory, retried: the relayer's upload to Walrus fails now and then ("exceeded step budget")."""
        if len(text)>MAX_MEMORY_BYTES: raise RuntimeError(f"archive piece is {len(text)//1024} KiB, over the Walrus Memory limit")
        import asyncio
        for attempt,wait in enumerate((*retries,None),1):
            try:
                r=await self.cp._memwal_client().remember_and_wait(text.decode("utf-8"),namespace=namespace,timeout_ms=600_000)
                if not r.blob_id: raise RuntimeError("Walrus Memory did not return a blob ID")
                log.info("Archive memory stored: %d bytes -> %s",len(text),r.blob_id)
                return r.blob_id
            except Exception as e:
                log.warning("Storing an archive memory (%d bytes) failed, attempt %d: %s",len(text),attempt,e)
                if wait is None: raise
                await asyncio.sleep(wait)

    async def _store_file(self,data:bytes,cipher:Cipher)->dict[str,Any]:
        """One file as an encrypted plain Walrus blob on the archive's network (mainnet)."""
        enc=cipher.encrypt(data)
        async def go(name):
            req={"context":self.a.walrus_context,"command":{"store":{"files":[name],"epochs":self.a.epochs}}}
            [r]=json.loads(await self.cp._run(self.cfg.memory.checkpoint.walrus_binary,"json",json.dumps(req)))
            res=r.get("blobStoreResult",r)
            for v in ("newlyCreated","alreadyCertified"):
                if v in res:
                    o=res[v].get("blobObject") or {}
                    return {"blob_id":o.get("blobId") or res[v].get("blobId"),"object_id":o.get("id") or res[v].get("object")}
            raise RuntimeError(f"unexpected walrus store result: {res}")
        return await self.cp._with_file(enc,go)

    async def price_files(self,messages:list[Message])->int:
        """Storage cost of all files in FROST (Walrus dry run, nothing stored)."""
        total=0
        for m in messages:
            if m.data is None: continue
            async def go(name):
                req={"context":self.a.walrus_context,"command":{"store":{"files":[name],"epochs":self.a.epochs,"dryRun":True}}}
                [r]=json.loads(await self.cp._run(self.cfg.memory.checkpoint.walrus_binary,"json",json.dumps(req)))
                return r["storageCost"]
            # encrypted size ~ 4/3 of the file plus the envelope: price that, not the plain file
            total+=await self.cp._with_file(b"x"*(len(m.data)*4//3+200),go)
        return total

    async def _scratch_agent(self):
        """A WaldenAgent on a throwaway memory store: extraction for the archive never touches live memory."""
        from .adapters.local import LocalBlobStore, SQLiteRegistry, NullSemanticIndex
        from .adapters.bindings import SQLiteEpisodeIndex
        from .memory_graph import MemoryGraph
        from .agent import WaldenAgent
        d=tempfile.mkdtemp(prefix="walden-archive-")
        cfg=copy.deepcopy(self.cfg); cfg.memory.walrus.local_dir=d+"/blobs"; cfg.memory.sui.local_db=d+"/db.sqlite"
        os.makedirs(cfg.memory.walrus.local_dir,exist_ok=True)
        reg=SQLiteRegistry(cfg.memory.sui.local_db)
        g=MemoryGraph(cfg,LocalBlobStore(cfg.memory.walrus.local_dir),reg,reg,NullSemanticIndex(),SQLiteEpisodeIndex(cfg.memory.sui.local_db))
        return WaldenAgent(cfg,g,self.agent.llm),d

    async def run(self,*,room_id:str,room_name:str,messages:list[Message],password_key:str,salt:bytes,
                  progress:Callable[[str],Awaitable[None]]|None=None)->ArchiveResult:
        """Archive `messages` (already filtered: only new ones, no opted-out senders, files downloaded)."""
        say=progress or (lambda t: _noop())
        g=self.agent.graph; room_ref=await g.ensure_room(room_id); ns=self.namespace(room_ref)
        cipher=Cipher(password_key,True); kdf={**KDF,"salt":salt.hex()}
        prev=self.index.latest(room_ref); seq=(prev["seq"]+1) if prev else 1
        created=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
        base={"schema":"walden/archive/1","room":room_ref,"seq":seq,"created_at":created,"kdf":kdf}

        # 1. files -> encrypted plain Walrus blobs
        files=[m for m in messages if m.data is not None]
        for i,m in enumerate(files,1):
            await say(f"Storing file {i}/{len(files)} on Walrus ({m.file.get('name')}) ...")
            m.file.update(await self._store_file(m.data,cipher)); m.data=None

        # 2. facts and summaries, episode by episode, on a scratch memory
        eps=segment(messages,self.a.episode_max_messages,self.a.episode_max_chars,int(self.a.episode_gap_hours*3600_000))
        scratch,tmpdir=await self._scratch_agent(); sg=scratch.graph
        scratch.cfg.matrix.user_id=self.cfg.matrix.user_id
        from .session import Session
        ep_refs=[]; summaries=[]
        for i,ep in enumerate(eps,1):
            await say(f"Reading episode {i}/{len(eps)} ({len(ep)} messages) ...")
            s=Session(room_id)
            for m in ep:
                body=m.body if not m.file else f"[shared a file: {m.file.get('name')} ({m.file.get('mimetype')})] {m.body if m.body!=m.file.get('name') else ''}".strip()
                s.add(MatrixEventRecord(event_id=m.event_id,room_id=room_id,sender=m.sender,timestamp_ms=m.ts,body=body,
                                        role="assistant" if m.is_walden else "human",sender_name="" if m.is_walden else m.name))
            ref=await scratch.commit_session(s,"archive"); ep_refs.append(ref)
            what=(await sg.resolve(ref)).get("what") or {} if ref else {}
            summaries.append((what.get("summary") or "",what.get("topics") or []))

        # 3. episodes -> Walrus Memory, each linked to the one before (also across archives)
        await say("Writing episodes to Walrus Memory ...")
        prev_ep=(prev["episodes"][-1] if prev and prev["episodes"] else None)
        ep_blobs=[]; idx_of={}
        for i,(ep,(summary,topics),ref) in enumerate(zip(eps,summaries,ep_refs),1):
            names=sorted({m.name or m.sender for m in ep if not m.is_walden})
            payload={"index":i,"when":{"start_ms":ep[0].ts,"end_ms":ep[-1].ts},"summary":summary,"topics":topics,"participants":names,
                     "previous_episode":prev_ep,"transcript":[transcript_line(m) for m in ep]}
            blob=await self._remember(encode({**base,"kind":"episode","index":i},json.dumps(payload,ensure_ascii=False).encode(),cipher),ns)
            ep_blobs.append(blob); prev_ep=blob
            if ref: idx_of[ref]=i

        # 4. participants -> Walrus Memory, facts pointing at their episodes, linked to their previous archive memory
        await say("Writing participants to Walrus Memory ...")
        scratch_room=await sg.ensure_room(room_id); people={}; n_facts=0
        for uid in sorted({m.sender for m in messages if not m.is_walden}):
            name=next((m.name for m in messages if m.sender==uid and m.name),uid)
            facts=in_room(facts_of((await sg.resolve(await sg.ensure_person(uid))).get("memory")),scratch_room)
            items=[{"id":f["id"],"fact":f["fact"],"said_by_name":f.get("said_by_name") or name,"at":f.get("at"),
                    "episode_index":idx_of.get(f.get("episode")),"episode":ep_blobs[idx_of[f["episode"]]-1] if f.get("episode") in idx_of else None}
                   for f in facts]
            ref=await g.ensure_person(uid)   # Walden's own pseudonymous ref, so loading finds the same person
            episodes_in=sorted({i for i,ep in enumerate(eps,1) if any(m.sender==uid for m in ep)})
            payload={"ref":ref,"name":name,"facts":items,"episodes":[ep_blobs[i-1] for i in episodes_in],
                     "previous":(prev["people"].get(ref) if prev else None)}
            people[ref]=await self._remember(encode({**base,"kind":"person"},json.dumps(payload,ensure_ascii=False).encode(),cipher),ns)
            n_facts+=len(items)
        walden_facts=in_room(facts_of((await sg.resolve(await scratch.self_ref())).get("memory")),scratch_room)
        room_facts=facts_of((await sg.resolve(scratch_room)).get("memory"))

        # 5. manifest
        manifest={"room":room_ref,"room_name":room_name,"seq":seq,"previous_manifest":prev["manifest_blob"] if prev else None,
                  "range":{"first_event":messages[0].event_id,"last_event":messages[-1].event_id,"first_ts":messages[0].ts,"last_ts":messages[-1].ts},
                  "episodes":[{"index":i,"blob":b,"when":{"start_ms":ep[0].ts,"end_ms":ep[-1].ts},"summary":s,"topics":t}
                              for i,(b,ep,(s,t)) in enumerate(zip(ep_blobs,eps,summaries),1)],
                  "people":[{"ref":r,"blob":b} for r,b in people.items()],
                  "files":[{**m.file,"episode":next(i for i,ep in enumerate(eps,1) if m in ep)} for m in files],
                  "walden_facts":[{"id":f["id"],"fact":f["fact"],"said_by_name":f.get("said_by_name"),"at":f.get("at")} for f in walden_facts],
                  "room_facts":[{"id":f["id"],"fact":f["fact"],"said_by_name":f.get("said_by_name"),"at":f.get("at")} for f in room_facts],
                  "messages":len(messages)}
        blob=await self._remember(encode({**base,"kind":"manifest"},json.dumps(manifest,ensure_ascii=False).encode(),cipher),ns)
        self.index.add(room_ref=room_ref,seq=seq,manifest_blob=blob,namespace=ns,last_event_id=messages[-1].event_id,
                       last_ts=messages[-1].ts,created_at=created,episodes=(prev["episodes"] if prev else [])+ep_blobs,
                       people={**(prev["people"] if prev else {}),**people})
        return ArchiveResult(blob,seq,len(eps),len(people),len(files),n_facts,[dict(m.file) for m in files])

    # ---- reading an archive back ---------------------------------------------------------------------
    async def read_pieces(self,manifest_blob:str,namespace:str|None=None)->dict[str,bytes]:
        """All memories in the archive's namespace, by blob ID (Walrus Memory has no read-by-ID)."""
        from memwal import RecallParams
        mw=self.cp._memwal_client(); row=self.index.by_manifest(manifest_blob)
        namespaces=[namespace or (row or {}).get("namespace")] if (namespace or row) else []
        if not namespaces:
            listed=await mw.list_namespaces()
            namespaces=[n.name for n in listed.namespaces if "-archive-" in n.name]
        out={}
        for ns in namespaces:
            count=next((n.memory_count for n in (await mw.list_namespaces()).namespaces if n.name==ns),100)
            res=await mw.recall(RecallParams(query=MAGIC.decode().strip(),limit=max(100,count+10),namespace=ns))
            got={m.blob_id:m.text.encode("utf-8") for m in res.results}
            if manifest_blob in got: out.update(got); break
        if manifest_blob not in out: raise ValueError(f"no archive {manifest_blob} in Walden's Walrus Memory")
        return out

    async def load_payload(self,manifest_blob:str,password:str)->tuple[dict[str,Any],dict[str,Any]]:
        """An archive as a checkpoint-like (header, payload) for plan_import: people with facts, episodes."""
        pieces=await self.read_pieces(manifest_blob)
        header,manifest=open_piece(pieces[manifest_blob],password)
        if header.get("kind")!="manifest": raise ValueError("that blob is a part of an archive, not its manifest")
        people={}
        for p in manifest.get("people") or []:
            if p["blob"] not in pieces: raise ValueError("a participant memory of this archive is missing")
            _,person=open_piece(pieces[p["blob"]],password)
            people[person["ref"]]={"name":person["name"],"facts":[
                {"id":f["id"],"fact":f["fact"],"said_by_name":f.get("said_by_name"),"at":f.get("at"),"episode":f.get("episode")}
                for f in person.get("facts") or []]}
        payload={"schema":"walden/checkpoint-payload/1","room":manifest["room"],"people":people,
                 "walden":{"facts":manifest.get("walden_facts") or []},"room_facts":manifest.get("room_facts") or [],
                 "episodes":[{"ref":e["blob"],"when":e["when"],"summary":e["summary"],"topics":e.get("topics") or []}
                             for e in manifest.get("episodes") or []]}
        hdr={"room":manifest["room"],"seq":manifest["seq"],"network":self.cp.network,"created_at":header.get("created_at"),
             "archive":True,"room_name":manifest.get("room_name"),"messages":manifest.get("messages"),
             "files":len(manifest.get("files") or []),"previous_manifest":manifest.get("previous_manifest")}
        return hdr,payload

async def _noop(): return None

def new_salt()->bytes: return secrets.token_bytes(16)
