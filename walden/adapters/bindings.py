from __future__ import annotations
import sqlite3
from ..ports import BindingStore

class SQLiteBindings(BindingStore):
    def __init__(self,db):
        self.db=db
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS bindings(kind TEXT,key_hash TEXT,object_id TEXT,PRIMARY KEY(kind,key_hash))")
    async def get(self,kind,key_hash):
        with sqlite3.connect(self.db) as c: r=c.execute("SELECT object_id FROM bindings WHERE kind=? AND key_hash=?",(kind,key_hash)).fetchone()
        return r[0] if r else None
    async def put(self,kind,key_hash,object_id):
        with sqlite3.connect(self.db) as c: c.execute("INSERT OR REPLACE INTO bindings VALUES(?,?,?)",(kind,key_hash,object_id))

class SQLiteEpisodeIndex:
    """Which episodes belong to which room, newest first. Episodes are immutable blobs with no
    registry head, so without this index they could only be found by semantic search."""
    def __init__(self,db):
        self.db=db
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE IF NOT EXISTS episodes(episode_ref TEXT PRIMARY KEY,room_ref TEXT,ts INTEGER)")
    async def add(self,room_ref,episode_ref,ts):
        with sqlite3.connect(self.db) as c: c.execute("INSERT OR REPLACE INTO episodes VALUES(?,?,?)",(episode_ref,room_ref,ts))
    async def recent(self,room_ref,limit):
        with sqlite3.connect(self.db) as c:
            return [r[0] for r in c.execute("SELECT episode_ref FROM episodes WHERE room_ref=? ORDER BY ts DESC LIMIT ?",(room_ref,limit))]
