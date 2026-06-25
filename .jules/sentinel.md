## 2025-06-25 - Insecure File Creation for OAuth Credentials
**Vulnerability:** OAuth credentials (`client_secret.json`) and tokens (`token.json`) were being written with default file permissions and potentially copied using `shutil.copy2` which preserves insecure source permissions. This could allow unauthorized access to sensitive API credentials.
**Learning:** Using `Path.mkdir` and `write_text` or `shutil.copy2` does not ensure that the resulting files are protected with restricted permissions. Furthermore, relying only on creating the file can lead to Time-of-Check to Time-of-Use (TOCTOU) vulnerabilities if an attacker pre-creates a file with broader permissions.
**Prevention:**
1. Set restricted directory permissions (`0o700`) where secrets are stored.
2. Use `Path.touch(mode=0o600, exist_ok=True)` followed explicitly by `Path.chmod(0o600)` to prevent TOCTOU before writing secrets to the file.
3. When copying credentials, use `shutil.copyfile` instead of `shutil.copy2` to ensure destination file permissions are not overwritten by insecure source file permissions.
