# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py:available_functions/dispatch（217–225、323–332 行），
# 以及 D:/Programs/Python/OOAD/Assignment1/support/assessment.py:validate_tool_interfaces（271–306 行）。
# 功能：保留注册、签名绑定和错误回传；增加内置 Schema 子集校验、默认拒绝和敏感工具审批。
# 后续：完整 JSON Schema/插件校验、持久化执行状态、幂等键和审批重放保护；当前不是持久化调度器。
import inspect
import json
from copy import deepcopy

from app.common.api.runtime import ApprovalService
from app.common.model.runtime import ApprovalRequest, ToolResult
from app.tools.approval import DenyApproval
from app.tools.builtin import BuiltinTools
from app.tools.schemas import TOOL_SCHEMAS
from app.tools.workspace import Workspace


def validate_tool_interfaces(schemas, functions):
    errors, definitions = [], {}
    for item in schemas:
        spec = item.get("function", {})
        name = spec.get("name")
        if item.get("type") != "function" or not name or name in definitions:
            errors.append(f"Missing/duplicate/invalid tool name: {name!r}")
        definitions[name] = spec
    for name in set(functions) | set(definitions):
        function = functions.get(name)
        if not callable(function) or name not in definitions:
            errors.append(f"{name}: missing callable registration or JSON Schema")
            continue
        parameters = definitions[name].get("parameters", {})
        properties = parameters.get("properties", {})
        signature = inspect.signature(function)
        accepted = set(signature.parameters)
        needed = {key for key, value in signature.parameters.items() if value.default is inspect.Parameter.empty}
        required = set(parameters.get("required", []))
        if parameters.get("type") != "object" or set(properties) != accepted:
            errors.append(f"{name}: schema parameters do not match function signature")
        if not needed <= required <= accepted:
            errors.append(f"{name}: required fields do not match function signature")
    return errors


def validate_arguments(value, schema, path="arguments"):
    types = schema.get("type", "object")
    types = types if isinstance(types, list) else [types]
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "null": value is None,
               "boolean": isinstance(value, bool),
               "integer": isinstance(value, int) and not isinstance(value, bool),
               "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
    if not any(matches.get(kind, False) for kind in types):
        raise TypeError(f"{path} must have type {' or '.join(types)}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise ValueError(f"{path} missing required fields: {sorted(missing)}")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError(f"{path} has unexpected fields")
        for key, item in value.items():
            if key in properties:
                validate_arguments(item, properties[key], f"{path}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{path} has too few items")
        for index, item in enumerate(value):
            validate_arguments(item, schema.get("items", {}), f"{path}[{index}]")


class ToolRegistry:
    def __init__(self, schemas, functions, workspace, approval: ApprovalService | None = None, sensitive=()):
        self._schemas = deepcopy(schemas)
        self.functions = dict(functions)
        self.workspace = str(workspace)
        self.approval = approval if approval is not None else DenyApproval()
        self.sensitive = set(sensitive)
        errors = validate_tool_interfaces(self._schemas, self.functions)
        if self.sensitive - self.functions.keys():
            errors.append("Sensitive tools must be registered")
        if errors:
            raise ValueError("; ".join(errors))
        self.definitions = {item["function"]["name"]: item["function"] for item in self._schemas}

    @property
    def schemas(self):
        return deepcopy(self._schemas)

    def execute(self, call_id, name, arguments):
        try:
            function = self.functions[name]
            validate_arguments(arguments, self.definitions[name]["parameters"])
            inspect.signature(function).bind(**arguments)
            approved = None
            if name in self.sensitive:
                request = ApprovalRequest(call_id, name, deepcopy(arguments), self.workspace,
                                          f"{name}: " + json.dumps(arguments, ensure_ascii=False))
                approved = self.approval.approve(request) is True
                if not approved:
                    return ToolResult("denied", "Operation denied. Stop without retrying or using another tool.", False)
            result = function(**arguments)
            if not isinstance(result, ToolResult):
                result = ToolResult("success", result)
            result.approved = approved
            return result
        except Exception as error:
            return ToolResult("error", f"Tool error ({type(error).__name__}): {error}")


def create_builtin_registry(workspace, approval=None, timeout=15.0):
    scope = Workspace(workspace)
    builtin = BuiltinTools(scope, timeout=timeout)
    functions = {item["function"]["name"]: getattr(builtin, item["function"]["name"]) for item in TOOL_SCHEMAS}
    return ToolRegistry(TOOL_SCHEMAS, functions, scope.root, approval,
                        sensitive={"write_file", "execute_command", "run_python"})