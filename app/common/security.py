# 来源：D:/Programs/Python/OOAD/Assignment1/support/runtime.py:redact（10–15 行）。
# 功能：保留密钥替换和 sk- 格式脱敏，扩展到嵌套事件及常见密钥环境变量。
# 后续：补充业务级敏感字段策略；脱敏不等于权限控制，原始会话仍需受控存储。
import os
import re


def redact(value):
    text = str(value)
    for name, key in os.environ.items():
        if key and (name.endswith(("API_KEY", "ACCESS_TOKEN", "SECRET"))):
            text = text.replace(key, "[REDACTED]")
    return re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)


def redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(key): redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value