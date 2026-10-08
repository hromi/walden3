"""On-chain checkpoints of a room's memory on Walrus.

During conversation memory lives in the local store (free, immediate). A checkpoint publishes the
room's current memory as ONE Walrus blob:

    WALDEN-CHECKPOINT/1
    {public header: what is inside, hashes, link to the previous checkpoint}
    {AES-256-GCM envelope with the facts and episode summaries}

Anyone with the link can read the header and check the chain of checkpoints; only Walden (holding
memory.encryption_key) can read the facts. `verify` downloads a checkpoint back from Walrus,
decrypts it and checks the content hash and the chain.
"""
from __future__ import annotations
import asyncio, base64, hashlib, json, logging, sqlite3, tempfile, time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .config import Config
from .core.crypto import Cipher
from .core.facts import facts_of, in_room, new_id, _norm
from .persona import KERNEL, restates_prompt

log=logging.getLogger(__name__)

MAGIC=b"WALDEN-CHECKPOINT/1\n"
FROST_PER_WAL=1_000_000_000
MEMWAL_MAX_BYTES=64*1024  # memwal.MAX_REMEMBER_TEXT_BYTES

def sha256(b:bytes)->str: return hashlib.sha256(b).hexdigest()

def encode(header:dict[str,Any],envelope:bytes)->bytes:
    return MAGIC+json.dumps(header,ensure_ascii=False,sort_keys=True).encode()+b"\n"+envelope

def decode(blob:bytes)->tuple[dict[str,Any],bytes]:
    if not blob.startswith(MAGIC): raise ValueError("not a Walden checkpoint")
    head,_,envelope=blob[len(MAGIC):].partition(b"\n")
    return json.loads(head),envelope

@dataclass
class Bundle:
    blob:bytes
    header:dict[str,Any]

class CheckpointIndex:
    """Local record of a room's checkpoints, so each new one can link to the previous one."""
    def __init__(self,db):
        self.db=db
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS checkpoints(room_ref TEXT,seq INTEGER,blob_id TEXT,object_id TEXT,"
                      "content_sha256 TEXT,network TEXT,created_at TEXT,PRIMARY KEY(room_ref,seq,network))")
            if "namespace" not in [r[1] for r in c.execute("PRAGMA table_info(checkpoints)")]:
                c.execute("ALTER TABLE checkpoints ADD COLUMN namespace TEXT DEFAULT ''")
    def latest(self,room_ref,network)->dict[str,Any]|None:
        with sqlite3.connect(self.db) as c:
            c.row_factory=sqlite3.Row
            r=c.execute("SELECT * FROM checkpoints WHERE room_ref=? AND network=? ORDER BY seq DESC LIMIT 1",(room_ref,network)).fetchone()
        return dict(r) if r else None
    def get(self,room_ref,seq,network)->dict[str,Any]|None:
        with sqlite3.connect(self.db) as c:
            c.row_factory=sqlite3.Row
            r=c.execute("SELECT * FROM checkpoints WHERE room_ref=? AND seq=? AND network=?",(room_ref,seq,network)).fetchone()
        return dict(r) if r else None
    def all(self,network)->list[dict[str,Any]]:
        with sqlite3.connect(self.db) as c:
            c.row_factory=sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM checkpoints WHERE network=? ORDER BY room_ref,seq",(network,))]
    def by_blob(self,blob_id)->dict[str,Any]|None:
        with sqlite3.connect(self.db) as c:
            c.row_factory=sqlite3.Row
            r=c.execute("SELECT * FROM checkpoints WHERE blob_id=?",(blob_id,)).fetchone()
        return dict(r) if r else None
    def add(self,**row):
        row={"namespace":"",**row}
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO checkpoints(room_ref,seq,blob_id,object_id,content_sha256,network,created_at,namespace) "
                      "VALUES(:room_ref,:seq,:blob_id,:object_id,:content_sha256,:network,:created_at,:namespace)",row)

class Checkpointer:
    def __init__(self,cfg:Config,graph):
        self.cfg=cfg; self.c=cfg.memory.checkpoint; self.graph=graph; self._cipher=None; self._index=None
    @property
    def memwal(self)->bool: return self.c.backend=="memwal"
    @property
    def network(self)->str:
        """Key for the local checkpoint list and the header: which store a checkpoint lives in."""
        return f"walrus-memory:{self.c.memwal_server_url or self.c.memwal_env}" if self.memwal else self.c.context
    @property
    def where(self)->str:
        if self.memwal:
            return {"prod":"Walrus Memory (mainnet)","staging":"Walrus Memory (testnet)"}.get(self.c.memwal_env,f"Walrus Memory ({self.c.memwal_env})") \
                if not self.c.memwal_server_url else f"Walrus Memory ({self.c.memwal_server_url})"
        return f"Walrus {self.c.context}"
    def namespace(self,room_ref:str)->str:
        return f"{self.c.memwal_namespace}-{room_ref.rsplit('/',1)[-1].removeprefix('0x')[:16]}"
    def _memwal_client(self):
        if getattr(self,"_mw",None) is None:
            if not (self.c.memwal_key and self.c.memwal_account_id):
                raise RuntimeError("Walrus Memory is not configured: set MEMWAL_PRIVATE_KEY and MEMWAL_ACCOUNT_ID in .env")
            from memwal import MemWal
            kw={"key":self.c.memwal_key,"account_id":self.c.memwal_account_id,"namespace":self.c.memwal_namespace}
            if self.c.memwal_server_url: kw["server_url"]=self.c.memwal_server_url
            else: kw["env"]=self.c.memwal_env
            self._mw=MemWal.create(**kw)
        return self._mw
    # Created on first use: the key and the database are only needed once a checkpoint command runs.
    @property
    def cipher(self)->Cipher:
        if self._cipher is None: self._cipher=Cipher(self.cfg.memory.encryption_key,True)
        return self._cipher
    @property
    def index(self)->CheckpointIndex:
        if self._index is None: self._index=CheckpointIndex(self.cfg.memory.sui.local_db)
        return self._index

    # ---- building -------------------------------------------------------------------------
    async def build(self,room_ref:str,people:dict[str,str],self_ref:str)->Bundle:
        """people: person ref -> display name (room members and episode participants)."""
        payload_people={}
        for ref,name in people.items():
            facts=in_room(facts_of((await self.graph.resolve(ref)).get("memory")),room_ref)
            if facts: payload_people[ref]={"name":name,"facts":facts}
        walden=in_room(facts_of((await self.graph.resolve(self_ref)).get("memory")),room_ref)
        room_facts=facts_of((await self.graph.resolve(room_ref)).get("memory"))
        episodes=[]
        for ep_ref in reversed(await self.graph.episodes.recent(room_ref,10_000) if self.graph.episodes else []):
            ep=await self.graph.resolve(ep_ref)
            episodes.append({"ref":ep_ref,"when":ep.get("when"),"summary":(ep.get("what") or {}).get("summary",""),
                             "topics":(ep.get("what") or {}).get("topics",[])})
        payload={"schema":"walden/checkpoint-payload/1","room":room_ref,"people":payload_people,
                 "walden":{"ref":self_ref,"facts":walden},"room_facts":room_facts,"episodes":episodes}
        plain=json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()
        prev=self.index.latest(room_ref,self.network)
        header={
            "schema":"walden/checkpoint/1",
            "agent":"Walden, a socially situated AI agent derived from Frank (frank.udk.ai)",
            # Walden's immutable values are committed to in every checkpoint.
            "kernel_sha256":sha256(KERNEL.strip().encode()),
            "network":self.network,
            "room":room_ref,                      # pseudonymous: an HMAC of the Matrix room ID
            "seq":(prev["seq"]+1) if prev else 1,
            "previous":prev["blob_id"] if prev else None,
            "previous_content_sha256":prev["content_sha256"] if prev else None,
            "created_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
            "counts":{"people":len(payload_people),"person_facts":sum(len(p["facts"]) for p in payload_people.values()),
                      "walden_facts":len(walden),"room_facts":len(room_facts),"episodes":len(episodes)},
            "content_sha256":sha256(plain),
            "encryption":"AES-256-GCM (key held by Walden)",
        }
        return Bundle(encode(header,self.cipher.encrypt(plain)),header)

    # ---- walrus / sui -----------------------------------------------------------------------
    async def _run(self,*args:str,timeout:float=300)->bytes:
        p=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try: out,err=await asyncio.wait_for(p.communicate(),timeout)
        except asyncio.TimeoutError:
            p.kill(); raise RuntimeError(f"{args[0]} timed out after {timeout:.0f}s")
        if p.returncode:
            msg=err.decode(errors="replace").strip().splitlines()
            raise RuntimeError(f"{args[0]} failed: {msg[-1] if msg else p.returncode}")
        return out
    async def _walrus(self,command:dict[str,Any])->Any:
        req={"context":self.c.context,"command":command}
        if self.c.walrus_config: req["config"]=self.c.walrus_config
        return json.loads(await self._run(self.c.walrus_binary,"json",json.dumps(req)))
    async def _with_file(self,data:bytes,fn):
        with tempfile.NamedTemporaryFile(prefix="walden-checkpoint-",suffix=".bin",delete=False) as f:
            f.write(data); name=f.name
        try: return await fn(name)
        finally: Path(name).unlink(missing_ok=True)
    async def dry_run(self,bundle:Bundle)->dict[str,Any]:
        if self.memwal:
            # The relayer pays storage, so there is no price to ask for; only the size limit matters.
            size=len(bundle.blob)
            if size>MEMWAL_MAX_BYTES:
                raise RuntimeError(f"checkpoint is {size/1024:.0f} KiB; Walrus Memory takes at most {MEMWAL_MAX_BYTES//1024} KiB per memory")
            return {"size":size,"namespace":self.namespace(bundle.header["room"])}
        return await self._walrus_dry_run(bundle)
    async def _walrus_dry_run(self,bundle:Bundle)->dict[str,Any]:
        async def go(name):
            [r]=await self._walrus({"store":{"files":[name],"epochs":self.c.epochs,"dryRun":True}})
            return {"blob_id":r["blobId"],"size":r["unencodedSize"],"encoded_size":r["encodedSize"],"storage_frost":r["storageCost"]}
        return await self._with_file(bundle.blob,go)
    async def epoch_days(self)->float|None:
        if self.memwal: return None
        return await self.epoch_days_on(self.c.context)
    async def epoch_days_on(self,context:str)->float|None:
        try:
            d=json.loads(await self._run(self.c.walrus_binary,"--context",context,"info","--json",timeout=60))
            return d["epochInfo"]["epochDuration"]["secs"]/86400
        except Exception:
            log.warning("Could not read the Walrus epoch length",exc_info=True); return None
    async def balance(self)->dict[str,float]:
        if self.memwal: return {}
        return await self.balance_on(self.c.sui_env)
    async def balance_on(self,env:str)->dict[str,float]:
        out=json.loads(await self._run(self.c.sui_binary,"client","--client.env",env,"balance","--json",timeout=60))
        coins={}
        for item in (out[0] if out and isinstance(out[0],list) else []):
            meta=item.get("metadata") or {}; md=meta.get("metadata") or meta
            sym=md.get("symbol","?"); dec=int(md.get("decimals",9))
            coins[sym]=coins.get(sym,0)+int((item.get("balance") or {}).get("balance",0))/10**dec
        return coins
    async def store(self,bundle:Bundle)->dict[str,Any]:
        if self.memwal:
            ns=self.namespace(bundle.header["room"])
            r=await self._memwal_client().remember_and_wait(bundle.blob.decode("utf-8"),namespace=ns,timeout_ms=300_000)
            if not r.blob_id: raise RuntimeError("Walrus Memory did not return a blob ID")
            return {"blob_id":r.blob_id,"object_id":"","owner":r.owner,"namespace":ns,"memory_id":r.id,"status":"done"}
        return await self._walrus_store(bundle)
    async def _walrus_store(self,bundle:Bundle)->dict[str,Any]:
        async def go(name):
            [r]=await self._walrus({"store":{"files":[name],"epochs":self.c.epochs}})
            res=r.get("blobStoreResult",r)
            for variant in ("newlyCreated","alreadyCertified"):
                if variant in res:
                    v=res[variant]; obj=v.get("blobObject") or {}
                    return {"blob_id":obj.get("blobId") or v.get("blobId"),"object_id":obj.get("id") or v.get("object"),
                            "end_epoch":(obj.get("storage") or {}).get("endEpoch") or (obj.get("storage") or {}).get("end_epoch") or v.get("endEpoch"),
                            "cost_frost":v.get("cost"),"status":variant}
            raise RuntimeError(f"unexpected walrus store result: {res}")
        return await self._with_file(bundle.blob,go)
    async def set_attributes(self,object_id:str,header:dict[str,Any])->None:
        """Tag the blob's Sui object so checkpoints can be found on-chain without Walden's database."""
        args=[self.c.walrus_binary,"--context",self.c.context,"set-blob-attribute",object_id,
              "--attr","walden.kind","checkpoint","--attr","walden.room",header["room"],
              "--attr","walden.seq",str(header["seq"]),"--attr","walden.content_sha256",header["content_sha256"]]
        if header.get("previous"): args+=["--attr","walden.previous",header["previous"]]
        await self._run(*args)
    async def read(self,blob_id:str)->bytes:
        if self.memwal: return await self._memwal_read(blob_id)
        r=await self._walrus({"read":{"blobId":blob_id}})
        return base64.b64decode(r["blob"])
    async def _memwal_read(self,blob_id:str)->bytes:
        """Walrus Memory has no read-by-ID: list the room namespace's checkpoints and pick the one.
        The relayer removes the Seal layer; Walden's own AES envelope and content hash still prove
        that the content is exactly what Walden stored."""
        from memwal import RecallParams
        mw=self._memwal_client(); row=self.index.by_blob(blob_id)
        namespaces=[row["namespace"]] if row and row.get("namespace") else []
        if not namespaces:
            listed=await mw.list_namespaces()
            namespaces=[n.name for n in getattr(listed,"namespaces",[]) if n.name.startswith(self.c.memwal_namespace+"-")]
        for ns in namespaces:
            res=await mw.recall(RecallParams(query=MAGIC.decode().strip(),limit=100,namespace=ns))
            for m in res.results:
                if m.blob_id==blob_id: return m.text.encode("utf-8")
        raise ValueError(f"no checkpoint {blob_id} in Walden's Walrus Memory")

    # ---- verification ------------------------------------------------------------------------
    def open(self,blob:bytes,key:str|None=None)->tuple[dict[str,Any],dict[str,Any]]:
        """Decrypt a checkpoint and check its content hash. Raises on any mismatch.
        key: the encryption key of the Walden that made it, if not this one."""
        header,envelope=decode(blob)
        try: plain=(Cipher(key,True) if key else self.cipher).decrypt(envelope)
        except Exception as e: raise ValueError("cannot decrypt: wrong encryption key, or not made by this Walden") from e
        if sha256(plain)!=header.get("content_sha256"): raise ValueError("content hash does not match the header")
        return header,json.loads(plain)
    async def verify(self,blob_id:str,room_ref:str,max_chain:int=10)->list[str]:
        """Download, decrypt and check a checkpoint and the chain of checkpoints before it.
        Returns human-readable lines; raises if anything does not match."""
        lines=[]; current=blob_id; expect=None
        for _ in range(max_chain):
            header,payload=self.open(await self.read(current))
            if header.get("room")!=room_ref or payload.get("room")!=room_ref:
                raise ValueError(f"checkpoint {current} belongs to another room")
            if expect and header["content_sha256"]!=expect:
                raise ValueError(f"checkpoint {current} does not match the hash its successor recorded")
            n=header["counts"]
            lines.append(f"#{header['seq']} {current}: content hash ok, {n['person_facts']} facts about {n['people']} people, "
                         f"{n['walden_facts']} about Walden, {n['room_facts']} about the room, {n['episodes']} episodes ({header['created_at']})")
            if not header.get("previous"): break
            current,expect=header["previous"],header.get("previous_content_sha256")
        return lines
    def links(self,blob_id:str|None=None,object_id:str|None=None)->list[str]:
        out=[]
        if self.memwal:
            if blob_id: out.append(self.c.memwal_blob_url.format(blob_id=blob_id))
            if self.c.memwal_account_id: out.append(self.c.memwal_account_url.format(account_id=self.c.memwal_account_id))
            return [u for u in out if u]
        if blob_id:
            out.append(self.c.explorer_blob_url.format(blob_id=blob_id))
            out.append(self.c.aggregator_url.format(blob_id=blob_id))
        if object_id: out.append(self.c.explorer_object_url.format(object_id=object_id))
        return [u for u in out if u]


# ---- loading: carry a room's memory into another room ----------------------------------------
@dataclass
class ImportPlan:
    blob_id:str
    header:dict[str,Any]
    facts:dict[str,list[dict[str,Any]]]   # local entity ref -> new facts for the target room
    labels:dict[str,str]                  # local entity ref -> how to call it in messages
    episodes:list[dict[str,Any]]
    skipped_people:list[str]              # names of people in the checkpoint who could not be matched
    duplicates:int
    updates:dict[str,list[tuple[str,dict[str,Any]]]]=None  # local ref -> (target fact id, new source fact)
    removals:dict[str,list[dict[str,Any]]]=None             # local ref -> target facts gone from the source
    synced_from:int|None=None             # newest checkpoint seq loaded before from the same source room
    older:bool=False                      # this checkpoint is older than one loaded before: add-only

def _origin_of(f:dict[str,Any])->dict[str,Any]: return f.get("imported_from") or {}

def slice_payload(payload:dict[str,Any],*,person:str|None=None,episode:str|None=None)->dict[str,Any]:
    """Part of a checkpoint: only one person's facts, or only one episode and the facts learned in it."""
    out=dict(payload)
    if person is not None:
        out["people"]={r:p for r,p in (payload.get("people") or {}).items() if r==person}
        out["walden"]={**(payload.get("walden") or {}),"facts":[]}; out["room_facts"]=[]; out["episodes"]=[]
    if episode is not None:
        keep=lambda fs:[f for f in fs or [] if f.get("episode")==episode]
        out["people"]={r:{**p,"facts":keep(p.get("facts"))} for r,p in (payload.get("people") or {}).items()}
        out["people"]={r:p for r,p in out["people"].items() if p["facts"]}
        out["walden"]={**(payload.get("walden") or {}),"facts":keep((payload.get("walden") or {}).get("facts"))}
        out["room_facts"]=keep(payload.get("room_facts")); out["episodes"]=[e for e in payload.get("episodes") or [] if e.get("ref")==episode]
    return out

async def plan_import(cp:"Checkpointer",blob_id:str,header:dict[str,Any],payload:dict[str,Any],*,target_room:str,
                      members:dict[str,str],self_ref:str,partial:bool=False)->ImportPlan:
    """Work out what a checkpoint changes in `target_room`.

    People are matched by their pseudonymous ref (same for this Walden in every room), else by display
    name among `members` (ref -> name), for checkpoints made by another Walden. Facts the room already
    knows are skipped. If facts were loaded before from the same source room, loading a newer checkpoint
    of that room syncs them: reworded facts (same source fact id) are updated, facts no longer in the
    source are removed. The room's own facts and facts from other rooms are never touched.
    partial: the payload is only part of the checkpoint (one person or one episode): never remove anything."""
    g=cp.graph; by_name={n.casefold():r for r,n in members.items() if n}
    src_room=header.get("room"); created=header.get("created_at") or ""
    async def known(ref):
        try: await g.resolve(ref); return True
        except Exception: return False
    sources=[]  # (local ref, label, facts)
    skipped=[]
    for ref,person in (payload.get("people") or {}).items():
        local=ref if ref in members or await known(ref) else by_name.get((person.get("name") or "").casefold())
        if local is None: skipped.append(person.get("name") or "unknown"); continue
        sources.append((local,members.get(local) or person.get("name") or "someone",person.get("facts") or []))
    sources.append((self_ref,"Walden",(payload.get("walden") or {}).get("facts") or []))
    sources.append((target_room,"this room",payload.get("room_facts") or []))

    # Facts loaded before from the same source room, per local entity: {source fact id: target fact}.
    # Members without facts in the checkpoint are included, so their removed facts are found too.
    labels={local:label for local,label,_ in sources}
    for r,n in members.items(): labels.setdefault(r,n or "someone")
    targets={}
    prev=[]
    for local in labels:
        try: mem=(await g.resolve(local)).get("memory")
        except Exception: continue
        here=facts_of(mem) if local==target_room else in_room(facts_of(mem),target_room)
        targets[local]=here
        for f in here:
            o=_origin_of(f)
            if o.get("room")==src_room and src_room!=target_room: prev.append((o.get("created_at") or "",o.get("seq") or 0,o.get("network")))
    # The same source room may have been loaded from another store (testnet / Walrus Memory), whose
    # numbering starts again at #1, so "newer" is decided by when the checkpoint was made; the number
    # only decides within one store, or when times are equal or were not recorded (older imports).
    latest=max(prev,key=lambda x:(x[0],x[1])) if prev else None
    synced_from=latest[1] if latest else None
    older=latest is not None and ((bool(latest[0]) and created<latest[0])
                                  or ((created==latest[0] or not latest[0]) and latest[2]==header.get("network") and (header.get("seq") or 0)<latest[1]))

    facts={}; updates={}; removals={}; dup=0
    for local,label,items in sources:
        here=targets.get(local,[])
        loaded={_origin_of(f).get("fact_id"):f for f in here
                if _origin_of(f).get("room")==src_room and src_room!=target_room}
        have={_norm(f["fact"]) for f in here}
        source_ids=set()
        for f in items:
            if local==self_ref and restates_prompt(f["fact"],cp.cfg): continue  # Walden reciting its kernel is not knowledge
            source_ids.add(f.get("id"))
            earlier=loaded.get(f.get("id"))
            if earlier is not None and older:
                dup+=1; continue  # a newer version of this fact is already here
            if earlier is not None:
                if _norm(earlier["fact"])!=_norm(f["fact"]):
                    updates.setdefault(local,[]).append((earlier["id"],f)); have.add(_norm(f["fact"]))
                else: dup+=1
                continue
            if _norm(f["fact"]) in have: dup+=1; continue
            have.add(_norm(f["fact"]))
            facts.setdefault(local,[]).append(f)
        if not older and not partial:
            gone=[f for sid,f in loaded.items() if sid not in source_ids]
            if gone: removals[local]=gone
    # Members with facts from that source room but none in this checkpoint at all.
    if not older and not partial:
        for local,here in targets.items():
            if local in {l for l,_,_ in sources}: continue
            gone=[f for f in here if _origin_of(f).get("room")==src_room and src_room!=target_room]
            if gone: removals[local]=gone

    # Episodes already in the target room (imported before, or the room's own) are not added again.
    seen=set()
    for ep in await g.recent_episodes(target_room,100_000):
        seen.add(((ep.get("what") or {}).get("imported_from") or {}).get("episode"))
    seen|=set(await g.episodes.recent(target_room,100_000)) if g.episodes else set()
    episodes=[e for e in payload.get("episodes") or [] if e.get("ref") not in seen]
    return ImportPlan(blob_id,header,facts,labels,episodes,skipped,dup,updates,removals,synced_from,older)

async def apply_import(cp:"Checkpointer",plan:ImportPlan,target_room:str)->dict[str,int]:
    """Write the planned changes into the target room. Returns counts of added, updated, removed facts."""
    g=cp.graph; origin={"blob":plan.blob_id,"room":plan.header.get("room"),"seq":plan.header.get("seq"),"network":plan.header.get("network"),
                        "created_at":plan.header.get("created_at")}
    prov={"source":"checkpoint-import","imported_from":origin,"room":target_room}
    counts={"added":0,"updated":0,"removed":0}
    for ref in set(plan.facts)|set(plan.updates or {})|set(plan.removals or {}):
        mem=(await g.resolve(ref)).get("memory") or {}; facts=[dict(f) for f in facts_of(mem)]; taken={f["id"] for f in facts}
        drop={f["id"] for f in (plan.removals or {}).get(ref,[])}
        facts=[f for f in facts if f["id"] not in drop]; counts["removed"]+=len(drop)
        by_id={f["id"]:f for f in facts}
        for target_id,f in (plan.updates or {}).get(ref,[]):
            if target_id in by_id:
                by_id[target_id].update({k:f[k] for k in ("fact","said_by","said_by_name","at") if k in f})
                by_id[target_id]["imported_from"]={**origin,"fact_id":f.get("id"),"episode":f.get("episode")}
                counts["updated"]+=1
        for f in plan.facts.get(ref,[]):
            i=new_id(taken); taken.add(i)
            facts.append({**f,"id":i,"room":target_room,"imported_from":{**origin,"fact_id":f.get("id"),"episode":f.get("episode")},
                          "episode":None})
            counts["added"]+=1
        await g.set_memory(ref,{**mem,"facts":facts},prov)
    for ep in plan.episodes:
        await g.create_episode(room_ref=target_room,person_refs=[],when=ep.get("when") or {},where={"imported":True},
                               what={"type":"imported-episode","summary":ep.get("summary",""),"topics":ep.get("topics") or [],
                                     "imported_from":{**origin,"episode":ep.get("ref")}},
                               source={"imported_from":origin})
    return counts
