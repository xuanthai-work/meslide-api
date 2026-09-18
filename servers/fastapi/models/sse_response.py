import json
from typing import Optional

from pydantic import BaseModel


class SSEResponse(BaseModel):
    event: str
    data: str

    def to_string(self):
        return f"event: {self.event}\ndata: {self.data}\n\n"


class SSEStatusResponse(BaseModel):
    status: str
    status_code: Optional[str] = None
    status_params: Optional[dict] = None

    def to_string(self):
        payload = {"type": "status", "status": self.status}
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.status_params is not None:
            payload["status_params"] = self.status_params
        return SSEResponse(event="response", data=json.dumps(payload)).to_string()


class SSETraceResponse(BaseModel):
    trace: object

    def to_string(self):
        return SSEResponse(
            event="response", data=json.dumps({"type": "trace", "trace": self.trace})
        ).to_string()


class SSEErrorResponse(BaseModel):
    detail: str
    error_code: Optional[str] = None
    error_params: Optional[dict] = None

    def to_string(self):
        payload = {"type": "error", "detail": self.detail}
        if self.error_code is not None:
            payload["error_code"] = self.error_code
        if self.error_params is not None:
            payload["error_params"] = self.error_params
        return SSEResponse(event="response", data=json.dumps(payload)).to_string()


class SSECompleteResponse(BaseModel):
    key: str
    value: object

    def to_string(self):
        return SSEResponse(
            event="response",
            data=json.dumps({"type": "complete", self.key: self.value}),
        ).to_string()


class SSEHeartbeatResponse(BaseModel):
    def to_string(self):
        return SSEResponse(
            event="response",
            data=json.dumps({"type": "heartbeat"}),
        ).to_string()
