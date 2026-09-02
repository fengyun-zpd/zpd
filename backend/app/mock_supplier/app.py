"""模拟供应商 API（需求 7 / 架构 9 / ADR 决策 6）。

支持五类结果：正常成功、明确失败、超时但实际创建、相同幂等键重复请求、查询状态。
故障模式由 admin 通过 REST 切换（写入审计由主系统负责），不暴露为 Agent 工具。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

DEFAULT_FAULT_MODES = {
    "normal": "normal",
    "explicit_failure": "explicit_failure",
    "timeout_but_created": "timeout_but_created",
}

# 全局模式（演示切换开关）：normal / explicit_failure / timeout_but_created
MODE_KEY = "__global__"


class SupplierStore:
    """模拟供应商订单与故障模式存储（内存 + 可选 JSON 文件持久化）。"""

    def __init__(self, store_path: str = "") -> None:
        self._lock = threading.Lock()
        self._orders: dict[str, dict] = {}
        self._fault_modes: dict[str, str] = {}
        self._path = store_path
        if store_path:
            self._load()

    def _load(self) -> None:
        path = Path(self._path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._orders = data.get("orders", {})
                self._fault_modes = data.get("fault_modes", {})
            except (json.JSONDecodeError, OSError):  # pragma: no cover
                pass

    def _save(self) -> None:
        if not self._path:
            return
        try:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            Path(self._path).write_text(
                json.dumps(
                    {"orders": self._orders, "fault_modes": self._fault_modes},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:  # pragma: no cover
            pass

    def mode(self, supplier_id: str) -> str:
        with self._lock:
            return self._fault_modes.get(supplier_id) or self._fault_modes.get(MODE_KEY, "normal")

    def set_mode(self, supplier_id: str, mode: str) -> None:
        with self._lock:
            self._fault_modes[supplier_id] = mode
            self._save()

    def all_modes(self) -> dict[str, str]:
        with self._lock:
            return dict(self._fault_modes)

    def get_order(self, supplier_key: str) -> dict | None:
        with self._lock:
            return self._orders.get(supplier_key)

    def create_order(self, supplier_key: str, payload: dict) -> dict:
        with self._lock:
            existing = self._orders.get(supplier_key)
            if existing is not None:
                return existing  # 相同幂等键重复请求：返回原结果
            order = {
                "supplier_key": supplier_key,
                "external_order_no": f"EXT-{uuid.uuid4().hex[:12].upper()}",
                "created_at": time.time(),
                "payload": payload,
            }
            self._orders[supplier_key] = order
            self._save()
            return order


store = SupplierStore()


class PlaceOrderRequest(BaseModel):
    supplier_id: str
    warehouse_id: str
    purchase_order_id: str
    lines: list[dict]


class PlaceOrderResponse(BaseModel):
    created: bool
    external_order_no: str | None = None
    reason: str | None = None


class QueryOrderResponse(BaseModel):
    found: bool
    external_order_no: str | None = None


class FaultModeRequest(BaseModel):
    mode: str


app = FastAPI(title="StockMind 模拟供应商 API", version="0.1.0")


def _timeout_sleep() -> float:
    """超时故障模式的延迟秒数（可配置；适配器超时应更短才能进入 order_unknown）。"""
    import os

    try:
        return float(os.environ.get("MOCK_SUPPLIER_TIMEOUT_SLEEP", "8"))
    except ValueError:  # pragma: no cover
        return 8.0


@app.on_event("startup")
def _startup() -> None:
    # 容器启动时通过环境变量注入存储路径
    import os

    path = os.environ.get("MOCK_SUPPLIER_STORE_PATH", "")
    if path:
        global store
        store = SupplierStore(path)


@app.post("/orders", response_model=PlaceOrderResponse)
def place_order(req: PlaceOrderRequest, request: Request, response: Response) -> PlaceOrderResponse:
    supplier_key = request.headers.get("x-idempotency-key", "")
    if not supplier_key:
        raise HTTPException(status_code=400, detail="缺少 x-idempotency-key")
    mode = store.mode(req.supplier_id)

    if mode == "explicit_failure":
        return PlaceOrderResponse(created=False, reason="supplier rejected the order")

    if mode == "timeout_but_created":
        # 先创建，再模拟超时：适配器超时后查询可确认已创建
        order = store.create_order(supplier_key, req.model_dump())
        time.sleep(_timeout_sleep())
        return PlaceOrderResponse(created=True, external_order_no=order["external_order_no"])

    order = store.create_order(supplier_key, req.model_dump())
    return PlaceOrderResponse(created=True, external_order_no=order["external_order_no"])


@app.get("/orders/{supplier_key}", response_model=QueryOrderResponse)
def query_order(supplier_key: str) -> QueryOrderResponse:
    order = store.get_order(supplier_key)
    if order is None:
        return QueryOrderResponse(found=False)
    return QueryOrderResponse(found=True, external_order_no=order["external_order_no"])


@app.get("/fault-modes")
def list_fault_modes() -> dict:
    return {"modes": store.all_modes(), "valid": list(DEFAULT_FAULT_MODES) + ["normal"]}


@app.put("/fault-modes/{supplier_id}")
def set_fault_mode(supplier_id: str, body: FaultModeRequest) -> dict:
    if body.mode not in DEFAULT_FAULT_MODES:
        raise HTTPException(status_code=422, detail=f"未知故障模式 {body.mode}")
    store.set_mode(supplier_id, body.mode)
    return {"supplier_id": supplier_id, "mode": body.mode}
