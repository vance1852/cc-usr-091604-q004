"""裁判指派领域的最小起点。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Official:
    """保存裁判姓名和等级。"""

    name: str
    grade: str


class AssignmentService:
    """提供指派服务的基础健康状态。"""

    def health(self) -> dict[str, str]:
        return {"service": "assignment", "status": "ok"}

