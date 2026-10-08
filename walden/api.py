from __future__ import annotations
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from .config import Config
from .memory_graph import MemoryGraph

class Patch(BaseModel): patch: dict
class EpisodeInput(BaseModel): room_ref:str|None=None; person_refs:list[str]; when:dict; where:dict={}; what:dict={}; source:dict={}

def make_app(cfg:Config,graph:MemoryGraph):
    app=FastAPI(title="Walden Memory Graph",version="0.2.0")
    async def auth(authorization: str|None):
        token=cfg.api.bearer_token
        if token and authorization != "Bearer "+token: raise HTTPException(401,"unauthorized")
    @app.get("/health")
    async def health(): return {"ok":True,"agent":cfg.agent.name}
    @app.get("/resolve")
    async def resolve(uri:str,authorization:str|None=Header(None)):
        await auth(authorization); return await graph.resolve(uri)
    @app.get("/lineage")
    async def lineage(uri:str,limit:int=50,authorization:str|None=Header(None)):
        await auth(authorization); return await graph.lineage(uri,limit)
    @app.post("/person")
    async def ensure_person(matrix_user_id:str,authorization:str|None=Header(None)):
        await auth(authorization); return {"ref":await graph.ensure_person(matrix_user_id)}
    @app.post("/room")
    async def ensure_room(matrix_room_id:str,authorization:str|None=Header(None)):
        await auth(authorization); return {"ref":await graph.ensure_room(matrix_room_id)}
    @app.patch("/person")
    async def patch_person(uri:str,p:Patch,authorization:str|None=Header(None)):
        await auth(authorization); return {"blob_id":await graph.update_person(uri,p.patch,{"source":"api"})}
    @app.patch("/room")
    async def patch_room(uri:str,p:Patch,authorization:str|None=Header(None)):
        await auth(authorization); return {"blob_id":await graph.update_room(uri,p.patch,{"source":"api"})}
    @app.post("/episode")
    async def episode(x:EpisodeInput,authorization:str|None=Header(None)):
        await auth(authorization); return {"ref":await graph.create_episode(**x.model_dump())}
    return app
