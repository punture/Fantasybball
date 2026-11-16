# Security policy

## Credentials
FBM stores Yahoo OAuth credentials (client id/secret, refresh token) in your OS credential store via
[`keyring`](https://pypi.org/project/keyring/) (Windows Credential Manager, macOS Keychain, Secret Service on Linux).
They are never written to tracked files. On a machine without an OS keyring (e.g. headless Linux) FBM keeps them in
`data/secrets/credentials.json` instead: gitignored, folder 0700 and file 0600 on POSIX, excluded from `fbm backup`.
Once that file exists FBM keeps using it, so scheduled jobs and terminals share one store
(`FBM_CREDENTIALS_BACKEND=keyring|file` forces one store).

## Never commit
- Secrets of any kind (`.env`, tokens, keys).
- Personal league state: `memory/`, `reports/`, `data/`, `config/settings.toml`, `config/league.toml`,
  `config/constants.toml`, `config/yahoo_id_overrides.toml`. These are gitignored; templates ship as
  `templates/memory/` and `config/*.example.toml`.

Run `python scripts/prepublish_check.py` before pushing; CI runs it too.

## Reporting a vulnerability
Please report privately via GitHub: **Security → Advisories → Report a vulnerability** on
https://github.com/quinjan/basketball-fantasy-manager. Do not open a public issue for security problems.
