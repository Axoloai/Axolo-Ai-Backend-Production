"""One error type + the response envelope every endpoint uses:
   {"ok": true,  "data": {...}, "error": null}
   {"ok": false, "data": null,  "error": {"code": "...", "message": "..."}}"""
from typing import Any


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def ok(data: Any = None) -> dict:
    return {"ok": True, "data": data, "error": None}


def fail(code: str, message: str) -> dict:
    return {"ok": False, "data": None, "error": {"code": code, "message": message}}
