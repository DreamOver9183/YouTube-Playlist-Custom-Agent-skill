# Jules / Sentinel Security Learnings & Guidelines

This document records key vulnerability patterns discovered during security audits by Sentinel / Jules, along with preventive design patterns implemented in this codebase.

---

## 1. OAuth Credentials and Token Storage Security (Insecure Permissions & TOCTOU)

### Vulnerability
- OAuth client secrets (`client_secret.json`) and cached access tokens (`token.json`) written with default `umask` permissions can be world- or group-readable on shared multi-user environments.
- Standard utility functions like `shutil.copy2` preserve source file permissions, potentially copying overly permissive modes (e.g. `0o644` or `0o666`).
- Relying solely on `Path.touch(mode=0o600, exist_ok=True)` without subsequent `chmod` does not remediate permissions if the file was pre-created, posing Time-of-Check to Time-of-Use (TOCTOU) risks.

### Prevention & Implementation Rules
1. **Directory Isolation**: Always create credential directories with mode `0o700` (`mkdir(mode=0o700, parents=True, exist_ok=True)` and explicit `chmod(0o700)`).
2. **Atomic / Explicit File Mode**: Use `touch(mode=0o600, exist_ok=True)` followed by explicit `chmod(0o600)` on secret files.
3. **Safe File Copying**: Use `shutil.copyfile` instead of `shutil.copy` or `shutil.copy2` when moving credentials to secure locations to avoid inheriting insecure source permissions.

---

## 2. Path Traversal Prevention in Cache and Progress File Handlers

### Vulnerability
- User-supplied inputs such as YouTube Playlist IDs or raw URLs could contain directory traversal sequences (e.g. `../../../etc/passwd` or `..\\..\\malicious`).
- When interpolated directly into local filesystem operations (such as `progress_{playlist_id}.json` in `scripts/yt_tool.py` or `playlist_{playlist_id}.json` in `scripts/cache_manager.py`), this could lead to arbitrary file read/write/delete vulnerabilities.

### Prevention & Implementation Rules
1. **Strict Whitelist Sanitization**: Sanitize all identifier parameters before concatenating them into filenames:
   ```python
   safe_id = "".join(c for c in raw_id if c.isalnum() or c in "-_") or "unknown"
   ```
2. **Defense in Depth**:
   - URL parsers (`extract_id` in `yt_tool.py` and `extract_playlist_id` in `youtube_api.py`) sanitize fallback values.
   - Filesystem helpers (`progress_path` and `_cache_path`) validate and sanitize identifiers at the point of path construction.
