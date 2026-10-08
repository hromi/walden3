from __future__ import annotations
from dataclasses import dataclass
from urllib.parse import urlparse

KINDS = {"person", "room", "episode", "blob"}

@dataclass(frozen=True)
class WalURI:
    kind: str
    ident: str
    def __str__(self) -> str:
        return f"wal://{self.kind}/{self.ident}"


def parse(uri: str) -> WalURI:
    p = urlparse(uri)
    if p.scheme != "wal":
        raise ValueError(f"not a wal URI: {uri}")
    kind = p.netloc
    ident = p.path.lstrip("/")
    if kind not in KINDS or not ident:
        raise ValueError(f"invalid wal URI: {uri}")
    return WalURI(kind, ident)

def person(object_id: str) -> str: return str(WalURI("person", object_id))
def room(object_id: str) -> str: return str(WalURI("room", object_id))
def episode(blob_id: str) -> str: return str(WalURI("episode", blob_id))
def blob(blob_id: str) -> str: return str(WalURI("blob", blob_id))
