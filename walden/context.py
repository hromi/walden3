from __future__ import annotations
import math, re, time
from .config import Config
from .memory_graph import MemoryGraph
from .core.facts import facts_of, in_room, render, _norm

_STOP={"the","and","you","your","what","which","who","whom","how","why","when","where","does","did","has","have","had","about",
       "this","that","with","from","for","are","was","were","been","can","could","would","should","will","tell","know","walden",
       "they","them","their","there","then","than","into","some","any","all","its","his","her","she","him"}
def _stems(text:str)->set[str]:
    return {w[:6] for w in _norm(text).split() if (len(w)>2 or w.isdigit()) and w not in _STOP}

def relevance(query:str,line:str)->float:
    """How much a memory line has to do with the question: shared word stems ("Compendium"/"compendium",
    "published"/"publish"), damped for long lines so a long fact does not win by size alone."""
    q=_stems(query)
    if not q: return 0.0
    w=_stems(line)
    return len(q&w)/math.sqrt(max(len(w),1))

class ContextBuilder:
    def __init__(self,cfg:Config,graph:MemoryGraph): self.cfg=cfg; self.graph=graph
    async def build(self,query:str,room_ref:str,people:dict[str,str],self_ref:str|None=None)->str:
        """people maps person ref -> display name: the current speaker plus everyone the message mentions.
        Nobody else's person memory is loaded, so it cannot leak into the answer."""
        if not self.cfg.recall.enabled: return ""
        sections=[]   # (title, lines); facts never leave the room they were learned in
        if self_ref:
            try:
                me=await self.graph.resolve(self_ref)
                sections.append(("WHAT YOU KNOW ABOUT YOURSELF, WALDEN",render(in_room(facts_of(me.get("memory")),room_ref)).splitlines()))
            except Exception: pass
        # Current snapshots for the speaker, mentioned people and room are deterministic, not semantic guesses.
        for ref,name in people.items():
            try:
                obj=await self.graph.resolve(ref)
                sections.append((f"WHAT YOU KNOW ABOUT {name} (another person, not you)",render(in_room(facts_of(obj.get("memory")),room_ref)).splitlines()))
            except Exception: pass
        try:
            room=await self.graph.resolve(room_ref)
            sections.append(("WHAT YOU KNOW ABOUT THIS ROOM",render(facts_of(room.get("memory"))).splitlines()))
        except Exception: pass
        try: episodes=await self.graph.recent_episodes(room_ref,self.cfg.recall.max_episodes)
        except Exception: episodes=[]
        if episodes:
            lines=[]
            for ep in reversed(episodes):
                end=(ep.get("when") or {}).get("end_ms")
                day=time.strftime("%Y-%m-%d %H:%M",time.gmtime(end/1000)) if end else "unknown time"
                what=ep.get("what") or {}
                topics=", ".join(what.get("topics") or [])
                lines.append(f"- {day} UTC: {what.get('summary','')}"+(f" [topics: {topics}]" if topics else ""))
            sections.append(("EARLIER EPISODES IN THIS ROOM (oldest first)",lines))
        sem=await self.graph.semantic_recall(query,list(people),room_ref,self.cfg.recall.max_semantic_items)
        if sem: sections.append(("SEMANTIC MEMORY",[x.get("text","") for x in sem]))
        return self._fit(query,sections,self.cfg.recall.max_context_chars)

    @staticmethod
    def _fit(query:str,sections:list[tuple[str,list[str]]],budget:int)->str:
        """Everything if it fits. Otherwise keep, in every section, the lines most relevant to the question
        (then the most recent ones), in their original order, with a note on what was left out. Cutting the
        text at the budget instead silently dropped whatever came last: the room, the episodes, newer facts."""
        full="\n\n".join(t+"\n"+"\n".join(ls) for t,ls in sections)
        if len(full)<=budget: return full
        # Lines the question points at are kept first, anywhere; then each section's newest lines in turn.
        ranked=[]
        for s,(t,ls) in enumerate(sections):
            for i,l in enumerate(ls):
                ranked.append((relevance(query,l),i/max(len(ls),1),s,i))
        keep=set()
        used=sum(len(t)+30 for t,_ in sections)
        for r,rec,s,i in sorted(ranked,key=lambda x:(-x[0],-x[1])):
            n=len(sections[s][1][i])+1
            if used+n>budget: continue
            keep.add((s,i)); used+=n
        out=[]
        for s,(t,ls) in enumerate(sections):
            kept=[l for i,l in enumerate(ls) if (s,i) in keep]
            dropped=len(ls)-len(kept)
            out.append(t+"\n"+"\n".join(kept)+(f"\n- ({dropped} less relevant lines not shown)" if dropped else ""))
        return "\n\n".join(out)
