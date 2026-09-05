# Convergence recovery

The repaired code restores proposal processing and makes failures visible. Runtime cutover and replay are separate operator steps. A merged branch is not evidence that the installed runtime changed.

| System | Active implementation | Authority |
|---|---|---|
| Imprint | [Imprint](https://github.com/anthony-ai-systems/imprint), installed operator runtime | Captured human judgment; derived candidates require existing review |
| Atlas | [Vault scripts](https://github.com/anthony-ai-systems/anthony-wiki-vault/tree/main/scripts) | Claim freshness and adjudication; archived [v1](https://github.com/anthony-ai-systems/atlas) is historical |
| IRIS | [Vault IRIS](https://github.com/anthony-ai-systems/anthony-wiki-vault/tree/main/IRIS), existing control-center Codex pulse | Operational reconciliation; [iris-ios](https://github.com/anthony-ai-systems/iris-ios) is roadmap only |
| Fully Aware | This repository and its scheduled main checkout | Read-only aggregation and advisory context |

The September 5 audit found installed Imprint capture files aligned with the compared source files, but did not establish a final budget/continue ruling. Do not change the observed operator budget based on old notes. The archived Atlas repository remains archived; v2 does not depend on another v1 GO decision.

## Controlled recovery

1. Review and merge the source changes through their PRs. Pair automated caller markers with the updated installed Imprint capture gate. A marker alone does not fix an older gate. No existing canon is relabeled or deleted.
2. Prepare the taste LaunchAgent with tools/taste-distiller/prepare_launch_agent.py. Review exact Python, worker, Claude and PATH values and executable identity (version and hash). This command only renders a plist. The template contains placeholders and must not be installed directly. The armer renders it before making changes, but also edits hooks and starts work; run it only in the authorized deployment window.
3. Preserve the live plist and exact source/runtime hashes before installing. Keep the prepared scoped patch and backup. The audit session holds prepared artifacts and receipts; deployed hashes remain null until the operator installs and verifies them. An unrelated dirty vault is not a reason for blind pull, stash, reset or cleanup.
4. Deploy the already merged Atlas daily queue cap before the dual-feed change. Follow the vault scripts/ATLAS-CONVERGENCE-DEPLOYMENT.md sequence. Existing queues stay intact. Check the next newly dated output and overflow quarantine.
5. After taste cutover, confirm the private macroseat/distill-health.json schema, timestamp, errors and retry-exhausted counts in the next digest. A healthy exit with unresolved backlog remains degraded. Missing/malformed or older-than-45-minute health is degraded. No transcript content or session identifiers enter this health file.
6. Prepare failed-only replay with taste_distiller.py --prepare-failed-replay /absolute/private/replay.json. It reads ledger/queue without changing them, writes a new owner-only manifest, excludes successful and transcript-missing records, and caps batches at six. It never invokes a model. Review the private manifest and current ledger again before authorizing each batch; do not raise retry limits or reprocess successes. The existing forced-session mechanism remains a manual action and can bypass safety gates, so use only the exact approved failed sessions.
7. Keep the IRIS aggregate-health contract at design/fixture scope until its project binding and intended data scope are approved. Reuse the existing manager, pulse, surface and plans infrastructure.

Rollback restores the exact prior executable/runtime artifacts and plist together, then verifies their hashes. Preserve ledger, queues and operator canon in place. Do not replay as part of rollback. Historical reports and previous deployment receipts remain evidence.

The digest prints absolute observation timestamps. The SessionStart hook still checks current artifact age and warns after 36 hours; regeneration-time labels cannot substitute for that consumer check.
