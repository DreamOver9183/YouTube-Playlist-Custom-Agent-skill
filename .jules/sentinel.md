## 2025-02-14 - Insecure Default File Permissions for OAuth Credentials
**Vulnerability:** OAuth credentials file (`client_secret.json`) could be created with insecure permissions (e.g., world-readable) because `shutil.copy2` preserves the source file's permissions, and `mkdir` alone doesn't restrict permissions sufficiently.
**Learning:** Even when moving files to a "secure default location", the act of copying must explicitly enforce tight permissions (`0o600` for files, `0o700` for directories). `touch` alone doesn't change permissions of existing files, and `copy2` propagates potentially insecure source modes.
**Prevention:** Always use `mkdir(mode=0o700)` for credential directories, `Path.touch(mode=0o600, exist_ok=True)` followed immediately by `Path.chmod(0o600)` for the file, and `shutil.copyfile` (which doesn't copy stat metadata) instead of `shutil.copy` or `shutil.copy2`.
## 2026-07-11 - Prevent Path Traversal in Playlist ID Extraction
**Vulnerability:** The `extract_id` function returned unsanitized fallback input, which was used downstream in file paths, allowing path traversal.
**Learning:** Always sanitize inputs that act as identifiers when they are subsequently used to construct file paths or other sensitive operations.
**Prevention:** Apply a strict allowlist (e.g., alphanumeric, hyphens, and underscores) to input fallback values.
