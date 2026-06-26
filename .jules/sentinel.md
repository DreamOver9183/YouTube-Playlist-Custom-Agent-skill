## 2024-06-26 - Insecure File Permissions for Credentials
**Vulnerability:** OAuth client secrets and token files were being stored without explicitly setting secure file permissions. `shutil.copy2` was used for copying credentials, which could potentially preserve insecure permissions from the source file. Additionally, files were created without strict permission modes.
**Learning:** Time-of-Check to Time-of-Use (TOCTOU) vulnerabilities can occur if file permissions are not explicitly and atomically set before data is written. When dealing with sensitive files like OAuth credentials, default system umask is insufficient protection. `shutil.copy2` is dangerous for sensitive files as it copies metadata including potentially loose source permissions.
**Prevention:**
1. Always use `Path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)` for credential directories.
2. Use `Path.touch(mode=0o600, exist_ok=True)` followed by `Path.chmod(0o600)` before writing sensitive data to explicitly enforce permissions.
3. Use `shutil.copyfile` instead of `shutil.copy2` when copying sensitive files to ensure destination permissions are defined by the current context, not the source file.
