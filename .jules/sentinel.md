## 2026-06-18 - [Insecure File Permissions for OAuth Credentials]
**Vulnerability:** OAuth `token.json` and `client_secret.json` files were being created without enforcing restricted file permissions, potentially allowing unauthorized read access on multi-user systems.
**Learning:** When generating or copying sensitive files (like OAuth tokens or credentials), default system permissions may be too permissive (e.g., `0o644` instead of `0o600`).
**Prevention:** Always explicitly set secure file permissions (`0o600` for files, `0o700` for directories) immediately after creating or copying files containing secrets or credentials. Use `.chmod()` on `Path` objects in Python.
