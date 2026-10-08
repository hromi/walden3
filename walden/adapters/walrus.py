from __future__ import annotations
import asyncio, base64, json, tempfile
from pathlib import Path
import httpx
from ..ports import BlobStore

class WalrusCliBlobStore(BlobStore):
    def __init__(self, binary="walrus", config="", wallet="", gas_budget=None, epochs=20):
        self.binary=binary; self.config=config; self.wallet=wallet; self.gas_budget=gas_budget; self.epochs=epochs
    def _root(self):
        d={}
        if self.config: d["config"]=self.config
        if self.wallet: d["wallet"]=self.wallet
        if self.gas_budget is not None: d["gasBudget"]=self.gas_budget
        return d
    async def _run(self, req):
        p=await asyncio.create_subprocess_exec(self.binary,"json",json.dumps(req),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await p.communicate()
        if p.returncode: raise RuntimeError(f"walrus CLI failed: {err.decode(errors='replace')}")
        return json.loads(out)
    async def put(self,data):
        with tempfile.NamedTemporaryFile(prefix="walden-",suffix=".blob",delete=False) as f:
            f.write(data); name=f.name
        try:
            req=self._root(); req["command"]={"store":{"files":[name],"epochs":self.epochs}}
            res=await self._run(req); item=res[0]["blobStoreResult"]
            for variant in ("newlyCreated","alreadyCertified"):
                if variant in item:
                    obj=item[variant].get("blobObject", item[variant])
                    if "blobId" in obj: return obj["blobId"]
            raise RuntimeError(f"unexpected walrus store result: {item}")
        finally: Path(name).unlink(missing_ok=True)
    async def get(self,blob_id):
        req=self._root(); req["command"]={"read":{"blobId":blob_id}}
        res=await self._run(req)
        return base64.b64decode(res["blob"])

class WalrusHttpBlobStore(BlobStore):
    """Publisher/aggregator HTTP adapter. Publisher wallet pays storage fees."""
    def __init__(self,publisher_url,aggregator_url,epochs=20,bearer_token="",timeout=90):
        self.publisher=publisher_url.rstrip("/"); self.aggregator=aggregator_url.rstrip("/"); self.epochs=epochs
        self.token=bearer_token; self.client=httpx.AsyncClient(timeout=timeout)
    async def put(self,data):
        headers={"content-type":"application/octet-stream"}
        if self.token: headers["authorization"]="Bearer "+self.token
        r=await self.client.put(f"{self.publisher}/v1/blobs",params={"epochs":self.epochs},content=data,headers=headers)
        r.raise_for_status(); payload=r.json(); result=payload.get("blobStoreResult",payload)
        for variant in ("newlyCreated","alreadyCertified"):
            if variant in result:
                obj=result[variant].get("blobObject",result[variant])
                return obj["blobId"]
        raise RuntimeError(f"unexpected publisher response: {payload}")
    async def get(self,blob_id):
        r=await self.client.get(f"{self.aggregator}/v1/blobs/{blob_id}")
        r.raise_for_status(); return r.content
    async def close(self): await self.client.aclose()
