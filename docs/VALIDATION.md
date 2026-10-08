# Validation status

The local implementation is executable without Sui/Walrus/Matrix credentials. The release test suite currently checks:

- typed `wal://` URI parsing;
- sparse patch deep merge;
- stable person/room identity;
- immutable revision history and exact backward lineage;
- episode participant and room historical snapshots;
- assistant turns present in episodes but excluded from interpersonal identity;
- Matrix callback buffering of a human turn and Walden's reply as one session interaction.

The release was also compiled with `python -m compileall`, built into a Python wheel with setuptools, and imported successfully from the built wheel.

Network adapters are deliberately isolated and cannot be authenticated end-to-end in the build environment. Validate them in the target deployment with the actual wallet, homeserver and CLI versions:

```bash
walrus --version
sui --version
sui move build --path move/walden_memory
walden -c config.yaml self-test
```

Then enable one network backend at a time: Sui first, Walrus exact storage second, Matrix third, and optional MemWal last. For Matrix E2EE, keep the nio store persistent and verify the bot device from a trusted Matrix client before using sensitive rooms.
