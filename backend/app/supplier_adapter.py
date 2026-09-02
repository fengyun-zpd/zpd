"""供应商适配器（httpx 客户端，连接模拟供应商 API）。

适配器只负责传输与结果映射；状态迁移必须由采购领域服务按允许转换表完成。
超时/断连/歧义一律返回 outcome="unknown"，由领域服务转入 order_unknown 后查询恢复。
"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.services.purchase_service import SupplierQueryResult, SupplierResult


class HttpSupplierAdapter:
    def __init__(self, base_url: str | None = None, timeout_seconds: float | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.mock_supplier_url).rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.order_timeout_seconds

    def place_order(self, supplier_key: str, payload: dict) -> SupplierResult:
        try:
            resp = httpx.post(
                f"{self.base_url}/orders",
                headers={"x-idempotency-key": supplier_key},
                json=payload,
                timeout=self.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.TransportError) as exc:
            return SupplierResult(outcome="unknown", raw=f"transport: {exc!r}")
        try:
            body = resp.json()
        except ValueError:
            return SupplierResult(outcome="unknown", raw=f"bad response: {resp.status_code}")
        if resp.status_code != 200:
            return SupplierResult(outcome="unknown", raw=f"http {resp.status_code}: {body}")
        if body.get("created") is True:
            return SupplierResult(
                outcome="success",
                external_order_no=body.get("external_order_no"),
                raw=str(body),
            )
        return SupplierResult(outcome="explicit_failure", raw=str(body))

    def query_order(self, supplier_key: str) -> SupplierQueryResult:
        try:
            resp = httpx.get(f"{self.base_url}/orders/{supplier_key}", timeout=self.timeout_seconds)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.TransportError) as exc:
            raise RuntimeError(f"查询供应商失败: {exc!r}") from exc
        body = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"查询供应商失败: http {resp.status_code}")
        return SupplierQueryResult(
            found=bool(body.get("found")),
            external_order_no=body.get("external_order_no"),
        )

    def set_fault_mode(self, supplier_id: str, mode: str) -> dict:
        resp = httpx.put(
            f"{self.base_url}/fault-modes/{supplier_id}",
            json={"mode": mode},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_fault_modes(self) -> dict:
        resp = httpx.get(f"{self.base_url}/fault-modes", timeout=10)
        resp.raise_for_status()
        return resp.json()
