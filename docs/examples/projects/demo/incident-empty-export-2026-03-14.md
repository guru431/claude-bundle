---
sources:
  - path: daily/2026-03-14.md
    processed: 2026-03-15T02:31:04
updated: 2026-03-15
---
# Empty nightly export

**Symptom.** The nightly export produced a zero-byte file in roughly one run out of five, and the downstream integrity check passed anyway.

**Cause.** The writer opened the destination on a network share BEFORE checking the share was mounted. On a slow mount the open created an empty file, and the integrity check compared that file against itself.

**Fix.** The mount check moved above the open, and the exporter now writes a `.part` file that is renamed only after the row count matches. Verified across twenty consecutive runs.

See also [[index]] and the day it was found, [[daily/2026-03-14]].
