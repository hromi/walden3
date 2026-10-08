from __future__ import annotations
from copy import deepcopy
from typing import Any

def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a consolidation patch into memory. None deletes a key; dicts merge recursively;
    lists gain the new items (memory accumulates, a patch never drops what is known);
    empty values ("", [], {}) are ignored, because extraction models emit them for "nothing new";
    other scalars replace."""
    out=deepcopy(base)
    for k,v in patch.items():
        if v is None:
            out.pop(k, None)
        elif v in ("", [], {}):
            continue
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k]=deep_merge(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k]=deepcopy(out[k])+[deepcopy(x) for x in v if x not in out[k]]
        else:
            out[k]=deepcopy(v)
    return out
