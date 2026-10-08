from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


def _secret(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("env:"):
        return os.getenv(value[4:], "")
    if isinstance(value, dict):
        return {k: _secret(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_secret(v) for v in value]
    return value

class AgentConfig(BaseModel):
    name: str = "Walden"
    instance_id: str = "walden"
    system_prompt: str = "You are Walden."

class PathsConfig(BaseModel):
    data_dir: str = "./data"
    cache_dir: str = "./data/cache"

class LoggingConfig(BaseModel):
    level: str = "INFO"

class MatrixSessionConfig(BaseModel):
    idle_seconds: int = 1800
    max_events: int = 80
    max_chars: int = 60000
    auto_commit: bool = True
    include_assistant_turns: bool = True
    prompt_recent_events: int = 30
    prompt_recent_chars: int = 12000
    save_every: int = 5  # save facts after each Walden reply, or after this many messages without one; 0 = only at session end

class MatrixConfig(BaseModel):
    enabled: bool = False
    display_name: str = ""     # set on the Matrix account at startup if it differs (empty = leave as is)
    homeserver: str = ""
    user_id: str = ""
    access_token: str = ""
    password: str = ""
    device_id: str = ""
    store_path: str = "./data/matrix-store"
    encryption: bool = True
    ignore_unverified_devices: bool = False
    verification_users: list[str] = Field(default_factory=list)  # empty = only the bot's own account
    auto_join_invites: bool = True
    allowed_rooms: list[str] = Field(default_factory=list)
    allowed_senders: list[str] = Field(default_factory=list)
    reply_mode: Literal["all", "mentions", "commands"] = "all"
    command_prefix: str = "!walden"
    send_as_notice: bool = False
    sync_timeout_ms: int = 30000
    session: MatrixSessionConfig = Field(default_factory=MatrixSessionConfig)

class LLMConfig(BaseModel):
    provider: Literal["openai_compatible", "echo"] = "echo"
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = ""
    model: str = ""
    temperature: float = 0.4
    max_tokens: int = 900
    timeout_seconds: int = 120
    json_mode: bool = True

class WalrusCliConfig(BaseModel):
    binary: str = "walrus"
    client_config: str = ""
    wallet: str = ""
    gas_budget: int | None = None

class WalrusHttpConfig(BaseModel):
    publisher_url: str = ""
    aggregator_url: str = ""
    bearer_token: str = ""
    timeout_seconds: int = 90

class WalrusConfig(BaseModel):
    backend: Literal["local", "cli", "http"] = "local"
    epochs: int = 20
    local_dir: str = "./data/blobs"
    cli: WalrusCliConfig = Field(default_factory=WalrusCliConfig)
    http: WalrusHttpConfig = Field(default_factory=WalrusHttpConfig)

class SuiCliConfig(BaseModel):
    binary: str = "sui"
    package_id: str = ""
    module: str = "memory_graph"
    gas_budget: int = 50_000_000

class SuiConfig(BaseModel):
    backend: Literal["local", "cli"] = "local"
    local_db: str = "./data/walden.sqlite3"
    cli: SuiCliConfig = Field(default_factory=SuiCliConfig)

class SemanticConfig(BaseModel):
    enabled: bool = False
    backend: Literal["memwal", "none"] = "memwal"
    best_effort: bool = True
    key: str = ""
    account_id: str = ""
    env: str = "prod"
    server_url: str = ""
    recall_limit: int = 8
    max_distance: float = 0.75

class ConsolidationConfig(BaseModel):
    enabled: bool = True
    min_events: int = 1
    update_profiles: bool = True
    events_per_pass: int = 20  # long sessions are read in chunks; small models skim long transcripts
    deep_rounds: int = 2       # deep pass for long texts: rounds of "which facts are still missing?" (0 = off)
    deep_min_chars: int = 600  # run the deep pass when a pass holds at least this much text from people

class CheckpointConfig(BaseModel):
    enabled: bool = True
    # memwal: Walrus Memory (MemWal) relayer; every checkpoint is one memory of the agent's account.
    # walrus: plain Walrus blob written with the walrus CLI (public header, Sui tags, self-managed lifetime).
    backend: Literal["memwal","walrus"] = "memwal"
    auto_save: bool = True        # memwal: mirror a room's memory to Walrus Memory whenever a session ends (if it changed)
    memwal_key: str = ""          # delegate private key (hex), e.g. env:MEMWAL_PRIVATE_KEY
    memwal_account_id: str = ""   # Walrus Memory account object ID, e.g. env:MEMWAL_ACCOUNT_ID
    memwal_env: str = "prod"      # relayer preset: prod (mainnet) | staging (testnet)
    memwal_server_url: str = ""   # explicit relayer URL; overrides memwal_env
    memwal_namespace: str = "walden"  # namespaces are "<this>-<room>"; one per room
    memwal_blob_url: str = "https://walruscan.com/mainnet/blob/{blob_id}"
    memwal_account_url: str = "https://suiscan.xyz/mainnet/object/{account_id}"
    walrus_binary: str = "walrus"
    walrus_config: str = ""       # empty = the Walrus CLI's default config
    context: str = "testnet"      # Walrus network; mainnet only if set explicitly
    sui_binary: str = "sui"
    sui_env: str = "testnet"      # used only to show the wallet balance
    epochs: int = 5
    deploy_users: list[str] = Field(default_factory=list)  # empty = room moderators (power level >= 50)
    confirm_timeout_seconds: int = 300
    set_attributes: bool = True   # tag the blob's Sui object with walden.* attributes
    explorer_blob_url: str = "https://walruscan.com/testnet/blob/{blob_id}"
    aggregator_url: str = "https://aggregator.walrus-testnet.walrus.space/v1/blobs/{blob_id}"
    explorer_object_url: str = "https://suiscan.xyz/testnet/object/{object_id}"

class ArchiveConfig(BaseModel):
    enabled: bool = True
    require_moderator: bool = False  # True: only moderators (here and in the archived room) may archive
    notice_hours: float = 24       # people in the room can opt out ("!walden exclude me") during this time
    walrus_context: str = "mainnet"  # where files go (plain Walrus blobs, paid in WAL by the walrus CLI wallet)
    sui_env: str = "mainnet"       # for showing that wallet's balance
    epochs: int = 53               # how long files are kept (mainnet: 14 days per epoch)
    max_file_mb: float = 50        # larger files are listed but not stored
    episode_max_messages: int = 80
    episode_max_chars: int = 12000 # one episode incl. transcript is one Walrus Memory memory; the relayer fails on very big ones
    episode_gap_hours: float = 6   # a longer pause starts a new episode

class MemoryConfig(BaseModel):
    identity_hmac_key: str = ""
    encryption_key: str = ""
    encrypt_blobs: bool = True
    namespace_prefix: str = "walden"
    cache_reads: bool = True
    cache_max_items: int = 512
    default_durable_memory: bool = True
    include_raw_transcript_in_episode: bool = False
    max_lineage_depth: int = 1000
    walrus: WalrusConfig = Field(default_factory=WalrusConfig)
    sui: SuiConfig = Field(default_factory=SuiConfig)
    semantic: SemanticConfig = Field(default_factory=SemanticConfig)
    consolidation: ConsolidationConfig = Field(default_factory=ConsolidationConfig)
    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)

class RecallConfig(BaseModel):
    enabled: bool = True
    max_person_lineage_items: int = 3
    max_room_lineage_items: int = 3
    max_semantic_items: int = 6
    max_episodes: int = 5  # most recent episodes of the current room put into every prompt
    max_context_chars: int = 24000  # memory in the prompt; over this, the lines most relevant to the question are kept

class ApiConfig(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    bearer_token: str = ""

class Config(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    recall: RecallConfig = Field(default_factory=RecallConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)


def load_config(path: str | Path = "config.yaml") -> Config:
    p = Path(path).expanduser().resolve()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(_secret(raw))
    # Resolve relative paths against config.yaml, not current working directory.
    base = p.parent
    for obj, attr in [
        (cfg.paths, "data_dir"), (cfg.paths, "cache_dir"),
        (cfg.matrix, "store_path"), (cfg.memory.walrus, "local_dir"),
        (cfg.memory.sui, "local_db")
    ]:
        value = getattr(obj, attr)
        q = Path(value).expanduser()
        if not q.is_absolute():
            setattr(obj, attr, str((base / q).resolve()))
    if cfg.memory.walrus.cli.client_config:
        q = Path(cfg.memory.walrus.cli.client_config).expanduser()
        if not q.is_absolute():
            cfg.memory.walrus.cli.client_config = str((base / q).resolve())
    Path(cfg.paths.data_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.paths.cache_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.matrix.store_path).mkdir(parents=True, exist_ok=True)
    Path(cfg.memory.walrus.local_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.memory.sui.local_db).parent.mkdir(parents=True, exist_ok=True)
    return cfg
