## 2024-05-20 - Path Traversal in Progress File
**Vulnerability:** In `scripts/yt_tool.py`, the `progress_file` path is constructed directly using `playlist_id` without sanitization (`progress_file = LOG_DIR / f"progress_{playlist_id}.json"`). An attacker providing a malicious `playlist_id` (e.g., `../../../tmp/evil`) can perform arbitrary file read/write operations.
**Learning:** Even internal cache or progress files can become vectors for path traversal if the filename includes unsanitized input derived from URLs or identifiers.
**Prevention:** Always sanitize identifiers used in file paths. For example, keeping only alphanumeric characters (`"".join(c for c in playlist_id if c.isalnum() or c in "-_")`) as is done in `scripts/cache_manager.py`.
