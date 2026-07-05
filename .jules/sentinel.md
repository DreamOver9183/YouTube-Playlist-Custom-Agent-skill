## 2025-02-14 - Insecure Default File Permissions for OAuth Credentials
**Vulnerability:** OAuth credentials file (`client_secret.json`) could be created with insecure permissions (e.g., world-readable) because `shutil.copy2` preserves the source file's permissions, and `mkdir` alone doesn't restrict permissions sufficiently.
**Learning:** Even when moving files to a "secure default location", the act of copying must explicitly enforce tight permissions (`0o600` for files, `0o700` for directories). `touch` alone doesn't change permissions of existing files, and `copy2` propagates potentially insecure source modes.
**Prevention:** Always use `mkdir(mode=0o700)` for credential directories, `Path.touch(mode=0o600, exist_ok=True)` followed immediately by `Path.chmod(0o600)` for the file, and `shutil.copyfile` (which doesn't copy stat metadata) instead of `shutil.copy` or `shutil.copy2`.

## 2026-07-05 - Insecure Default File Permissions for OAuth Token
**Vulnerability:** OAuth token file (`token.json`) could be created with insecure permissions (e.g., world-readable) because `Path.write_text` writes files with default umask permissions, and `Path.mkdir` alone without a mode argument doesn't restrict permissions sufficiently.
**Learning:** Just like copying credentials, generating/writing sensitive tokens directly using `Path.write_text` requires explicit permission restriction to prevent unauthorized access.
**Prevention:** Always use `mkdir(mode=0o700)` for credential/token directories, and `Path.touch(mode=0o600, exist_ok=True)` followed immediately by `Path.chmod(0o600)` for the file before writing the sensitive data.
