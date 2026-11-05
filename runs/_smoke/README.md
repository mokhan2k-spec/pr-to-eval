# Pre-gate smoke run (not part of the results)

`boltons-458/claude/trial-1/attempt-1` was run on 2026-11-05 22:13 UTC to check the enforced agent command end to end. It passed (PASS, 212/212).

It came before the R18 probe gate. Its meta.json has no probe `_binding` (CLI version + policy digest + probe digest), so `report --check` cannot verify it against the current policy. It is kept here as evidence and is NOT counted. All 30 scored trials were run fresh after the gate landed.
