## 2025-02-14 - Insecure Default File Permissions for OAuth Credentials
**Vulnerability:** OAuth credentials file (`client_secret.json`) could be created with insecure permissions (e.g., world-readable) because `shutil.copy2` preserves the source file's permissions, and `mkdir` alone doesn't restrict permissions sufficiently.
**Learning:** Even when moving files to a "secure default location", the act of copying must explicitly enforce tight permissions (`0o600` for files, `0o700` for directories). `touch` alone doesn't change permissions of existing files, and `copy2` propagates potentially insecure source modes.
**Prevention:** Always use `mkdir(mode=0o700)` for credential directories, `Path.touch(mode=0o600, exist_ok=True)` followed immediately by `Path.chmod(0o600)` for the file, and `shutil.copyfile` (which doesn't copy stat metadata) instead of `shutil.copy` or `shutil.copy2`.

## 2025-02-14 - Insecure Default File Permissions for OAuth Tokens
**Vulnerability:** OAuth token file (`token.json`) was being created with default file permissions, making it potentially world-readable and leaking access/refresh tokens.
**Learning:** Default file creation ignores strict security constraints needed for credential storage. Using `mkdir(parents=True)` and `write_text()` without setting explicit, secure modes leaves sensitive data vulnerable.
**Prevention:** Always use `mkdir(mode=0o700, ...)` for token directories and enforce file permissions with `touch(mode=0o600, exist_ok=True)` and `chmod(0o600)` prior to writing sensitive data to the file.
