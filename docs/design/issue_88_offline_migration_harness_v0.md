# Issue #88 — Offline backup/restore and shared-claim preparation v0

**Status:** experimental, **clone-only**. Do not run on a production WorkStore or treat its output as an enabled migration. Extends the design-only cutover contract merged in PR #92.

This stage establishes three narrower properties before a production migration is even considered: an SQLite-consistent backup (including committed WAL state), an independently restored clone whose Gen2 Work chronology remains parseable, and a **fixture-only** atomic provider-result claim ledger shared by simulated normal live and recovery paths.

## Workflow

1. Supply an existing local Gen2 \`SqliteWorkStore\` file and an output **directory that does not exist**. Never specify the source directory as the output. The caller is responsible for permissions, confidentiality, and a suitable offline/disposable environment.
2. Verify Gen2 table shape, SQLite integrity, and absence of previous Issue #88 preparation objects. Open the source in SQLite \`mode=ro\`. Take a SQLite online backup into \`backup.sqlite3\`, not a naive file copy that misses WAL.
3. Compare the source and backup Work/event-row digests and counts, and project all backed-up Work/event chronologies through the **real** \`SqliteWorkStore\`. A concurrent source mutation detected by the validation rejects preparation.
4. Independently restore \`backup.sqlite3\` via SQLite backup into \`prepared.sqlite3\`, verify Work/event equivalence, then create \`issue88_offline_meta\` and \`issue88_offline_claim\` in that **clone only**. \`meta\` is always schema v1, \`PREPARED_DISABLED\`.
5. Recheck preservation of Work/event data and write a \`manifest.json\` **last**. An interruption before the manifest leaves an incomplete quarantined output directory, never a ready migration. \`inspect_prepared()\` rejects missing or inconsistent manifests and rechecks the backup and clone.
6. Optionally, on the **disposable prepared clone only**, run \`claim_fixture()\` with \`path="live"\` or \`path="recovery"\` to test that the same provider result is claimed exactly once across both simulated paths. Both paths call one \`BEGIN IMMEDIATE\` uniqueness boundary, exclude adapter version from the canonical provider result key and forbid claiming the same result for a different Work/request/handoff or digest.

None of these steps modifies the original SQLite schema, installs a trigger, writes a live CognitionOutcome, dispatches a model, admits a RoleRun outcome, or enables recovery.

## Safety and evidence limitations

- A successful backup/restore is a **consistent point-in-time snapshot**, not proof that the live source will stay unchanged afterwards. Concurrent writers may resume immediately; no writer epoch has been installed, and no production cutover is authorized.
- \`issue88_offline_meta\` does **not** certify provider-history coverage, provider receipt authenticity, human approval, or migration readiness. It cannot represent \`RECOVERY_ENABLED\`.
- \`issue88_offline_claim\` is a **claim-contract fixture**, not a provider-authenticated result index. It does not append a \`role.completed\` event. Claims are sticky, and a matching replay returns \`ALREADY_CLAIMED\`; this does not mean an outcome was actually admitted.
- A partially created output directory is intentionally **not auto-deleted**, so backup/corruption evidence is not destroyed. Its absence of a valid manifest means it must not be used as a prepared clone. The caller chooses the retention/deletion policy for potentially sensitive backups.
- Backup copies are sensitive user data; do not commit these SQLite files to Git, transmit them, or point any test at the DW3 database. This project PR adds only source code, tests and documentation.
- Invalid Gen2 schema, unparsable Work/event digests, missing core tables, existing \`issue88_offline_\` objects and unexpected SQLite corruption fail closed.
- A restored database can become *stale* or be cloned onto a second installation. The uniqueness index in a clone does not coordinate multiple installations or act as a shared external ownership lease.
- The code is a testing/import-only harness, **not** an executable CLI for migrating real installations.

## Reproduce on a disposable test database

\`\`\`powershell
python -m ruff check tools/issue88_offline_migration_harness.py tests/test_issue88_offline_migration_harness.py
python -m ruff format --check tools/issue88_offline_migration_harness.py tests/test_issue88_offline_migration_harness.py
python -m pytest tests/test_issue88_offline_migration_harness.py -q
\`\`\`

The tests create fresh \`tmp_path\` SQLite files with the real \`SqliteWorkStore\`. They cover WAL-backed source snapshots, native Work/event projection, version/state inspection, injected partial failures, destination refusal, index collision across live/recovered paths, adapter-version-independent identity, service isolation, restart persistence, and atomic claim conflicts. They do **not** prove a production migration or real provider receipts.

## Required next gates

- An **explicit source-writer drain and storage-side epoch fence**, separately reviewed against legacy processes, \`append_with_child_create()\` and raw SQLite inserts.
- Authenticated, stable provider-result identities on **ordinary live callbacks**, not just recovered observations.
- Full historical backfill/exclusion proof, including restore/replay across generations; fail-closed until established.
- A crash-safe, strictly CAS-bound **real** \`RoleAdmission\` outcome protocol with proof event ordering, replay and D19 revocation races.
- A separately authorized rollout/rollback procedure for non-disposable databases. Never infer enablement solely from \`manifest.json\` or the fixture claim ledger.
