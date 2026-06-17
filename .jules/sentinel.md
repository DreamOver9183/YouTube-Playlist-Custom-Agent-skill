## 2024-06-17 - Insecure Permissions on OAuth Credentials

**Vulnerability:** OAuth access tokens (`token.json`) and client secrets (`client_secret.json`) were being written to disk with default file permissions (often 0o644 or 0o664). This allows any local user on the system to read the files, steal the user's YouTube API credentials, and impersonate the user.
**Learning:** Python's standard `Path.write_text()` and `shutil.copy2()` do not restrict permissions by default. It's critical to be aware of the default `umask` and use lower-level functions when storing secrets on the filesystem, especially since this tool runs locally on potentially multi-user machines.
**Prevention:** Use `os.open` with `os.O_CREAT | os.O_WRONLY | os.O_TRUNC` and an explicit mode like `0o600` when creating files containing secrets. Additionally, ensure the directory containing the secrets is also restricted using `0o700` permissions.
