from __future__ import annotations
import asyncio, json
from ..ports import EntityRegistry
from ..core.models import EntityHead

def _vec(s: str) -> str:
    return "[" + ",".join(str(b) for b in s.encode("utf-8")) + "]"

def _text(v) -> str:
    if isinstance(v, list): return bytes(int(x) for x in v).decode("utf-8")
    return str(v)

class SuiCliRegistry(EntityRegistry):
    """Uses the official Sui CLI to mutate the included Move Entity object."""
    def __init__(self,binary,package_id,module="memory_graph",gas_budget=50_000_000):
        if not package_id: raise ValueError("memory.sui.cli.package_id is required")
        self.binary=binary; self.package_id=package_id; self.module=module; self.gas_budget=gas_budget
    async def _run(self,*args):
        p=await asyncio.create_subprocess_exec(self.binary,*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await p.communicate()
        if p.returncode: raise RuntimeError(f"sui CLI failed: {err.decode(errors='replace')}")
        return json.loads(out)
    async def create(self,kind,key_hash,head_blob_id):
        # kind: 1 person, 2 room. vector<u8> arguments are UTF-8 bytes.
        k="1" if kind=="person" else "2"
        res=await self._run("client","call","--package",self.package_id,"--module",self.module,"--function","create_entity","--args",k,_vec(key_hash),_vec(head_blob_id),"--gas-budget",str(self.gas_budget),"--json")
        changes=res.get("objectChanges",[])
        created=next((x for x in changes if x.get("type")=="created" and x.get("objectType","").endswith("::Entity")),None)
        if not created: raise RuntimeError(f"Entity object not found in Sui effects: {res}")
        return EntityHead(object_id=created["objectId"],kind=kind,key_hash=key_hash,head_blob_id=head_blob_id,revision=1)
    async def _object(self,object_id):
        return await self._run("client","object",object_id,"--json")
    async def get(self,object_id):
        o=await self._object(object_id)
        content=o.get("content") or o.get("data",{}).get("content",{})
        fields=content.get("fields",{})
        kind="person" if int(fields.get("kind",1))==1 else "room"
        return EntityHead(object_id=object_id,kind=kind,key_hash=_text(fields["key_hash"]),head_blob_id=_text(fields["head_blob_id"]),revision=int(fields["revision"]))
    async def update(self,object_id,expected_revision,new_head_blob_id):
        await self._run("client","call","--package",self.package_id,"--module",self.module,"--function","update_head","--args",object_id,str(expected_revision),_vec(new_head_blob_id),"--gas-budget",str(self.gas_budget),"--json")
        return await self.get(object_id)
