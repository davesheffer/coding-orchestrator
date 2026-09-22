# Previous-release fixtures

These are the pre-hardening configurations from the locally retained snapshot
of upstream commit `acf910d36a0d70c6ecd360a556994f76e323c279`.
Codex fixtures retain the obsolete feature keys and read-only runner that blocked
upgrades. Claude fixtures restore the upstream single trailing newline (the
local reconstruction added an extra blank line). The installer recognizes both
byte variants, while still refusing unknown local edits without `--force`.

Keep these static: generating fixtures from current roles would hide migration
regressions. They are test inputs, not recommended/current configurations.
