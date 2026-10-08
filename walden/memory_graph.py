from __future__ import annotations
import json, logging
from typing import Any
from .config import Config
from .core.crypto import Cipher, pseudonym
from .core.merge import deep_merge
from .core.models import PersonRevision, RoomRevision, EpisodeRevision, ParticipantRef, Link
from .core.uri import parse, person as person_uri, room as room_uri, episode as episode_uri, blob as blob_uri
from .ports import BlobStore, EntityRegistry, BindingStore, SemanticIndex

log=logging.getLogger(__name__)

class MemoryGraph:
    def __init__(self,cfg:Config,blobs:BlobStore,registry:EntityRegistry,bindings:BindingStore,semantic:SemanticIndex,episodes=None):
        self.cfg=cfg; self.blobs=blobs; self.registry=registry; self.bindings=bindings; self.semantic=semantic; self.episodes=episodes
        self.cipher=Cipher(cfg.memory.encryption_key,cfg.memory.encrypt_blobs)
        self.cache:dict[str,dict[str,Any]]={}
    def _key(self,kind,external): return pseudonym(self.cfg.memory.identity_hmac_key,f"{kind}:{external}")
    async def _put_json(self,obj:dict[str,Any])->str:
        raw=json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str).encode()
        return await self.blobs.put(self.cipher.encrypt(raw))
    async def _get_json(self,blob_id:str)->dict[str,Any]:
        if self.cfg.memory.cache_reads and blob_id in self.cache: return self.cache[blob_id]
        raw=await self.blobs.get(blob_id); obj=json.loads(self.cipher.decrypt(raw))
        if self.cfg.memory.cache_reads:
            if len(self.cache)>=self.cfg.memory.cache_max_items: self.cache.pop(next(iter(self.cache)))
            self.cache[blob_id]=obj
        return obj
    async def ensure_person(self,matrix_user_id:str)->str:
        kh=self._key("person",matrix_user_id); oid=await self.bindings.get("person",kh)
        if oid: return person_uri(oid)
        # Two-step create: local registry IDs are known only after create, so initial snapshot is written after object creation with a temporary empty head.
        # In Sui mode, create with sentinel then immediately update to the first real immutable snapshot.
        head=await self.registry.create("person",kh,"PENDING")
        ref=person_uri(head.object_id)
        rev=PersonRevision(version=1,person_ref=ref,memory={},provenance={"origin":"matrix-first-observation"})
        bid=await self._put_json(rev.model_dump(by_alias=True,exclude_none=True))
        head=await self.registry.update(head.object_id,head.revision,bid)
        await self.bindings.put("person",kh,head.object_id)
        return ref
    async def ensure_room(self,matrix_room_id:str)->str:
        kh=self._key("room",matrix_room_id); oid=await self.bindings.get("room",kh)
        if oid: return room_uri(oid)
        head=await self.registry.create("room",kh,"PENDING"); ref=room_uri(head.object_id)
        rev=RoomRevision(version=1,room_ref=ref,memory={},provenance={"origin":"matrix-first-observation"})
        bid=await self._put_json(rev.model_dump(by_alias=True,exclude_none=True))
        head=await self.registry.update(head.object_id,head.revision,bid)
        await self.bindings.put("room",kh,head.object_id)
        return ref
    async def head(self,entity_ref:str):
        u=parse(entity_ref)
        if u.kind not in {"person","room"}: raise ValueError("not a stable entity URI")
        return await self.registry.get(u.ident)
    async def resolve(self,uri:str)->dict[str,Any]:
        u=parse(uri)
        if u.kind in {"person","room"}:
            h=await self.registry.get(u.ident); return await self._get_json(h.head_blob_id)
        return await self._get_json(u.ident)
    async def update_person(self,person_ref:str,patch:dict[str,Any],provenance:dict[str,Any]|None=None)->str:
        h=await self.head(person_ref); cur=PersonRevision.model_validate(await self._get_json(h.head_blob_id))
        merged=deep_merge(cur.memory,patch)
        nxt=PersonRevision(version=cur.version+1,person_ref=person_ref,previous=h.head_blob_id,memory=merged,provenance=provenance or {},links=[Link(rel="previous",ref=blob_uri(h.head_blob_id))])
        bid=await self._put_json(nxt.model_dump(by_alias=True,exclude_none=True))
        await self.registry.update(h.object_id,h.revision,bid)
        await self.semantic.remember(self._semantic_text(nxt.model_dump(by_alias=True)),self._namespace("person",h.key_hash))
        return bid
    async def update_room(self,room_ref:str,patch:dict[str,Any],provenance:dict[str,Any]|None=None)->str:
        h=await self.head(room_ref); cur=RoomRevision.model_validate(await self._get_json(h.head_blob_id))
        merged=deep_merge(cur.memory,patch)
        nxt=RoomRevision(version=cur.version+1,room_ref=room_ref,previous=h.head_blob_id,memory=merged,provenance=provenance or {},links=[Link(rel="previous",ref=blob_uri(h.head_blob_id))])
        bid=await self._put_json(nxt.model_dump(by_alias=True,exclude_none=True))
        await self.registry.update(h.object_id,h.revision,bid)
        await self.semantic.remember(self._semantic_text(nxt.model_dump(by_alias=True)),self._namespace("room",h.key_hash))
        return bid
    async def set_memory(self,entity_ref:str,memory:dict[str,Any],provenance:dict[str,Any]|None=None)->str:
        """Write a new person/room revision holding exactly `memory` (no merge). History stays in the lineage."""
        u=parse(entity_ref); h=await self.head(entity_ref); cur=await self._get_json(h.head_blob_id)
        Rev,field=(PersonRevision,"person_ref") if u.kind=="person" else (RoomRevision,"room_ref")
        nxt=Rev(version=cur.get("version",0)+1,previous=h.head_blob_id,memory=memory,provenance=provenance or {},
                links=[Link(rel="previous",ref=blob_uri(h.head_blob_id))],**{field:entity_ref})
        bid=await self._put_json(nxt.model_dump(by_alias=True,exclude_none=True))
        await self.registry.update(h.object_id,h.revision,bid)
        await self.semantic.remember(self._semantic_text(nxt.model_dump(by_alias=True)),self._namespace(u.kind,h.key_hash))
        return bid
    async def create_episode(self,*,room_ref:str|None,person_refs:list[str],when:dict[str,Any],where:dict[str,Any],what:dict[str,Any],source:dict[str,Any],raw_transcript=None,self_ref:str|None=None)->str:
        who=[]
        for p in person_refs:
            h=await self.head(p); who.append(ParticipantRef(person=p,memory_at_time=blob_uri(h.head_blob_id)))
        if self_ref:
            h=await self.head(self_ref); who.append(ParticipantRef(person=self_ref,memory_at_time=blob_uri(h.head_blob_id),role="self"))
        room_at=None
        if room_ref:
            rh=await self.head(room_ref); room_at=blob_uri(rh.head_blob_id)
        ep=EpisodeRevision(when=when,where=where,what=what,who=who,room=room_ref,room_memory_at_time=room_at,source=source,raw_transcript=raw_transcript)
        bid=await self._put_json(ep.model_dump(by_alias=True,exclude_none=True))
        # episode_ref is intentionally not embedded in the blob: content address would be self-referential.
        await self.semantic.remember(self._semantic_text(ep.model_dump(by_alias=True)),self._namespace("episode",bid[:24]))
        if room_ref and self.episodes: await self.episodes.add(room_ref,episode_uri(bid),when.get("end_ms") or 0)
        return episode_uri(bid)
    async def recent_episodes(self,room_ref:str,limit:int)->list[dict[str,Any]]:
        if not self.episodes or limit<=0: return []
        return [await self.resolve(ref) for ref in await self.episodes.recent(room_ref,limit)]
    async def lineage(self,entity_ref:str,limit:int|None=None)->list[dict[str,Any]]:
        h=await self.head(entity_ref); bid=h.head_blob_id; out=[]; limit=limit or self.cfg.memory.max_lineage_depth
        while bid and len(out)<limit:
            obj=await self._get_json(bid); out.append({"blob_id":bid,"revision":obj}); bid=obj.get("previous")
        return out
    async def semantic_recall(self,query:str,person_refs:list[str],room_ref:str|None=None,limit:int=6):
        namespaces=[]
        for p in person_refs:
            h=await self.head(p); namespaces.append(self._namespace("person",h.key_hash))
        if room_ref:
            h=await self.head(room_ref); namespaces.append(self._namespace("room",h.key_hash))
        return await self.semantic.recall(query,namespaces,limit)
    def _namespace(self,kind,key): return f"{self.cfg.memory.namespace_prefix}:{kind}:{key}"
    @staticmethod
    def _semantic_text(obj):
        # Avoid including Sui refs as the only searchable content; make structured JSON searchable while preserving provenance.
        return json.dumps(obj,ensure_ascii=False,sort_keys=True)
