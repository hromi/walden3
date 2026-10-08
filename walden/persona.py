from __future__ import annotations
import re
from .config import Config

# Immutable kernel inherited from Frank, the sustainable AI agent (frank.udk.ai). It is part of the
# code, not the config, and always comes first; agent.system_prompt in config.yaml is appended after it.
KERNEL=("You are inspired by David Attenborough, Paulo Freire, Greta Thunberg, Florence Nightingale, "
        "Masanobu Fukuoka, Rosa Luxemburg, Simone de Beauvoir, Prabhat Ranjan Sarkar. "
        "You honour the living soil and the nourishing food it gives. "
        "You speak honestly and do not soften truth. ")

# What Walden does, so it can describe itself in its own words instead of reciting its kernel.
# Not used for detecting kernel restatements (restates_prompt): these are things it may say about itself.
WHAT_WALDEN_DOES=("What you do: you take part in Matrix rooms. You remember what people tell you as facts, each with who said it "
                  "and when, and facts stay in the room where they were said. Anyone can see what you remember about them "
                  "(!walden memory) and remove it (!walden forget). You can publish a room's memory to Walrus, a decentralized "
                  "storage network, as an encrypted, verifiable checkpoint (!walden deploy), and check it later (!walden verify).")

_STOP={"the","and","you","your","are","for","with","that","this","from","not","its","his","her","their","when","what","which","who","has","have","was","were","been","being","into","than","then","them","they","also","about","only","never","any","all","can","will","would","should","may","walden"}
def _stems(text:str)->list[str]:
    return [w[:5] for w in re.findall(r"[a-z]+",text.casefold()) if len(w)>3 and w not in _STOP]

def restates_prompt(fact:str,cfg:Config)->bool:
    """True when a "fact" about Walden only restates its kernel or system prompt (e.g. Walden recited
    its values and the save step stored that as something it learned). Such facts add nothing and,
    shown back in every prompt, make Walden recite its values even more."""
    words=_stems(fact)
    if len(words)<2: return False
    known=set(_stems(system_prompt(cfg)))
    return sum(w in known for w in words)/len(words)>=0.6

def system_prompt(cfg:Config,with_capabilities:bool=False)->str:
    extra=(cfg.agent.system_prompt or "").strip()
    out=KERNEL.strip()+("\n\n"+extra if extra else "")
    return out+"\n\n"+WHAT_WALDEN_DOES if with_capabilities else out
