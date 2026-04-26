from __future__ import annotations

from typing import Any

from sot_cli.config import KNOWN_PROVIDERS
from sot_cli.config.prompts import (
    APPLY_TEXT_EDITS_PROMPT,
    ATTACH_PATH_PROMPT,
    DELEGATE_TASK_PROMPT,
    DELETE_FILE_PROMPT,
    DETACH_PATH_PROMPT,
    EDIT_FILE_PROMPT,
    GET_SESSION_STATE_PROMPT,
    LIST_DIR_PROMPT,
    LIST_COMMANDS_PROMPT,
    LIST_TASKS_PROMPT,
    OPEN_PATH_PROMPT,
    READ_COMMAND_OUTPUT_PROMPT,
    READ_MANY_FILES_PROMPT,
    RUN_COMMAND_PROMPT,
    SEARCH_CODE_PROMPT,
    STOP_COMMAND_PROMPT,
    UPDATE_SESSION_PROMPT,
    WAIT_COMMAND_PROMPT,
    WAIT_TASK_PROMPT,
    WRITE_FILE_PROMPT,
)


def get_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": LIST_DIR_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path to the directory."},
                        "follow_symlinks": {"type": "boolean", "description": "If true, recurse through symlinked directories."},
                        "kind": {"type": "string", "enum": ["file", "directory", "symlink", "symlink_file", "symlink_directory"], "description": "Optional kind filter."},
                        "extensions": {"type": "array", "items": {"type": "string"}, "description": "Optional extension filter list. Accept values like '.png' or 'png'. Case-insensitive."},
                        "name_contains": {"type": "string", "description": "Optional case-insensitive substring filter applied to the basename. Supports multiple keywords separated by commas (e.g., 'File1, fILE2, etc') acting as an OR condition."},
                        "path_contains": {"type": "string", "description": "Optional case-insensitive substring filter applied to the relative path and absolute path."},
                        "name_pattern": {"type": "string", "description": "Optional basename glob pattern using wildcards like '*', '?', and '[]'. Case-insensitive."},
                        "path_pattern": {"type": "string", "description": "Optional relative-path or absolute-path glob pattern using wildcards like '*', '?', and '[]'. Case-insensitive."},
                        "content_contains": {"type": "string", "description": "Optional text search inside file contents (UTF-8 text files). Supports multiple keywords separated by commas as OR."},
                        "content_case_sensitive": {"type": "boolean", "description": "If true, content_contains is case-sensitive. Default false."},
                        "content_max_bytes": {"type": "integer", "minimum": 0, "description": "Optional maximum file size in bytes for content_contains scanning. Files above this size are skipped. 0 means no size cap."},
                        "min_size_bytes": {"type": "integer", "minimum": 0, "description": "Optional minimum size in bytes."},
                        "max_size_bytes": {"type": "integer", "minimum": 0, "description": "Optional maximum size in bytes."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_files",
                "description": READ_MANY_FILES_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "description": "List of file read requests to execute together.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "Absolute path or project-relative path to the file."},
                                    "start_line": {"type": "integer", "minimum": 1, "description": "1-indexed starting line for a targeted text excerpt. Use with end_line to read a specific range."},
                                    "end_line": {"type": "integer", "minimum": 1, "description": "1-indexed ending line (inclusive) for a targeted text excerpt. Must be used together with start_line."},
                                    "pages": {"type": "string", "description": "Optional PDF page range like '1-5' or '3'. Only valid for PDF files."},
                                    "password": {"type": "string", "description": "Optional password for encrypted/protected PDF files."},
                                },
                                "required": ["path"],
                                "additionalProperties": False,
                            },
                            "minItems": 1,
                        },
                    },
                    "required": ["files"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "open_path",
                "description": OPEN_PATH_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path to the file or directory to open."},
                        "application": {"type": "string", "description": "Optional app name, command name, or executable path to use instead of the OS default application."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_code",
                "description": SEARCH_CODE_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regular expression pattern to search for in file contents."},
                        "path": {"type": "string", "description": "File or directory to search in. Defaults to project root."},
                        "glob": {"type": "string", "description": "Glob pattern to filter files (e.g. \"*.py\", \"*.{ts,tsx}\")."},
                        "type": {"type": "string", "description": "File type to search (e.g., \"py\", \"js\", \"rust\", \"go\", \"java\")."},
                        "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "description": "Output mode: \"content\" shows matching lines, \"files_with_matches\" shows file paths (default), \"count\" shows match counts."},
                        "context_before": {"type": "integer", "minimum": 0, "description": "Lines of context to show before each match (content mode only)."},
                        "context_after": {"type": "integer", "minimum": 0, "description": "Lines of context to show after each match (content mode only)."},
                        "context": {"type": "integer", "minimum": 0, "description": "Lines of context before and after each match (content mode only). Overrides context_before/context_after."},
                        "show_line_numbers": {"type": "boolean", "description": "Show line numbers in content mode output. Defaults to true."},
                        "case_insensitive": {"type": "boolean", "description": "Case insensitive search. Defaults to false."},
                        "head_limit": {"type": "integer", "minimum": 0, "description": "Max result lines/entries. Defaults to 200. Pass 0 for unlimited."},
                        "offset": {"type": "integer", "minimum": 0, "description": "Skip first N results before applying head_limit. For pagination."},
                        "multiline": {"type": "boolean", "description": "Enable multiline mode where patterns can span lines. Defaults to false."},
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": RUN_COMMAND_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to run."},
                        "stdin": {"type": "string", "description": "Optional text to feed to the process stdin. Use for passwords, interactive prompts, or piped input. Not available in background mode."},
                        "cwd": {"type": "string", "description": "Optional absolute or project-relative working directory."},
                        "timeout_seconds": {"type": "integer", "minimum": 0, "description": "Optional timeout in seconds. Use 0 for no timeout."},
                        "background": {"type": "boolean", "description": "If true, start the command in the background and return immediately with pid and log paths."},
                        "run_in_background": {"type": "boolean", "description": "Alias for background."},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_commands",
                "description": LIST_COMMANDS_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_command_output",
                "description": READ_COMMAND_OUTPUT_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command_id": {"type": "string", "description": "Stable ID returned by run_command in background mode."},
                        "tail_lines": {"type": "integer", "minimum": 1, "description": "Optional number of trailing lines to read. Defaults to 100."},
                    },
                    "required": ["command_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "wait_command",
                "description": WAIT_COMMAND_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command_id": {"type": "string", "description": "Stable ID returned by run_command in background mode."},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "description": "Optional maximum time to wait. If omitted, waits indefinitely."},
                    },
                    "required": ["command_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "stop_command",
                "description": STOP_COMMAND_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command_id": {"type": "string", "description": "Stable ID returned by run_command in background mode."},
                    },
                    "required": ["command_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": EDIT_FILE_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path to the file."},
                        "old_string": {"type": "string", "description": "Exact text to find and replace."},
                        "new_string": {"type": "string", "description": "Replacement text."},
                        "replace_all": {"type": "boolean", "description": "If true, replace all matches instead of requiring a unique match."},
                    },
                    "required": ["path", "old_string", "new_string"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "apply_text_edits",
                "description": APPLY_TEXT_EDITS_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path to the file."},
                        "edits": {
                            "type": "array",
                            "minItems": 1,
                            "description": "Sequential list of atomic edits to apply in memory before writing the file.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "new_string": {"type": "string", "description": "Replacement text for this edit."},
                                    "old_string": {"type": "string", "description": "Exact text to replace. Use this or start_line/end_line."},
                                    "start_line": {"type": "integer", "minimum": 1, "description": "1-indexed starting line for an exact block replacement."},
                                    "end_line": {"type": "integer", "minimum": 1, "description": "1-indexed ending line for an exact block replacement."},
                                    "before_context": {"type": "string", "description": "Optional exact text that must appear immediately before old_string."},
                                    "after_context": {"type": "string", "description": "Optional exact text that must appear immediately after old_string."},
                                },
                                "required": ["new_string"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["path", "edits"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": WRITE_FILE_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path to the file."},
                        "content": {"type": "string", "description": "Full UTF-8 text content to write."},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_file",
                "description": DELETE_FILE_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path to the file or directory."},
                        "recursive": {"type": "boolean", "description": "Required for deleting directories that are not symlinks."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_session_state",
                "description": GET_SESSION_STATE_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_session",
                "description": UPDATE_SESSION_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Optional new title for the session."},
                        "provider": {"type": "string", "enum": list(KNOWN_PROVIDERS), "description": "Optional provider to use for future turns in this session."},
                        "model": {"type": "string", "description": "Optional model to use for future turns in this session."},
                        "temperature": {"type": "number", "description": "Optional temperature override for future turns in this session."},
                        "max_output_tokens": {"type": "integer", "minimum": 1, "description": "Optional max output tokens override for future turns in this session."},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "detach_path_from_source",
                "description": DETACH_PATH_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute path or project-relative path already attached to the session source of truth."},
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": "Batch removal: absolute paths or project-relative paths already attached to the session source of truth. Prefer this over multiple calls when detaching several paths."
                        },
                    },
                    "additionalProperties": False,
                    "oneOf": [
                        {"required": ["path"]},
                        {"required": ["paths"]}
                    ],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "attach_path_to_source",
                "description": ATTACH_PATH_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute or project-relative path to attach."},
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": "Batch attach: absolute or project-relative paths to attach. Prefer this over multiple calls when attaching several paths."
                        },
                        "recursive": {"type": "boolean", "description": "Whether a directory attach should recurse. Applies to every path in the batch."},
                        "label": {"type": "string", "description": "Optional human label. Only supported when attaching a single path."},
                    },
                    "additionalProperties": False,
                    "oneOf": [
                        {"required": ["path"]},
                        {"required": ["paths"]}
                    ],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_tasks",
                "description": LIST_TASKS_PROMPT,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "wait_task",
                "description": WAIT_TASK_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", "description": "The agent_id to wait for (e.g., agent_1)."},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "description": "Optional maximum time to wait in seconds. If omitted, waits indefinitely."}
                    },
                    "required": ["agent_id"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delegate_task",
                "description": DELEGATE_TASK_PROMPT,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_prompt": {
                            "type": "string",
                            "description": "Detailed instructions for the sub-agent. Be specific about what to do and what to return.",
                        },
                        "provider": {
                            "type": "string",
                            "description": "Optional provider override for the sub-agent. Defaults to the current session provider.",
                        },
                        "attempts": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum number of repeated failed attempts allowed before the sub-agent aborts. Default is 2.",
                        },
                        "background": {
                            "type": "boolean",
                            "description": "If true, launches the agent and returns immediately. If false, waits for the final report. Default is false.",
                        },
                    },
                    "required": ["task_prompt"],
                    "additionalProperties": False,
                },
            },
        },
    ]