# Shared briefing in the IRIS coordinator

`coordinator_consumer.coordinator_observe(configuration, *, initialize, installation)`
is the read-only shared-briefing entrypoint for the host-pinned IRIS role runner.
It builds the existing situation brief, then invokes the native selected-reader
probe and returns its unchanged proof. Neither observation grants control or
source checkpoint authority; the service coordinator checks those separately.

The closed configuration is:

```json
{
  "schema": "fully-aware-coordinator-consumer/v1",
  "probe_release": {"path": "/private/native-release.json", "sha256": "<64 hex>"},
  "consumer_config": "/private/shared-reader.json",
  "brief": {
    "boot_pack": "/private/boot-pack.json",
    "plans": "/private/plans-snapshot.json",
    "sweep_automation": null
  }
}
```

Installation is supplied separately as `{path, sha256}` by the trusted host
invocation, avoiding a circular installation/configuration pin. All paths must
be absolute physical paths. The existing native helper validates private file
ownership, permissions, hashes and retained child loading. The role runner pins
the adapter, its complete module closure, interpreter and configuration. The
closure includes `situation_brief`, `work_view`, `priority_context`, `sweep_attempt`,
`sweep_clock`, `sweep_route`, and the native `generation_probe_entry` helper.
Do not add an ambient repository to the child import path.

Active reads require the actual brief and native probe to agree on the complete
typed selection reference. Initialization requires the brief to advertise
unavailable selected state, followed by the native loader's precise missing,
pending-first-command or disabled boundary. A generic fallback cannot substitute
for that observation. Native timeouts are reported as unavailable, without child
arguments or private paths. The coordinator owns final proof shape and expiry
checks. `read_current` also returns the brief beside the proof for readback.

This entrypoint is opt-in source integration. Adding it does not install the
coordinator, retarget the current reader, schedule a pass or prove live delivery.
The existing launcher and installed reader remain unchanged until coordinated
release. IRIS owns morning-routine and board adoption.

Run focused checks from this directory with
`python3 -B -m unittest test_coordinator_consumer test_situation_brief`.
Interface tests use the real brief builder with fictional HTTP responses and a
module-shaped native helper. Retained-child/service integration is verified
separately against the matching vault coordinator candidate.
