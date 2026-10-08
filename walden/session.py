from __future__ import annotations
import asyncio, time
from dataclasses import dataclass, field
from uuid import uuid4
from .core.models import MatrixEventRecord

@dataclass
class Session:
    room_id:str
    events:list[MatrixEventRecord]=field(default_factory=list)
    last_activity:float=field(default_factory=time.monotonic)
    chars:int=0
    # Facts are saved during the session: events[:saved] are already in memory.
    sid:str=field(default_factory=lambda:uuid4().hex[:12])
    saved:int=0
    summaries:list[str]=field(default_factory=list)
    topics:list[str]=field(default_factory=list)
    closed:bool=False
    def add(self,e):
        self.events.append(e); self.last_activity=time.monotonic(); self.chars+=len(e.body)
    def recent(self,max_events:int,max_chars:int):
        out=[]; chars=0
        for event in reversed(self.events[-max_events:]):
            n=len(event.body)
            if out and chars+n>max_chars: break
            out.append(event); chars+=n
        return list(reversed(out))

class SessionManager:
    def __init__(self,idle_seconds,max_events,max_chars,on_close,auto_commit=True):
        self.idle=idle_seconds; self.max_events=max_events; self.max_chars=max_chars
        self.on_close=on_close; self.auto_commit=auto_commit; self.sessions={}; self.task=None
    async def add(self,e,check_limits=True):
        s=self.sessions.setdefault(e.room_id,Session(e.room_id)); s.add(e)
        if check_limits: await self.check_limits(e.room_id)
    async def check_limits(self,room_id):
        s=self.sessions.get(room_id)
        if s and (len(s.events)>=self.max_events or s.chars>=self.max_chars):
            await self.close(room_id,"limit")
    async def close(self,room_id,reason="manual",force_commit=False):
        s=self.sessions.pop(room_id,None)
        if s and s.events and (self.auto_commit or force_commit):
            return await self.on_close(s,reason)
        return None
    async def sweep(self):
        while True:
            await asyncio.sleep(max(5,min(60,self.idle/4)))
            now=time.monotonic()
            for rid,s in list(self.sessions.items()):
                if now-s.last_activity>=self.idle: await self.close(rid,"idle")
    async def start(self):
        if not self.task: self.task=asyncio.create_task(self.sweep())
    async def stop(self):
        if self.task: self.task.cancel()
        for rid in list(self.sessions): await self.close(rid,"shutdown")
