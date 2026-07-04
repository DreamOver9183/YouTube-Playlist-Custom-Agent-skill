## 2025-02-14 - Insecure Default File Permissions for OAuth Credentials
**Vulnerability:** OAuth credentials file (`client_secret.json`) could be created with insecure permissions (e.g., world-readable) because `shutil.copy2` preserves the source file's permissions, and `mkdir` alone doesn't restrict permissions sufficiently.
**Learning:** Even when moving files to a "secure default location", the act of copying must explicitly enforce tight permissions (`0o600` for files, `0o700` for directories). `touch` alone doesn't change permissions of existing files, and `copy2` propagates potentially insecure source modes.
**Prevention:** Always use `mkdir(mode=0o700)` for credential directories, `Path.touch(mode=0o600, exist_ok=True)` followed immediately by `Path.chmod(0o600)` for the file, and `shutil.copyfile` (which doesn't copy stat metadata) instead of `shutil.copy` or `shutil.copy2`.
## 2025-02-14 - Insecure Token Cache Permissions
**Vulnerability:** OAuth cached token file (`token.json`) was being created with insecure default permissions, allowing unauthorized access to the sensitive `access_token` and `refresh_token`.
**Learning:** Automatically generated cached tokens containing sensitive credentials must be guarded with explicit secure file permissions upon creation, just like the original secret files they represent.
**Prevention:** Apply `mode=0o700` to the token cache directory upon creation, and use `Path.touch(mode=0o600, exist_ok=True)` and `Path.chmod(0o600)` on the token file before writing credentials to disk.
