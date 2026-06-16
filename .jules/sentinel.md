## 2024-06-16 - [Path Traversal in Progress Log Path]
**Vulnerability:** Path traversal vulnerability in `scripts/yt_tool.py` when generating the progress file path based on `playlist_id`.
**Learning:** External inputs, like YouTube playlist IDs, shouldn't be directly concatenated into file paths without sanitization. Even if `playlist_id` looks alphanumeric, an attacker could supply a crafted ID like `../../../foo` to read, write, or delete arbitrary files on the system when creating progress logs.
**Prevention:** Always sanitize variables that are used to construct file paths, ensuring they only contain safe characters (e.g., alphanumeric, dashes, and underscores) before using them in file operations.
