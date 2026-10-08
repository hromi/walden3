from __future__ import annotations
import asyncio, hashlib, sqlite3, uuid
from pathlib import Path
from ..ports import BlobStore, EntityRegistry, BindingStore, SemanticIndex
from ..core.models import EntityHead, now_iso

class LocalBlobStore(BlobStore):
    def __init__(self, directory: str):
        self.dir=Path(directory); self.dir.mkdir(parents=True, exist_ok=True)
    async def put(self, data: bytes) -> str:
        blob_id=hashlib.sha256(data).hexdigest()
        path=self.dir/blob_id
        if not path.exists(): path.write_bytes(data)
        return blob_id
    async def get(self, blob_id: str) -> bytes:
        return (self.dir/blob_id).read_bytes()

class SQLiteRegistry(EntityRegistry, BindingStore):
    def __init__(self, db: str):
        self.db=db
        self._init()
    def _con(self):
        c=sqlite3.connect(self.db); c.row_factory=sqlite3.Row; return c
    def _init(self):
        with self._con() as c:
            c.execute("CREATE TABLE IF NOT EXISTS entities(object_id TEXT PRIMARY KEY,kind TEXT,key_hash TEXT,head_blob_id TEXT,revision INTEGER,updated_at TEXT)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS entities_key ON entities(kind,key_hash)")
            c.execute("CREATE TABLE IF NOT EXISTS bindings(kind TEXT,key_hash TEXT,object_id TEXT,PRIMARY KEY(kind,key_hash))")
    async def create(self, kind, key_hash, head_blob_id):
        oid="0x"+uuid.uuid4().hex+uuid.uuid4().hex
        h=EntityHead(object_id=oid,kind=kind,key_hash=key_hash,head_blob_id=head_blob_id,revision=1)
        with self._con() as c:
            c.execute("INSERT INTO entities VALUES(?,?,?,?,?,?)",(h.object_id,h.kind,h.key_hash,h.head_blob_id,h.revision,h.updated_at))
        return h
    async def get(self, object_id):
        with self._con() as c: r=c.execute("SELECT * FROM entities WHERE object_id=?",(object_id,)).fetchone()
        if not r: raise KeyError(object_id)
        return EntityHead(**dict(r))
    async def update(self, object_id, expected_revision, new_head_blob_id):
        with self._con() as c:
            r=c.execute("SELECT * FROM entities WHERE object_id=?",(object_id,)).fetchone()
            if not r: raise KeyError(object_id)
            if r["revision"] != expected_revision: raise RuntimeError(f"stale head: expected {expected_revision}, found {r['revision']}")
            rev=expected_revision+1; ts=now_iso()
            c.execute("UPDATE entities SET head_blob_id=?,revision=?,updated_at=? WHERE object_id=?",(new_head_blob_id,rev,ts,object_id))
        return EntityHead(object_id=object_id,kind=r["kind"],key_hash=r["key_hash"],head_blob_id=new_head_blob_id,revision=rev,updated_at=ts)
    async def get_binding(self, kind, key_hash):
        return await self.get(kind,key_hash)
    async def get(self, *args):
        # dual interface dispatch: get(object_id) for registry, get(kind,key_hash) for binding
        if len(args)==1:
            object_id=args[0]
            with self._con() as c: r=c.execute("SELECT * FROM entities WHERE object_id=?",(object_id,)).fetchone()
            if not r: raise KeyError(object_id)
            return EntityHead(**dict(r))
        kind,key_hash=args
        with self._con() as c: r=c.execute("SELECT object_id FROM bindings WHERE kind=? AND key_hash=?",(kind,key_hash)).fetchone()
        return r[0] if r else None
    async def put(self, kind, key_hash, object_id):
        with self._con() as c: c.execute("INSERT OR REPLACE INTO bindings VALUES(?,?,?)",(kind,key_hash,object_id))

class NullSemanticIndex(SemanticIndex):
    async def remember(self, text, namespace): return None
    async def recall(self, query, namespaces, limit): return []
