from __future__ import annotations
import logging, re
from typing import Any
from .config import Config
from .core.facts import SELF, _norm, restates
from .core.models import MatrixEventRecord, Consolidation
from .llm import LLM, parse_json_text
from .persona import system_prompt, restates_prompt

log=logging.getLogger(__name__)

ROOM="room"
def _same(a:str,b:str)->bool: return _norm(a)==_norm(b)
KNOWS_ABOUT=re.compile(r"\s*(knows|remembers|learned|learnt|is aware|was told|has been told|has been informed)\b",re.IGNORECASE)

def schema(subjects:dict[str,list[str]],speakers:list[str])->dict:
    """Per-session schema. One section per subject (person alias, WALDEN, ROOM): the subject is
    the container, as with the older per-person patches, which small models get right far more
    often than a flat list where every entry names its subject. Under structured output the
    model can only use this session's speakers and, per subject, only ids of that subject's facts."""
    said_by={"type":"string","enum":speakers}
    def section(ids):
        return {"type":"object","additionalProperties":False,"properties":{
            "add":{"type":"array","items":{"type":"object","additionalProperties":False,
                   "properties":{"fact":{"type":"string"},"said_by":said_by},"required":["fact","said_by"]}},
            "update":{"type":"array","items":{"type":"object","additionalProperties":False,
                   "properties":{"id":{"type":"string","enum":ids or [""]},"fact":{"type":"string"},"said_by":said_by},"required":["id","fact","said_by"]}},
            "retract":{"type":"array","items":{"type":"object","additionalProperties":False,
                   "properties":{"id":{"type":"string","enum":ids or [""]},"said_by":said_by},"required":["id","said_by"]}}},
            "required":["add","update","retract"]}
    return {"type":"object","additionalProperties":False,"properties":{
              "episode_summary":{"type":"string"},
              "episode_topics":{"type":"array","items":{"type":"string"}},
              "memory":{"type":"object","additionalProperties":False,
                        "properties":{k:section(ids) for k,ids in subjects.items()},"required":list(subjects)}},
            "required":["episode_summary","episode_topics","memory"]}

class _People:
    """Aliases for a transcript: raw Matrix IDs never reach the model. Humans become P1, P2, ...
    and results are mapped back to authenticated senders."""
    def __init__(self,events:list[MatrixEventRecord]):
        self.senders=[]
        for e in events:
            if e.role=="human" and e.sender not in self.senders: self.senders.append(e.sender)
        self.alias={s:f"P{i+1}" for i,s in enumerate(self.senders)}
        self.by_alias={v:k for k,v in self.alias.items()}
        self.names={}
        for e in events:
            if e.role=="human" and e.sender_name: self.names.setdefault(e.sender,e.sender_name)
        self.by_name={n.casefold():s for s,n in self.names.items()}
    def resolve(self,key):
        # Models name people "P1", "P1 (DDH)" or just "DDH"; accept all three.
        key=(key or "").strip(); m=re.match(r"(P\d+)\b",key)
        if m and m.group(1) in self.by_alias: return self.by_alias[m.group(1)]
        if key.upper()=="WALDEN": return SELF
        return self.by_name.get(key.casefold())
    def label(self,s): return self.alias[s]+(f" ({self.names[s]})" if s in self.names else "")
    def unalias(self,text):
        # Facts outlive the session's aliases: "P1 (DDH)" / "P1" -> "DDH", "WALDEN" -> "Walden".
        text=re.sub(r"\b(P\d+)\b(\s*\([^)]*\))?",lambda m:self.names.get(self.by_alias.get(m.group(1),""),m.group(0)) if m.group(1) in self.by_alias else m.group(0),text)
        text=re.sub(r"\bWALDEN\b","Walden",text)
        return re.sub(r"\b([^()]{1,40}?) \(\1\)",r"\1",text)  # "The Archivist (The Archivist)" -> "The Archivist"
    def lines(self,evs):
        return "\n".join(f"[{self.label(e.sender) if e.role=='human' else 'WALDEN' if e.role=='assistant' else 'SYSTEM'}] {e.body}" for e in evs)

class Consolidator:
    def __init__(self,cfg:Config,llm:LLM): self.cfg=cfg; self.llm=llm
    async def consolidate(self,events:list[MatrixEventRecord],known:dict[str,list[dict[str,Any]]]|None=None,
                          context:list[MatrixEventRecord]|None=None)->Consolidation:
        """known: facts already stored, keyed by subject (Matrix user ID, "walden" or "room"), limited to this room.
        context: messages just before `events` (an earlier chunk), shown only so the model knows who said what.
        Returns operations with "about"/"said_by" resolved to Matrix user IDs, "walden" or "room"."""
        context=context or []
        if not self.cfg.memory.consolidation.enabled:
            return self._fallback(events)
        known=known or {}
        people=_People(context+events)

        # Never expose raw Matrix IDs to the consolidation model. Human speakers get
        # ephemeral aliases; results are mapped back to authenticated senders afterwards.
        human_senders=[]
        for e in context+events:
            if e.role=="human" and e.sender not in human_senders:
                human_senders.append(e.sender)
        external_to_alias={sender:f"P{i+1}" for i,sender in enumerate(human_senders)}
        alias_to_external={v:k for k,v in external_to_alias.items()}
        names={}
        for e in context+events:
            if e.role=="human" and e.sender_name: names.setdefault(e.sender,e.sender_name)
        name_to_external={n.casefold():s for s,n in names.items()}
        def resolve(key):
            # Models name people "P1", "P1 (DDH)" or just "DDH"; accept all three.
            key=(key or "").strip(); m=re.match(r"(P\d+)\b",key)
            if m and m.group(1) in alias_to_external: return alias_to_external[m.group(1)]
            if key.upper()=="WALDEN": return SELF
            return name_to_external.get(key.casefold())
        def label(s): return external_to_alias[s]+(f" ({names[s]})" if s in names else "")
        def unalias(text):
            # Facts outlive the session's aliases: "P1 (DDH)" / "P1" -> "DDH", "WALDEN" -> "Walden".
            text=re.sub(r"\b(P\d+)\b(\s*\([^)]*\))?",lambda m:names.get(alias_to_external.get(m.group(1),""),m.group(0)) if m.group(1) in alias_to_external else m.group(0),text)
            text=re.sub(r"\bWALDEN\b","Walden",text)
            return re.sub(r"\b([^()]{1,40}?) \(\1\)",r"\1",text)  # "The Archivist (The Archivist)" -> "The Archivist"

        def lines(evs):
            out=[]
            for e in evs:
                who=label(e.sender) if e.role=="human" else "WALDEN" if e.role=="assistant" else "SYSTEM"
                out.append(f"[{who}] {e.body}")
            return "\n".join(out)
        transcript=lines(events)
        if context:
            transcript=("EARLIER IN THIS SESSION (already processed; context only, take no facts from here):\n"+lines(context)
                        +"\n\nNEW MESSAGES (take facts only from here):\n"+transcript)

        # Sections: this session's people, Walden, the room. Only their facts (from this room) are
        # shown, so facts of anyone else cannot be updated or retracted from here.
        sections={external_to_alias[s]:s for s in human_senders}; sections.update({"WALDEN":SELF,"ROOM":ROOM})
        known_lines=[]; ids={}
        for key,subject in sections.items():
            facts=known.get(subject) or []; ids[key]=[f["id"] for f in facts]
            title=label(subject) if subject not in (SELF,ROOM) else key
            known_lines.append(f"{title}:\n"+("\n".join(f"- [{f['id']}] {f['fact']}" for f in facts) or "- (nothing yet)"))
        aliases=", ".join(external_to_alias.values()) or "none"
        prompt=(
            "Update Walden's memory from this Matrix session. Return JSON only.\n\n"
            "TRANSCRIPT:\n"+transcript+"\n\n"
            "KNOWN FACTS (from earlier sessions):\n"+"\n\n".join(known_lines)+"\n\n"
            "Rules:\n"
            "- episode_summary: at most 3 short sentences on what happened (what was said, decided or asked; not every exchange). Call people by their display name (shown in parentheses), never by alias. "
            "Say who said what: credit each piece of information to the person who actually said it. WALDEN repeating or answering is not a person sharing it.\n"
            "- episode_topics: at most 5 short topic tags.\n"
            f"- memory: one section per subject: each person ({aliases}), WALDEN and ROOM. In each section list only what changed in this session:\n"
            "  * add: new durable facts about that subject. One short statement per fact, without the subject's name "
            "(in section P1: \"has a dog named Juni\"). Never write aliases like P1 inside a fact; use display names.\n"
            "  * update: a known fact of that subject (by id) changed or became more precise; give the new statement.\n"
            "  * retract: a known fact of that subject (by id) turned out wrong or is no longer true.\n"
            f"  * said_by: who first said it in the transcript ({aliases} or WALDEN). WALDEN repeating or answering with something a person said is not WALDEN saying it.\n"
            "- A text a person shares about themselves that someone else wrote (an AI's description of them, a CV, a bio, an article) "
            "counts as that person sharing it: record its facts about them in their section, naming the source in the fact "
            "(\"according to GPT SOL, works on ...\"); said_by is the person who shared it.\n"
            "- What goes where: a person's section holds what that person said about themselves: name and what it stands for, family, pets, home, work, "
            "research, projects, preferences, beliefs, commitments. WALDEN's section holds roles and tasks people gave WALDEN, and WALDEN's own commitments, "
            "projects and views; credit them to whoever said them. Never record WALDEN's built-in values or self-description that WALDEN merely recites. ROOM holds the room's shared purpose, projects and agreements, not individual people's facts.\n"
            "- Facts are lasting statements about the subject, not a report of this conversation: \"is a Junior Professor\", not \"introduced themselves\" "
            "or \"summarized the known facts\". What happened in the conversation belongs in episode_summary.\n"
            "- Never add a fact that is already known. Do not infer sensitive attributes. "
            "Do not store passwords, authentication tokens, private keys, or transient small talk. Empty lists are allowed."
        )
        try:
            text=await self.llm.chat([{"role":"system","content":system_prompt(self.cfg)},{"role":"user","content":prompt}],
                                     schema(ids,list(external_to_alias.values())+["WALDEN"]))
            raw=parse_json_text(text)
            ops=[]
            for key,section in (raw.get("memory") or {}).items():
                subject=sections.get(key) or (ROOM if key.upper()=="ROOM" else resolve(key))
                if subject is None or not isinstance(section,dict):
                    log.warning("Consolidation section for unknown subject %r dropped",key); continue
                own={f["id"] for f in known.get(subject) or []}
                for kind in ("add","update","retract"):
                    for item in section.get(kind) or []:
                        said_by=resolve(item.get("said_by"))
                        if kind!="add" and item.get("id") not in own:
                            log.warning("Consolidation %s for %s dropped (unknown id): %r",kind,key,item); continue
                        text=self._keep(people,kind,subject,said_by,item.get("fact",""),key)
                        if text is None: continue
                        op={"op":kind,"about":subject,"said_by":said_by}
                        if kind!="add": op["id"]=item["id"]
                        if kind!="retract": op["fact"]=text
                        ops.append(op)
            return Consolidation(episode_summary=unalias(raw.get("episode_summary") or ""),episode_topics=raw.get("episode_topics") or [],operations=ops)
        except Exception:
            log.exception("LLM consolidation failed; using deterministic fallback")
            return self._fallback(events)
    def _keep(self,people:"_People",kind:str,subject:str,said_by:str|None,text:str,key:str)->str|None:
        """The checks every fact goes through. Returns the cleaned fact text, or None to drop it."""
        if said_by is None:
            log.warning("Fact %s for %s dropped (unknown speaker): %r",kind,key,text); return None
        if subject==SELF and kind!="retract" and KNOWS_ABOUT.match(text):
            # "knows that Lucia is DDH's wife" is a fact about someone else, not about Walden.
            log.info("Fact %s for WALDEN dropped (about someone else): %r",kind,text); return None
        if subject==SELF and kind!="retract" and restates_prompt(text,self.cfg):
            log.info("Fact %s for WALDEN dropped (restates its kernel/system prompt): %r",kind,text); return None
        if subject not in (SELF,ROOM) and said_by!=subject:
            # A person's memory holds only what they said about themselves.
            log.warning("Fact %s for %s dropped (said by someone else): %r",kind,key,text); return None
        text=people.unalias(text).strip()
        text=re.sub(r"^(she|he|they)\s+",lambda m:"",text,flags=re.IGNORECASE).rstrip(".")  # "She lives in X." -> "lives in X"
        name=people.names.get(subject) if subject not in (SELF,ROOM) else ("Walden" if subject==SELF else None)
        # "DDH stands for ..." in DDH's section is "stands for ...": same fact, found as a duplicate.
        if name and text.casefold().startswith(name.casefold()+" "): text=text[len(name)+1:]
        return text
    async def extract_more(self,events:list[MatrixEventRecord],known:dict[str,list[dict[str,Any]]],
                           context:list[MatrixEventRecord]|None=None,also_known:list[str]|None=None,rounds:int=2)->list[dict[str,Any]]:
        """Deep pass for long texts: ask only for facts in `events` that are not known yet, then ask
        again for anything still missing ("gleaning"), up to `rounds` times. A plain fact list is a
        much easier task for a small model than the full update format, so it finds far more.
        Returns add-operations (about/said_by resolved), like consolidate()."""
        context=context or []; people=_People(context+events)
        speakers=[e.sender for e in events if e.role=="human"]
        if not speakers or not self.cfg.memory.consolidation.enabled: return []
        sharer=speakers[-1]
        subjects=[people.alias[s] for s in people.senders]+["WALDEN","ROOM"]
        def subject_of(key):
            return ROOM if (key or "").strip().upper()=="ROOM" else people.resolve(key)
        listed=[f"- {f['fact']}" for fs in known.values() for f in fs]+[f"- {t}" for t in also_known or []]
        found=[]; shown=[]; ops=[]   # found: kept facts; shown: everything the model said (so it does not repeat itself)
        schema={"type":"object","additionalProperties":False,"required":["facts"],"properties":{"facts":{"type":"array","items":{
            "type":"object","additionalProperties":False,"required":["about","fact"],
            "properties":{"about":{"type":"string","enum":subjects},"fact":{"type":"string"}}}}}}
        for r in range(max(1,rounds)):
            prompt=(
                f"Below is {'part of ' if events[0].body.startswith('(part ') else ''}a message from {people.label(sharer)}. "
                "List every durable fact in it that is NOT already in KNOWN FACTS"+(" or FOUND SO FAR" if shown else "")+". Return JSON only.\n"
                "- One claim per fact: split a sentence that says several different things. Keep names, numbers, dates and places exactly.\n"
                "- Include beliefs, goals, values and opinions the text states (\"believes that ...\", \"wants ...\").\n"
                f"- \"about\": who the fact is about ({', '.join(subjects)}). If the text was written by someone else about "
                f"{people.label(sharer)} (an AI's description, a CV, a bio), its facts are still about {people.alias[sharer]}.\n"
                "- Leave out the subject's name at the start (\"founded kyberia.sk\", not \"DDH founded kyberia.sk\"), small talk, "
                "passwords and keys, and sensitive attributes that are only implied. An empty list is fine.\n\n"
                +("EARLIER IN THE CONVERSATION (context only):\n"+people.lines(context)+"\n\n" if context else "")
                +"TEXT:\n"+people.lines(events)+"\n\n"
                "KNOWN FACTS:\n"+("\n".join(listed) or "- (none)")
                +("\n\nFOUND SO FAR:\n"+"\n".join(f"- {t}" for t in shown) if shown else ""))
            try:
                raw=parse_json_text(await self.llm.chat([{"role":"system","content":system_prompt(self.cfg)},{"role":"user","content":prompt}],schema))
            except Exception:
                log.exception("Deep fact extraction failed"); break
            items=raw.get("facts") or []
            if not items: break  # the model says nothing is missing
            for item in items:
                subject=subject_of(item.get("about")); text=(item.get("fact") or "").strip()
                if subject is None or not text: continue
                shown.append(text)
                # The person who shared the text is the source of everything in it.
                text=self._keep(people,"add",subject,sharer if subject not in (SELF,ROOM) else sharer,text,item.get("about"))
                if not text or any(_same(text,t) or restates(text,t) for t in found+[l[2:] for l in listed]): continue
                found.append(text)
                ops.append({"op":"add","about":subject,"said_by":sharer,"fact":text})
        return ops
    async def merge_summaries(self,parts:list[str],max_words:int=80)->str:
        """One summary for a session that was read in several parts."""
        parts=[p.strip() for p in parts if p.strip()]
        if len(parts)<=1: return parts[0] if parts else ""
        prompt=(f"These are summaries of consecutive parts of one conversation. Write ONE factual summary of the whole "
                f"conversation in at most {max_words} words. Keep who said what; leave out repetition and small talk. "
                "Reply with the summary only.\n\n"+"\n".join(f"Part {i+1}: {p}" for i,p in enumerate(parts)))
        try:
            text=(await self.llm.chat([{"role":"system","content":system_prompt(self.cfg)},{"role":"user","content":prompt}])).strip()
            if text: return text
        except Exception:
            log.exception("Merging episode summaries failed; joining them instead")
        return " ".join(parts)[:1500]
    def _fallback(self,events):
        bodies=" ".join(e.body.strip() for e in events if e.body.strip())
        return Consolidation(episode_summary=bodies[:1500] or "Matrix session",episode_topics=[])
