from __future__ import annotations
from .config import Config
from .adapters.local import LocalBlobStore, SQLiteRegistry, NullSemanticIndex
from .adapters.bindings import SQLiteBindings, SQLiteEpisodeIndex
from .adapters.walrus import WalrusCliBlobStore, WalrusHttpBlobStore
from .adapters.sui import SuiCliRegistry
from .adapters.memwal import MemWalSemanticIndex, BestEffortSemanticIndex
from .memory_graph import MemoryGraph
from .llm import make_llm
from .agent import WaldenAgent

def build(cfg:Config):
    wc=cfg.memory.walrus
    if wc.backend=="local": blobs=LocalBlobStore(wc.local_dir)
    elif wc.backend=="cli": blobs=WalrusCliBlobStore(wc.cli.binary,wc.cli.client_config,wc.cli.wallet,wc.cli.gas_budget,wc.epochs)
    else: blobs=WalrusHttpBlobStore(wc.http.publisher_url,wc.http.aggregator_url,wc.epochs,wc.http.bearer_token,wc.http.timeout_seconds)
    sc=cfg.memory.sui
    if sc.backend=="local": registry=SQLiteRegistry(sc.local_db); bindings=registry
    else: registry=SuiCliRegistry(sc.cli.binary,sc.cli.package_id,sc.cli.module,sc.cli.gas_budget); bindings=SQLiteBindings(sc.local_db)
    semc=cfg.memory.semantic
    if semc.enabled and semc.backend=="memwal":
        sem=MemWalSemanticIndex(semc.key,semc.account_id,semc.env,semc.server_url,semc.max_distance)
        if semc.best_effort: sem=BestEffortSemanticIndex(sem)
    else: sem=NullSemanticIndex()
    graph=MemoryGraph(cfg,blobs,registry,bindings,sem,SQLiteEpisodeIndex(sc.local_db))
    llm=make_llm(cfg.llm); agent=WaldenAgent(cfg,graph,llm)
    return agent
