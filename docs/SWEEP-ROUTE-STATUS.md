# Scheduled route observation

The current-situation reader accepts optional `--sweep-automation /absolute/path/to/automation.toml`.
It reports the existing IRIS heartbeat's configured active/paused state and whether
its owner and recurrence still match the reviewed route. Missing local storage,
invalid input and unexpected route identity remain visibly unavailable or mismatched.
The reader does not infer deletion when a local file is missing.

Only fixed status fields are emitted. Prompt, display name, arbitrary owner and
recurrence strings, file path and exception text are excluded. Reads are bounded,
reject symlinks and nonregular files, and detect file replacement during observation.

An active configuration is not proof that the scheduler or model can execute.
This field does not clear a platform rejection, establish current source coverage,
or supersede the last useful sweep and latest recorded attempt. Those observations
keep their original timestamps. No automation API, state write, retry, route
transfer, notification or execution control is added.

The reviewed owner remains the original IRIS task. A future supported owner change
requires explicit release coordination; a mismatch must never silently accept a
new route. Existing invocations without this optional input keep their prior output.
