"""Memory as a list of single facts, each with its origin.

A person's, Walden's own or a room's memory is {"facts": [fact, ...]} where a fact is
    {"id": "f-1a2b3c", "fact": "has a dog named Juni", "said_by": "wal://person/..." | "walden",
     "said_by_name": "DDH", "room": "wal://room/...", "episode": "wal://episode/...", "at": "2026-09-24",
     "session": "3f2a...", "events": ["$matrix-event-id", ...]}
Facts are saved during a session (after each exchange), so "episode" is null until the session
closes; "events" are the messages the fact was taken from.
After each session the extraction model returns operations instead of a new memory:
    {"op": "add", "about": <subject>, "fact": "...", "said_by": <speaker>}
    {"op": "update", "id": "f-...", "fact": "...", "said_by": <speaker>}
    {"op": "retract", "id": "f-...", "said_by": <speaker>}
Old revisions stay in the lineage, so an update or retraction never loses history.
"""
from __future__ import annotations
import re, secrets
from typing import Any

SELF="walden"

def new_id(taken:set[str])->str:
    while True:
        i="f-"+secrets.token_hex(3)
        if i not in taken: return i

def _norm(text:str)->str:
    words=re.sub(r"[\W_]+"," ",text.casefold()).split()
    return " ".join(w for w in words if w not in ("the","a","an"))

_FILLER={"she","he","they","her","his","their","him","them","is","are","was","were","has","have","had","been","be",
         "and","with","that","this","who","which","for","from","of","in","on","at","to","as","by","an","also","now","own","its"}
def _words(text:str)->set[str]:
    return {w for w in _norm(text).split() if (len(w)>1 or w.isdigit()) and w not in _FILLER}  # "has 2 sons" != "has 3 sons"
def restates(new:str,old:str,threshold:float=0.75)->bool:
    """True when `new` adds nothing to `old`: (almost) all its content words are already there.
    One-sided on purpose: a new fact with more detail than an old one is not a restatement."""
    def covered(text):
        a=_words(text)
        return bool(a) and len(a & _words(old))/len(a)>=threshold
    if covered(new): return True
    # "Kovac lives in Bratislava" vs "lives in Bratislava ...": the subject named in front, by a name
    # (here a surname) the known fact does not use.
    m=re.match(r"([A-Z][\w-]*)(?:'s)?\s+(.*)",new.strip())
    return bool(m) and m.group(1).casefold() not in _norm(old).split() and covered(m.group(2))

def facts_of(memory:dict[str,Any]|None)->list[dict[str,Any]]:
    return list((memory or {}).get("facts") or [])

def in_room(facts:list[dict[str,Any]],room_ref:str)->list[dict[str,Any]]:
    """Facts learned in this room. Facts from other rooms never leave their room."""
    return [f for f in facts if f.get("room")==room_ref]

def apply(memory:dict[str,Any]|None,ops:list[dict[str,Any]],*,room:str,episode:str|None,at:str,taken:set[str],
          extra:dict[str,Any]|None=None)->dict[str,Any]:
    """Return a new memory with ops applied. ops are already resolved: said_by is a ref or SELF,
    said_by_name a display name. Unknown ids are ignored. Keys other than "facts" are kept."""
    out=dict(memory or {}); facts=[dict(f) for f in facts_of(memory)]; by_id={f["id"]:f for f in facts}
    for op in ops:
        kind=op.get("op"); text=(op.get("fact") or "").strip()
        origin={"said_by":op.get("said_by"),"said_by_name":op.get("said_by_name",""),"room":room,"episode":episode,"at":at,**(extra or {})}
        if kind=="add" and text:
            if any(_norm(f["fact"])==_norm(text) or restates(text,f["fact"]) for f in facts): continue
            i=new_id(taken|set(by_id)); taken.add(i)
            f={"id":i,"fact":text,**origin}; facts.append(f); by_id[i]=f
        elif kind=="update" and text and op.get("id") in by_id:
            if any(_norm(f["fact"])==_norm(text) for f in facts if f["id"]!=op["id"]):
                # The new wording already exists as another fact: the old one is simply superseded.
                facts=[f for f in facts if f["id"]!=op["id"]]; by_id.pop(op["id"])
            else:
                by_id[op["id"]].update({"fact":text,**origin})
        elif kind=="retract" and op.get("id") in by_id:
            facts=[f for f in facts if f["id"]!=op["id"]]; by_id.pop(op["id"])
    out["facts"]=facts
    return out

def render(facts:list[dict[str,Any]],with_ids:bool=False)->str:
    lines=[]
    for f in facts:
        who=f.get("said_by_name") or ("Walden" if f.get("said_by")==SELF else "someone")
        lines.append(f"- {'['+f['id']+'] ' if with_ids else ''}{f['fact']} (said by {who}, {f.get('at','?')})")
    return "\n".join(lines) or "- (nothing yet)"
