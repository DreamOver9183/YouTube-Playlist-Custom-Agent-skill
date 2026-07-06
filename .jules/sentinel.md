## 2025-02-14 - Insecure Default File Permissions for OAuth Credentials
**Vulnerability:** OAuth credentials file (`client_secret.json`) could be created with insecure permissions (e.g., world-readable) because `shutil.copy2` preserves the source file's permissions, and `mkdir` alone doesn't restrict permissions sufficiently.
**Learning:** Even when moving files to a "secure default location", the act of copying must explicitly enforce tight permissions (`0o600` for files, `0o700` for directories). `touch` alone doesn't change permissions of existing files, and `copy2` propagates potentially insecure source modes.
**Prevention:** Always use `mkdir(mode=0o700)` for credential directories, `Path.touch(mode=0o600, exist_ok=True)` followed immediately by `Path.chmod(0o600)` for the file, and `shutil.copyfile` (which doesn't copy stat metadata) instead of `shutil.copy` or `shutil.copy2`.

## 2025-02-14 - Insecure Default File Permissions for Saved OAuth Tokens
**Vulnerability:** Cached OAuth token files (`token.json`) were being written without explicitly restricting file permissions. Depending on the environment's `umask`, the file could be created as world-readable, exposing the valid OAuth token and allowing unauthorized API access on the user's behalf.
**Learning:** Writing sensitive secrets using `Path.write_text()` without setting the correct permissions beforehand relies on the system's `umask`, which is often insecure by default (e.g., `022` resulting in `644`).
**Prevention:** Before writing sensitive tokens or credentials to a file, ensure the parent directory is restricted (`mkdir(mode=0o700)`) and explicitly create and restrict the file permissions (`Path.touch(mode=0o600, exist_ok=True)` followed by `Path.chmod(0o600)`) before writing the contents.
