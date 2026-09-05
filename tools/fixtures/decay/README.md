# DECAY fixture contract

This directory contains synthetic Atlas-v2 Loop 3 queue data for the Fully
Aware assembler tests. The shape is derived from the installed
`scripts/decay_review.py` producer: a queue is named
`DECAY-YYYY-MM-DD.md`, repeated runs use a numeric suffix (`-2`, `-3`, ...),
and each `###` block carries bold `STILL TRUE`, `NEEDS UPDATE`, and `DEFER`
checkboxes.

The installed producer is pinned in `producer-metadata.json` by SHA-256:
`a169a8b6c0ed07ce5d0dbd5e555b5470644dc783fec743eda94bf1e229f1795f`.
The fixture is content-free with respect to the real memory corpus.
