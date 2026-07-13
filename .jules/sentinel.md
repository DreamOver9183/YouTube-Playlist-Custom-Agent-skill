## 2024-05-18 - Path Traversal Vulnerability in Progress File Loading
**Vulnerability:** The `playlist_id` extracted from user input was directly used in the `progress_file` path without sanitization, leading to a path traversal vulnerability.
**Learning:** Even internal filenames created for logging or state tracking should not blindly trust parsed user input without explicit sanitization.
**Prevention:** Sanitize the input to allow only safe alphanumeric characters, hyphens, and underscores before using it in any file operations.
