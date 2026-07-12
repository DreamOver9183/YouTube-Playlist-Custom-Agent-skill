## 2025-02-14 - Insecure Default File Permissions for OAuth Credentials
**Vulnerability:** OAuth credentials file (`client_secret.json`) could be created with insecure permissions (e.g., world-readable) because `shutil.copy2` preserves the source file's permissions, and `mkdir` alone doesn't restrict permissions sufficiently.
**Learning:** Even when moving files to a "secure default location", the act of copying must explicitly enforce tight permissions (`0o600` for files, `0o700` for directories). `touch` alone doesn't change permissions of existing files, and `copy2` propagates potentially insecure source modes.
**Prevention:** Always use `mkdir(mode=0o700)` for credential directories, `Path.touch(mode=0o600, exist_ok=True)` followed immediately by `Path.chmod(0o600)` for the file, and `shutil.copyfile` (which doesn't copy stat metadata) instead of `shutil.copy` or `shutil.copy2`.

## 2025-02-14 - Path Traversal Vulnerability in Progress File
**Vulnerability:** The application unsafely used the `playlist_id` input directly in file paths (e.g., `LOG_DIR / f"progress_{playlist_id}.json"`), allowing attackers to traverse directories and potentially read/write files if a malicious `playlist_id` like `../../../etc/passwd` was provided.
**Learning:** Even when reading from local JSON or cache, input parameters that make up file paths must be strictly validated. Untrusted input should never be placed directly into a path construction.
**Prevention:** Always sanitize strings before using them in file paths (e.g., limiting to alphanumeric characters, hyphens, and underscores: `safe_id = "".join(c for c in id if c.isalnum() or c in "-_")`). Alternatively, hash the input string to generate a safe filename.
