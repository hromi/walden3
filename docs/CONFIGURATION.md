# Configuration

`config.yaml` is the only configuration document read by Walden. Relative paths resolve against the directory containing that file.

A string `env:NAME` is replaced with environment variable `NAME`. This keeps one YAML configuration surface while allowing secrets to stay outside version control.

Important switches:

- `matrix.enabled`: documents whether this deployment is intended to run the Matrix service; start it with `walden matrix`.
- `matrix.reply_mode`: `all`, `mentions`, or `commands`. `mentions` replies only to explicit mentions: a Matrix mention (pill), the bot's full user ID, or `@` followed by its username or display name. The bare word "walden" does not trigger a reply.
- `matrix.session.auto_commit`: when true, idle/size session boundaries commit durable memories automatically. When false, automatic boundaries discard the buffered session; `!walden close` still forces a commit.
- `matrix.session.include_assistant_turns`: include Walden's own replies in episodic transcripts/summaries. Assistant turns never become interpersonal entities.
- `llm.provider`: `openai_compatible` or `echo`.
- `memory.walrus.backend`: `local`, `cli`, or `http`.
- `memory.sui.backend`: `local` or `cli`.
- `memory.semantic.enabled`: optional MemWal semantic mirror.
- `memory.include_raw_transcript_in_episode`: defaults false; summaries/provenance are safer and cheaper.
- `memory.default_durable_memory`: master switch for session-derived durable memory. If false, session commits are no-ops.

## Required production secrets

- `memory.identity_hmac_key`: server-only HMAC key used to pseudonymize Matrix IDs.
- `memory.encryption_key`: server-only memory encryption key; arbitrary text is SHA-256-derived to 32 bytes, while 32-byte hex/base64 keys are accepted directly.
- Matrix access token or password.
- LLM API key when the selected endpoint requires one.
- MemWal delegate key/account when semantic search is enabled.

Back these up securely. Losing the memory encryption key makes historical Walrus blobs unreadable. Rotating the identity HMAC key without a migration plan makes existing private Matrix-to-Sui bindings unreachable by new events.
