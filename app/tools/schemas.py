# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py:tools（130–216 行）。
# 功能：保留五个工具的名称、参数及 JSON Schema，更新为工作区和宿主审批描述。
# 后续：扩展插件元数据、风险级别和 Schema 版本；本文件不负责授权。
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a UTF-8 text file inside the configured workspace.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string", "description": "Workspace-relative or in-workspace absolute file path"}},
            "required": ["file_path"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or replace a complete UTF-8 text file; subject to host approval.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string", "description": "File path inside the workspace"},
            "content": {"type": "string", "description": "Complete replacement text"}},
            "required": ["file_path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "list_files", "description": "Recursively list files and directories, including empty directories; paths are relative to the requested directory.",
        "parameters": {"type": "object", "properties": {
            "directory": {"type": "string", "description": "Workspace directory to inspect recursively"}},
            "required": ["directory"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "execute_command", "description": "Execute an argv list only after host approval; not a shell and not a sandbox.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                        "description": "Executable and each argument as separate strings; no shell expansion"}},
            "required": ["command"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "run_python", "description": "Run a workspace Python file with the current interpreter after host approval; capture real process output.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string", "description": "Python file inside the workspace"},
            "args": {"type": ["array", "null"], "items": {"type": "string"},
                     "description": "Optional script arguments; omit or use null for none"}},
            "required": ["file_path"], "additionalProperties": False}}},
]