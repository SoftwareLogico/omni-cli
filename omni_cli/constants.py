from __future__ import annotations

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "tiff", "tif"}
PDF_EXTENSIONS = {"pdf"}
NOTEBOOK_EXTENSIONS = {"ipynb"}
AUDIO_EXTENSIONS = {"mp3", "wav", "ogg", "flac", "aac", "m4a", "wma", "aiff", "opus"}
VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "wmv", "flv", "m4v", "mpeg", "mpg"}

# Binary extensions that this tool refuses to read.
# Images, PDFs, notebooks, audio, and video are excluded because they have dedicated handlers.
BINARY_EXTENSIONS = {
    "zip", "tar", "gz", "bz2", "7z", "rar", "xz", "z", "tgz", "iso",
    "exe", "dll", "so", "dylib", "bin", "o", "a", "obj", "lib", "app", "msi", "deb", "rpm",
    "pyc", "pyo", "class", "jar", "war",
    "ear", "node", "wasm", "rlib",
    "dmg", "img",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
    "woff", "woff2", "ttf", "otf", "eot",
    "sqlite", "sqlite3", "db", "mdb", "idx",
    "psd", "ai", "eps", "sketch", "fig", "xd", "blend", "3ds", "max",
    "swf", "fla",
    "lockb", "dat", "data",
    "DS_Store",
}

ARCHIVE_EXTENSIONS = {"zip", "tar", "gz", "bz2", "7z", "rar", "xz", "z", "tgz"}

ARCHIVE_HINTS: dict[str, str] = {
    "zip": (
        "ZIP archives may require a password to inspect their contents. "
        "Use run_command with: unzip -l {path} (list contents), "
        "unzip -P <password> {path} -d /tmp/out (extract with password), "
        "or zipinfo {path} for metadata."
    ),
    "7z": (
        "7z archives may require a password. "
        "Use run_command with: 7z l {path} (list), 7z x -p<password> {path} (extract with password)."
    ),
    "rar": (
        "RAR archives may require a password. "
        "Use run_command with: unrar l {path} (list), unrar x -p<password> {path} (extract with password)."
    ),
    "tar": "Use run_command with: tar -tf {path} (list contents), tar -xf {path} -C /tmp/out (extract).",
    "gz": "Use run_command with: gunzip -c {path} | head or zcat {path}.",
    "tgz": "Use run_command with: tar -tzf {path} (list), tar -xzf {path} -C /tmp/out (extract).",
    "bz2": "Use run_command with: tar -tjf {path} (list), bunzip2 -c {path} | head.",
    "xz": "Use run_command with: tar -tJf {path} (list), xz -d -c {path} | head.",
}

# Device paths that would hang the process (infinite output or blocking input).
BLOCKED_DEVICE_PATHS = {
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr",
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
}

# ── Source of Truth ──
SOT_MARKER = "=== SOURCE OF TRUTH ==="

# ── Version control directories to exclude from searches and scans ──
VCS_DIRS = {".git", ".svn", ".hg", ".bzr", ".jj", ".sl"}

# ── Tool-config fallbacks (authoritative values live in [tools] of omni.toml) ──
# Used only when the runtime calls a tool without a config-backed override
# (e.g. direct library usage, tests, or legacy entry points).
FALLBACK_MAX_ROUNDS = 25
FALLBACK_DELEGATED_MAX_ROUNDS = 8
FALLBACK_REPEAT_LIMIT = 3
FALLBACK_DELEGATED_REPEAT_LIMIT = 2
FALLBACK_SEARCH_DEFAULT_HEAD_LIMIT = 200
FALLBACK_SEARCH_MAX_LINE_LENGTH = 500
FALLBACK_SEARCH_TIMEOUT_SECONDS = 30
# Hard cap on streamed reasoning/thinking characters per turn.
# If the model's reasoning channel exceeds this budget without emitting
# a final answer or tool call, the stream is cut and the round advances.
# Set to 0 to disable the cap.
FALLBACK_REASONING_CHAR_BUDGET = 8000
FALLBACK_DELEGATED_REASONING_CHAR_BUDGET = 4000

# ── Tools that mutate the session (trigger SoT/session refresh) ──
SESSION_MUTATION_TOOLS = {
    "update_session",
    "attach_path_to_source",
    "detach_path_from_source",
}