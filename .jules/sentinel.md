## 2025-06-19 - [CRITICAL] File Permissions for Credentials
**Vulnerability:** OAuth credential files and token files were being created without explicit, secure file permissions (e.g., they could default to world-readable or group-readable based on umask).
**Learning:** Any file or directory that stores sensitive information, like API keys, secrets, or tokens, must be explicitly restricted to only the user running the application to avoid unauthorized read access.
**Prevention:** Always enforce strict file permissions (mode `0o600` for files, `0o700` for directories) immediately when creating directories or files that will contain sensitive material.
