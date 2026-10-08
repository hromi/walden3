from __future__ import annotations
import logging
from typing import Any
from ..ports import SemanticIndex

log=logging.getLogger(__name__)

class MemWalSemanticIndex(SemanticIndex):
    def __init__(self,key,account_id,env="prod",server_url="",max_distance=0.75):
        try:
            from memwal import MemWal
        except ImportError as e:
            raise RuntimeError("MemWal Python SDK is not installed; install walden-agent[memwal]") from e
        kwargs={"key":key,"account_id":account_id,"namespace":"walden"}
        if server_url: kwargs["server_url"]=server_url
        else: kwargs["env"]=env
        self.client=MemWal.create(**kwargs); self.max_distance=max_distance
    async def health(self): return await self.client.health()
    async def remember(self,text,namespace):
        r=await self.client.remember_and_wait(text,namespace=namespace)
        return r.blob_id
    async def recall(self,query,namespaces,limit):
        from memwal import RecallParams
        out=[]
        for ns in namespaces:
            r=await self.client.recall(RecallParams(query=query,limit=limit,namespace=ns,max_distance=self.max_distance))
            out += [{"namespace":ns,"blob_id":x.blob_id,"text":x.text,"distance":x.distance} for x in r.results]
        out.sort(key=lambda x:x["distance"])
        return out[:limit]
    async def close(self): await self.client.close()

class BestEffortSemanticIndex(SemanticIndex):
    def __init__(self,inner): self.inner=inner
    async def remember(self,text,namespace):
        try: return await self.inner.remember(text,namespace)
        except Exception: log.exception("semantic mirror write failed"); return None
    async def recall(self,query,namespaces,limit):
        try: return await self.inner.recall(query,namespaces,limit)
        except Exception: log.exception("semantic recall failed"); return []
    async def close(self): await self.inner.close()
