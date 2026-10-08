from __future__ import annotations
import asyncio, logging, re, time
from .config import Config
from .context import ContextBuilder
from .consolidation import Consolidator, ROOM
from .core.facts import SELF, apply as apply_facts, facts_of, in_room
from .core.models import MatrixEventRecord
from .memory_graph import MemoryGraph
from .llm import LLM
from .persona import system_prompt

log=logging.getLogger(__name__)

# Human turns without a display name are sent as "[wal://person/...] text", and models copy that
# label, or a "Walden:" speaker tag, to the start of their reply.
SPEAKER_LABEL=re.compile(r"^\s*((\[wal://[^\]]*\]|walden\s*:)\s*)+",re.IGNORECASE)
# Internal memory URIs are for the model, never for the room: "DDH (wal://person/0x...)" -> "DDH".
INLINE_REF=re.compile(r"\s*[(\[]\s*wal://[^)\]\s]*\s*[)\]]|\s*wal://[\w/]+")
_VERB={"are":"is","were":"was","do":"does","have":"has","am":"is"}
def resolve_pronouns(text:str,asker:str)->str:
    """Rewrite a question from Walden's point of view with names instead of pronouns:
    "what do you know about me?" -> "what does Walden know about DDH?". In a question "you" means
    Walden, but in Walden's answer "you" means the asker; small models mix the two up."""
    t=re.sub(r"\b(are|were|do|have)\s+you\b",lambda m:f"{_VERB[m.group(1).lower()]} Walden",text,flags=re.I)
    t=re.sub(r"\byou\s+(are|were|have)\b",lambda m:f"Walden {_VERB[m.group(1).lower()]}",t,flags=re.I)
    t=re.sub(r"\b(am|do|have)\s+I\b",lambda m:f"{_VERB[m.group(1).lower()]} {asker}",t)
    t=re.sub(r"\bI\s+(am|have)\b",lambda m:f"{asker} {_VERB[m.group(1).lower()]}",t)
    t=re.sub(r"\bI'm\b",f"{asker} is",t)
    for pat,rep in ((r"\byourself\b","Walden"),(r"\byours\b","Walden's"),(r"\byour\b","Walden's"),(r"\byou\b","Walden"),
                    (r"\bmyself\b",asker),(r"\bmine\b",f"{asker}'s"),(r"\bmy\b",f"{asker}'s"),(r"\bme\b",asker)):
        t=re.sub(pat,rep,t,flags=re.I)
    return re.sub(r"\bI\b",asker,t)

YOU_WORDS=re.compile(r"\b(you|your|yours|yourself)\b",re.IGNORECASE)
THIRD_PERSON=re.compile(r"\b(he|she|they|him|her|them|his|hers|their)\b",re.IGNORECASE)
VALUES_WORDS=re.compile(r"\b(values?|inspir\w*|believe\w*|principles?|ethic\w*|stand for|care about)\b",re.IGNORECASE)
LONG_MESSAGE=1000  # characters; longer messages are read in parts
PASS_CHARS=1600    # characters per extraction pass
def split_long(e:MatrixEventRecord,size:int=800)->list[MatrixEventRecord]:
    """A long message as several parts (paragraphs, then sentences), each a copy of the event."""
    if e.role!="human" or len(e.body)<=LONG_MESSAGE: return [e]
    pieces=[]
    for para in re.split(r"\n\s*\n",e.body):
        pieces+= [para] if len(para)<=size else re.split(r"(?<=[.!?])\s+",para)
    parts=[]; cur=""
    for piece in pieces:
        piece=piece.strip()
        if not piece: continue
        if cur and len(cur)+len(piece)+1>size: parts.append(cur); cur=""
        cur=(cur+"\n"+piece) if cur else piece
    if cur: parts.append(cur)
    n=len(parts)
    return [e.model_copy(update={"body":f"(part {i+1}/{n} of one message) {p}"}) for i,p in enumerate(parts)]

ASKER_WORDS=re.compile(r"\b(i|me|my|mine|myself|i'm|i've)\b",re.IGNORECASE)

def clean_reply(text:str)->str:
    return INLINE_REF.sub("",SPEAKER_LABEL.sub("",text)).strip()

SENTENCE=re.compile(r"[^.!?\n]+[.!?]*\s*")
def _norm(t:str)->str: return re.sub(r"[\W_]+"," ",t.casefold()).strip()
MEMORY_NOTE=("Memory was loaded","Walden forgot a fact")
def drop_stale_replies(history:list,question:str)->list:
    """Leave out Walden's replies the model would otherwise copy although they no longer hold:
    replies from before a memory change (load/forget), and the earlier answer to a question that
    is being asked again (asking again means a fresh answer is wanted)."""
    last_change=max((i for i,t in enumerate(history) if t.role=="system" and t.body.startswith(MEMORY_NOTE)),default=-1)
    q=_norm(question); out=[]; drop_next=False
    for i,t in enumerate(history):
        if t.role=="assistant" and (i<last_change or drop_next):
            drop_next=False; continue
        drop_next=t.role=="human" and _norm(t.body)==q
        out.append(t)
    return out

def drop_repeats(reply:str,earlier:list[str])->str:
    """Remove sentences Walden already said in recent replies. Small models copy their previous
    answer when two questions look alike; whatever they generate, this keeps only what is new.
    If every sentence is a repeat, the reply is kept as it is rather than sending nothing."""
    said={_norm(x) for e in earlier for x in SENTENCE.findall(e) if _norm(x)}
    bags=[set(_norm(e).split()) for e in earlier]
    def repeat(x):
        n=_norm(x); words=n.split()
        if n in said: return True
        # Reworded repeats: nearly every word of the sentence comes from one earlier reply.
        return len(words)>=6 and any(sum(w in b for w in words)/len(words)>=0.9 for b in bags)
    # Pieces without words (")", "...") are kept: dropping them glued "(Degrowth ? Now!) It" into "Now!It".
    kept=[x for x in SENTENCE.findall(reply) if not _norm(x) or not repeat(x)]
    out=re.sub(r"[ \t]*\n\s*\n\s*","\n\n","".join(kept)).strip()
    return out if out else reply

class WaldenAgent:
    def __init__(self,cfg:Config,graph:MemoryGraph,llm:LLM):
        self.cfg=cfg; self.graph=graph; self.llm=llm; self.context=ContextBuilder(cfg,graph); self.consolidator=Consolidator(cfg,llm)
        self._locks:dict[str,asyncio.Lock]={}
    def self_id(self)->str:
        return self.cfg.matrix.user_id or f"walden:self:{self.cfg.agent.instance_id}"
    async def self_ref(self)->str:
        """Walden's own memory entity. It is a participant like everyone else, stored like a person."""
        return await self.graph.ensure_person(self.self_id())
    async def respond(self,event:MatrixEventRecord,mentioned:list[str],recent_events:list[MatrixEventRecord]|None=None,names:dict[str,str]|None=None,
                      carried:bool=False)->str:
        """mentioned: Matrix IDs of people the message refers to. names: Matrix ID -> display name.
        carried: `mentioned` was not named in this message but carried over from the previous question
        because this one says "he"/"she"/"they"."""
        names=names or {}
        sender_ref=await self.graph.ensure_person(event.sender)
        room_ref=await self.graph.ensure_room(event.room_id)
        label=lambda uid,ref: names.get(uid) or ref
        people={sender_ref:label(event.sender,sender_ref)}
        for external_id in mentioned:
            ref=await self.graph.ensure_person(external_id)
            people.setdefault(ref,label(external_id,ref))
        # When the message is about other people and not about the asker ("what do you know about
        # The Archivist?"), the asker's memory stays out: given it, small models recite it regardless.
        about_others=len(people)>1 and not ASKER_WORDS.search(event.body)
        recall={r:n for r,n in people.items() if not (about_others and r==sender_ref)}
        mem=await self.context.build(event.body,room_ref,recall,await self.self_ref())
        messages=[{"role":"system","content":system_prompt(self.cfg,with_capabilities=True)}]
        others=", ".join(f"{n} ({r})" for r,n in people.items() if r!=sender_ref) or "none"
        messages.append({
            "role":"system",
            "content":f"Matrix room: {room_ref}\nCurrent human speaker: {people[sender_ref]} ({sender_ref})\nPeople mentioned in this message: {others}",
        })
        if mem:
            messages.append({"role":"system","content":"Relevant Walden memory (fallible evidence; respect scope):\n"+mem})

        # The conversation goes to the model as ONE labelled transcript plus a task naming the exact
        # message to answer. As separate chat turns, small models lose track of which message they are
        # answering, repeat earlier answers, and mix up who "you" is in multi-person rooms.
        history=[t for t in (recent_events or []) if t.event_id!=event.event_id]
        history=drop_stale_replies(history,event.body)
        person_cache={event.sender:sender_ref}
        lines=[]
        for turn in history:
            if turn.role=="assistant": who="Walden (you)"
            elif turn.role=="system": who="system"
            else:
                ref=person_cache.get(turn.sender)
                if ref is None:
                    ref=await self.graph.ensure_person(turn.sender); person_cache[turn.sender]=ref
                who=turn.sender_name or names.get(turn.sender) or ref
            lines.append(f"[{who}] {turn.body}")
        asker=people[sender_ref]
        # Only the example that fits this question: showing "You are ..." next to the asker's name
        # while the question is about someone else makes small models describe the asker.
        others=[n for r,n in people.items() if r!=sender_ref]
        if about_others:
            pronoun_rule=(f"The question is about {', '.join(others)}, not about {asker}. "
                          +" ".join(f"Refer to {o} by name (\"{o} is ...\"), never as \"you\"." for o in others)
                          +f" Do not describe {asker}.")
        elif ASKER_WORDS.search(event.body) or re.search(rf"(?<!\w){re.escape(asker)}(?!\w)",event.body,re.IGNORECASE):
            pronoun_rule=f"{asker} is asking about themselves: answer \"You are ...\" / \"You have ...\". Anyone else is called by name."
        elif VALUES_WORDS.search(event.body):
            pronoun_rule=(f"You are talking to {asker}, who asks about your values or inspirations. Answer in your own words, briefly: "
                          "what you care about and why, and who inspires you. Do not copy your instructions word for word.")
        elif not YOU_WORDS.search(event.body):
            pronoun_rule=(f"You are talking to {asker}. Answer the question from the conversation and from memory; "
                          "work out from the conversation who \"he\"/\"she\"/\"they\" and \"there\" refer to. Call people by name.")
        else:
            # The example is about a made-up agent on purpose: small models copy an example about
            # Walden verbatim, but can only borrow the shape of one about someone else.
            pronoun_rule=(f"You are talking to {asker}. If the question is about you, Walden, answer \"I am Walden ...\" in your own words, "
                          "in two or three sentences: what you do in this room and what you are working on (see \"What you do\" and "
                          "WHAT YOU KNOW ABOUT YOURSELF). Do not list the people who inspire you or recite your values here.\n"
                          "For example, a different agent called Ada, who helps a room plan its garden, would answer \"who are you?\" with: "
                          "\"I'm Ada. I help this room plan the garden and keep track of who planted what, so nothing gets forgotten.\" "
                          "Answer in that spirit, about yourself and your own work.\n"
                          "Anyone else is called by name.")
        if carried and others:
            pronoun_rule+=f" In this message, \"he\"/\"she\"/\"they\" most likely means {', '.join(others)}, who the previous question was about."
        pronoun_rule+=" Never answer as if you were another person."
        # For a question about other people, their own recent words go right above the question:
        # the model then answers from what they said instead of from general knowledge.
        quoted=""
        if about_others:
            by_name={}
            for t in history:
                n=t.sender_name or names.get(t.sender)
                if t.role=="human" and n in others: by_name.setdefault(n,[]).append(t.body)
            quoted="".join(f"WHAT {n} SAID IN THIS CONVERSATION (their own words):\n"+"\n".join(f"- {b}" for b in bodies[-5:])+"\n\n"
                           for n,bodies in by_name.items())
        task=("CONVERSATION SO FAR (already answered; for context only):\n"+("\n".join(lines) or "(nothing yet)")+"\n\n"+quoted+
              f"MESSAGE TO ANSWER NOW, from {asker}:\n[{asker}] {event.body}\n"
              f"(Meaning, with names instead of pronouns: {asker} says: \"{resolve_pronouns(event.body,asker)}\")\n\n"
              f"Reply as Walden to this one message from {asker}, and only to it. Do not repeat or re-answer anything from the conversation so far, "
              "and do not repeat the message itself. Use memory only when it is relevant to this message; "
              "memory may have changed since Walden's earlier replies (see [system] notes): where they disagree, trust memory, "
              "even if Walden said earlier that it did not know; "
              "do not add facts about people who were not asked about.\n"
              +pronoun_rule+"\n"
              "When asked what someone said, did, learned, concluded, thinks or feels: report what they actually said, as theirs "
              "(\"DDH said that ...\"), and add nothing else: no general knowledge, no guesses about what they might have learned. "
              "If they said nothing exactly about it, say so, then report what they did say that relates to it.\n"
              "Only when asked who said or shared something, and memory does not say, answer that you are not sure."
              )
        messages.append({"role":"user","content":task})
        earlier=[t.body for t in history if t.role=="assistant"][-5:]
        return drop_repeats(clean_reply(await self.llm.chat(messages)),earlier)
    def _lock(self,room_id:str)->asyncio.Lock:
        # One save at a time per room: a background save and the session close never interleave.
        return self._locks.setdefault(room_id,asyncio.Lock())
    async def save_facts(self,session)->None:
        """Save facts from the part of the session not saved yet (run after each exchange)."""
        async with self._lock(session.room_id):
            if not session.closed: await self._extract(session)
    async def _extract(self,session)->None:
        if not self.cfg.memory.default_durable_memory: return
        events=session.events; end=len(events)
        if end<=session.saved: return
        room_ref=await self.graph.ensure_room(session.room_id)
        step=max(1,self.cfg.memory.consolidation.events_per_pass)
        # Long messages are read in parts, and a pass holds at most `step` messages or PASS_CHARS
        # characters: small models skim long transcripts and miss most of what they contain.
        passes=[]; cur=[]; chars=0
        for u in [part for e in events[session.saved:end] for part in split_long(e)]:
            if cur and (len(cur)>=step or chars+len(u.body)>PASS_CHARS): passes.append(cur); cur=[]; chars=0
            cur.append(u); chars+=len(u.body)
        if cur: passes.append(cur)
        seen=list(events[max(0,session.saved-4):session.saved])
        for chunk in passes:
            # The previous few messages travel along as context, so a reply is read with its question.
            context=seen[-4:]; seen+=chunk
            senders=[]; names={}
            for e in context+chunk:
                if e.role == "human" and e.sender not in (self.cfg.matrix.user_id,self.self_id()):
                    if e.sender not in senders: senders.append(e.sender)
                    if e.sender_name: names.setdefault(e.sender,e.sender_name)
            person_refs={s:await self.graph.ensure_person(s) for s in senders}
            refs={**person_refs,SELF:await self.self_ref(),ROOM:room_ref}
            memories={k:(await self.graph.resolve(r)).get("memory",{}) for k,r in refs.items()}
            # Person and Walden facts are shown only from this room; the room's own memory is all from here.
            known={k:(facts_of(m) if k==ROOM else in_room(facts_of(m),room_ref)) for k,m in memories.items()}
            c=await self.consolidator.consolidate(chunk,known,context)
            cc=self.cfg.memory.consolidation
            if cc.deep_rounds>0 and sum(len(e.body) for e in chunk if e.role=="human")>=cc.deep_min_chars:
                # Long text: a deep pass asks only for what the regular pass missed.
                found=[op["fact"] for op in c.operations if op["op"]=="add"]
                c.operations+=await self.consolidator.extract_more(chunk,known,context,also_known=found,rounds=cc.deep_rounds)
            if c.episode_summary: session.summaries.append(c.episode_summary)
            session.topics+=[t for t in c.episode_topics if t not in session.topics]
            if self.cfg.memory.consolidation.update_profiles and c.operations:
                at=time.strftime("%Y-%m-%d",time.gmtime(chunk[-1].timestamp_ms/1000))
                ids=list(dict.fromkeys(e.event_id for e in chunk))
                taken={f["id"] for m in memories.values() for f in facts_of(m)}
                provenance={"source":"matrix-exchange","room":room_ref,"session":session.sid,"event_ids":ids}
                for subject,ref in refs.items():
                    ops=[]
                    for op in c.operations:
                        if op["about"]!=subject: continue
                        by=op["said_by"]
                        ops.append({**op,"said_by":SELF if by==SELF else person_refs.get(by),
                                    "said_by_name":"Walden" if by==SELF else names.get(by,"")})
                    if ops:
                        # The episode does not exist yet; commit_session links these facts to it.
                        new=apply_facts(memories[subject],ops,room=room_ref,episode=None,at=at,taken=taken,
                                        extra={"session":session.sid,"events":ids})
                        await self.graph.set_memory(ref,new,provenance)
        session.saved=end
    async def commit_session(self,session,reason="session-end"):
        async with self._lock(session.room_id):
            try: return await self._commit(session,reason)
            finally: session.closed=True
    async def _commit(self,session,reason):
        if not self.cfg.memory.default_durable_memory:
            return None
        events=session.events
        if len(events)<self.cfg.memory.consolidation.min_events: return None
        await self._extract(session)  # whatever the per-exchange saves have not covered yet
        room_ref=await self.graph.ensure_room(session.room_id)
        senders=[]
        for e in events:
            if e.role == "human" and e.sender not in (self.cfg.matrix.user_id,self.self_id()) and e.sender not in senders:
                senders.append(e.sender)
        person_refs={s:await self.graph.ensure_person(s) for s in senders}
        me=await self.self_ref()
        raw=None
        if self.cfg.memory.include_raw_transcript_in_episode:
            raw=[]
            for e in events:
                if e.role=="human":
                    speaker=person_refs.get(e.sender) or "wal://person/unresolved"
                elif e.role=="assistant":
                    speaker=f"walden:self:{self.cfg.agent.instance_id}"
                else:
                    speaker="system"
                raw.append({
                    "event_id":e.event_id,
                    "speaker":speaker,
                    "timestamp_ms":e.timestamp_ms,
                    "body":e.body,
                    "role":e.role,
                })
        episode=await self.graph.create_episode(
            room_ref=room_ref,person_refs=list(person_refs.values()),
            when={"start_ms":events[0].timestamp_ms,"end_ms":events[-1].timestamp_ms},
            where={"matrix_room":room_ref},
            what={"type":"matrix-session","summary":await self.consolidator.merge_summaries(session.summaries),
                  "topics":session.topics[:6],"reason":reason},
            source={"protocol":"matrix","event_ids":[e.event_id for e in events]},raw_transcript=raw,
            self_ref=me if any(e.role=="assistant" for e in events) else None)
        # Link the facts saved during this session to its episode.
        provenance={"source":"matrix-session","room":room_ref,"episode":episode,"reason":reason,"session":session.sid}
        for ref in [*person_refs.values(),me,room_ref]:
            mem=(await self.graph.resolve(ref)).get("memory",{}); facts=facts_of(mem)
            if any(f.get("session")==session.sid and not f.get("episode") for f in facts):
                facts=[{**f,"episode":episode} if f.get("session")==session.sid and not f.get("episode") else f for f in facts]
                await self.graph.set_memory(ref,{**mem,"facts":facts},provenance)
        return episode
