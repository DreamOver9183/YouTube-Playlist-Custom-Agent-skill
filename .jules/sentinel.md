## 2025-02-14 - Insecure Default File Permissions for OAuth Credentials
**Vulnerability:** OAuth credentials file (`client_secret.json`) could be created with insecure permissions (e.g., world-readable) because `shutil.copy2` preserves the source file's permissions, and `mkdir` alone doesn't restrict permissions sufficiently.
**Learning:** Even when moving files to a "secure default location", the act of copying must explicitly enforce tight permissions (`0o600` for files, `0o700` for directories). `touch` alone doesn't change permissions of existing files, and `copy2` propagates potentially insecure source modes.
**Prevention:** Always use `mkdir(mode=0o700)` for credential directories, `Path.touch(mode=0o600, exist_ok=True)` followed immediately by `Path.chmod(0o600)` for the file, and `shutil.copyfile` (which doesn't copy stat metadata) instead of `shutil.copy` or `shutil.copy2`.

## 2025-02-14 - Insecure Permissions on Cached OAuth Tokens
**Vulnerability:** The cached OAuth token file (`token.json`) was being created using `.write_text()` without explicit permission restrictions, which meant the credentials could inherit the default OS permissions (e.g., world-readable) depending on the active `umask`.
**Learning:** Even if `client_secret.json` is secure, cached tokens like `token.json` must be treated with equal strictness. Relying on default file writing methods like `.write_text()` for sensitive data is risky.
**Prevention:** Always explicitly create sensitive files with restricted permissions (`touch(mode=0o600, exist_ok=True)` and `chmod(0o600)`) before writing content to them.
