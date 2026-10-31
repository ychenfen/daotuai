"""工作流错误类型（HTTP 层按 status 映射状态码）。"""
from __future__ import annotations


class WorkflowError(Exception):
    status = 400


class NotFound(WorkflowError):
    status = 404


class Conflict(WorkflowError):
    status = 409

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


class BudgetExceeded(WorkflowError):
    status = 402

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


class Busy(WorkflowError):
    status = 409
