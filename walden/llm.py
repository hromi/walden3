from __future__ import annotations
import json, re
from typing import Any
import httpx
from .config import LLMConfig

class LLM:
    async def chat(self,messages:list[dict[str,str]],json_schema:dict[str,Any]|None=None)->str: raise NotImplementedError

class EchoLLM(LLM):
    async def chat(self,messages,json_schema=None): return "Echo: "+messages[-1]["content"]

class OpenAICompatibleLLM(LLM):
    def __init__(self,cfg:LLMConfig):
        self.cfg=cfg; self.client=httpx.AsyncClient(timeout=cfg.timeout_seconds)
    async def chat(self,messages,json_schema=None):
        payload={"model":self.cfg.model,"messages":messages,"temperature":self.cfg.temperature,"max_tokens":self.cfg.max_tokens}
        if json_schema:
            # Structured extraction (memory saving) must be repeatable and must not be cut off mid-JSON.
            payload["temperature"]=0; payload["max_tokens"]=max(self.cfg.max_tokens,4096)
        if json_schema and self.cfg.json_mode:
            payload["response_format"]={"type":"json_schema","json_schema":{"name":"walden_output","strict":True,"schema":json_schema}}
        headers={"content-type":"application/json"}
        if self.cfg.api_key: headers["authorization"]="Bearer "+self.cfg.api_key
        r=await self.client.post(self.cfg.base_url.rstrip("/")+"/chat/completions",json=payload,headers=headers); r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    async def close(self): await self.client.aclose()

def make_llm(cfg): return EchoLLM() if cfg.provider=="echo" else OpenAICompatibleLLM(cfg)

def parse_json_text(text:str)->dict[str,Any]:
    try: return json.loads(text)
    except Exception:
        m=re.search(r"\{.*\}",text,re.S)
        if not m: raise
        return json.loads(m.group(0))
