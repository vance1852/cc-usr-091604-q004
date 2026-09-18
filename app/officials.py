"""裁判指派领域的最小起点（保留兼容导出）。

完整的指派服务已迁移至 :mod:`app.service`，这里保留早期的
``Official`` 对象与 ``AssignmentService`` 导出以兼容既有调用方。
"""

from dataclasses import dataclass

from app.service import AssignmentService

__all__ = ["Official", "AssignmentService"]


@dataclass(frozen=True)
class Official:
    """保存裁判姓名和等级。"""

    name: str
    grade: str
