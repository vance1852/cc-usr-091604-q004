"""平台领域错误。

所有业务异常都继承 :class:`PlatformError` 并带有稳定的 ``code``，
接口层可以直接把 ``code``/``message``/``details`` 映射为响应体。
"""


class PlatformError(Exception):
    """平台业务错误基类。"""

    code = "PLATFORM_ERROR"

    def __init__(self, message: str = "", *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFoundError(PlatformError):
    """引用的实体不存在。"""

    code = "NOT_FOUND"


class VersionConflictError(PlatformError):
    """乐观锁版本不匹配：确认/锁定时数据已被他人修改。"""

    code = "VERSION_CONFLICT"


class MatchFinishedError(PlatformError):
    """比赛已结束，历史指派不能被覆盖。"""

    code = "MATCH_FINISHED"


class InvalidStateError(PlatformError):
    """当前状态不允许执行该操作。"""

    code = "INVALID_STATE"


class ReasonRequiredError(PlatformError):
    """接受/拒绝/撤销等操作必须填写理由。"""

    code = "REASON_REQUIRED"


class NotEligibleError(PlatformError):
    """候选不满足资格或冲突规则。

    ``details`` 形如 ``{referee_id: [exclusion, ...]}``，每条 exclusion
    带有 ``code`` 与 ``category``（qualification / availability / conflict），
    调用方可以据此区分“不合格”与“被冲突规则排除”。
    """

    code = "NOT_ELIGIBLE"
