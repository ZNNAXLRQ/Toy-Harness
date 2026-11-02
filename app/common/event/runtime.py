# 来源：D:/Programs/Python/OOAD/Assignment1/agent.py:emit/trace（263–269 行）。
# 功能：提取可注入内存事件接收器，用于离线验证和后续界面适配。
# 后续：替换为持久化事件与提交后 SSE 发布；此列表不会跨进程恢复。
from copy import deepcopy

from app.common.security import redact_value


class MemoryEventSink:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(deepcopy(redact_value(event)))