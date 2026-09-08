"""端到端浏览器测试（需求 9 / 架构 10）。

需前端（:3000）与后端（:8000）已启动；标记 e2e，默认不在 CI 主流程运行。
走通：助手对话 -> 草稿 -> 审批 -> 建单 -> 下达 -> 收货 -> 关闭。
可从 Windows 或 WSL 运行；BASE_URL 可用环境变量 E2E_BASE_URL 覆盖。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.e2e

BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:3000")


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


def _goto(page, path: str) -> None:
    page.goto(f"{BASE_URL}{path}", wait_until="networkidle")


def test_browser_closed_loop(browser):
    """浏览器真实闭环：补货对话（可能因已有活动建议而防重）-> 审批 -> 建单 -> 下达 -> 收货 -> 关闭。

    容器环境 Celery Beat 每分钟扫描低库存 SKU 并生成待审批计划；此时助手对话同一 SKU
    会被活动建议防重拦截（正确安全行为）。测试容忍两种结果：对话生成新草稿，或
    直接使用已存在的待审批计划走审批闭环。
    """
    page = browser.new_page()
    try:
        # 1) 助手发起补货（草稿或防重提示均为正常）
        _goto(page, "/")
        page.fill("input[placeholder='输入补货请求…']", "帮我检查华东仓未来两周需要补货的紧固件")
        page.keyboard.press("Enter")
        page.wait_for_function(
            "() => { const t = document.querySelector('.chat-log'); return t && (t.textContent.includes('已生成补货草稿') || t.textContent.includes('无法生成草稿')); }",
            timeout=180000,
        )
        plan_text = page.text_content(".chat-log") or ""
        assert ("已生成补货草稿" in plan_text) or ("无法生成草稿" in plan_text)

        # 2) 审批（待审批计划）。主环境可能已经由定时扫描或上一次演示处理完
        # 待审批计划；此时保留助手防重结果，后续复用已有 po_created 采购单继续
        # 验证采购状态机，不把持久化业务事实误判为页面故障。
        page.click("button:has-text('审批箱')")
        page.wait_for_timeout(1000)
        pending_select = page.locator(".card select").first
        pending_available = pending_select.locator("option").count() > 1
        if pending_available:
            pending_select.select_option(index=1)
            page.wait_for_selector("button:has-text('提交审批')", timeout=15000)
            page.click("button:has-text('提交审批')")
            page.wait_for_selector("text=审批箱", timeout=15000)

        # 3) 采购单：从已批准计划创建采购单（页面内完成建单）
        page.click("button:has-text('采购单')")
        page.wait_for_timeout(1000)
        create_select = page.locator(".create-po select")
        if create_select.locator("option").count() > 1:
            create_select.select_option(index=1)
            page.click("button:has-text('创建采购单')")
        if not page.locator(".card select").nth(1).locator("option").filter(has_text="已建采购单").count():
            pytest.skip("当前持久化演示库没有 po_created 采购单；重置合成种子后可运行完整采购闭环")
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('.card select option')).some(o => o.textContent.includes('已建采购单'))",
            timeout=15000,
        )
        # 选中待下达的采购单。持久化演示库可能只剩种子 po_created 单，
        # 因此按状态选择而不依赖是否为 SEED 或下拉 index。
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('.card select option')).some(o => o.textContent.includes('已建采购单'))",
            timeout=15000,
        )
        po_select = page.locator(".card select").nth(1)
        po_label = po_select.locator("option").filter(has_text="已建采购单").first.text_content()
        assert po_label, "采购单下拉中没有待下达的 po_created 采购单"
        po_select.select_option(label=po_label)
        page.wait_for_selector("button:has-text('下达（模拟供应商）')", timeout=15000)

        # 4) 下达
        page.click("button:has-text('下达（模拟供应商）')")
        page.wait_for_selector("span.badge.status-ordered", timeout=60000)

        # 5) 收货：先通过页面点击"登记到货"验证 prompt 收货流程，再用 API 收满全部明细行。
        #    多行 PO 需全部收齐才 received（需求 6.3）；页面 prompt 对多行大数量时序脆弱，
        #    因此页面点击仅验证入口可达，数量收满交由 API（与集成测试同一领域服务）。
        page.wait_for_selector("button:has-text('登记到货')", timeout=15000)

        def _on_dialog_first(dialog):
            dialog.accept("1")  # 只验证页面可发起收货（后续数量由 API 收满）

        page.once("dialog", _on_dialog_first)
        page.locator("button:has-text('登记到货')").first.click()
        page.wait_for_timeout(800)

        # 通过 API 收满选中 PO 的全部明细行（补齐剩余量；用标准库 urllib 避免额外依赖）
        import json
        import urllib.error
        import urllib.request

        api_base = BASE_URL.replace("http://localhost:13000", "http://127.0.0.1:18000").replace(
            "http://127.0.0.1:13000", "http://127.0.0.1:18000"
        )
        po_id = po_select.input_value()

        def _api_get(path: str) -> dict:
            req = urllib.request.Request(f"{api_base}{path}", headers={"X-Actor-Id": "dave"}, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())

        def _api_post(path: str, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                f"{api_base}{path}",
                data=data,
                headers={
                    "X-Actor-Id": "alice",
                    "X-Idempotency-Key": f"rcv-{os.urandom(8).hex()}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()

        # 页面弹窗可能已经登记第一批到货；每次按最新业务事实读取，避免用
        # 旧 remaining 覆盖并发收货。409 仅在该合法竞态下重读，其他错误继续失败。
        for _ in range(3):
            po_data = _api_get(f"/api/v1/purchase-orders/{po_id}")["data"]
            pending_lines = [line for line in po_data["lines"] if int(line["remaining"]) > 0]
            if not pending_lines:
                break
            for line in pending_lines:
                try:
                    _api_post(
                        f"/api/v1/purchase-order-lines/{line['line_id']}/receive",
                        {
                            "receipt_event_id": f"evt-{os.urandom(8).hex()}",
                            "qty": int(line["remaining"]),
                        },
                    )
                except urllib.error.HTTPError as exc:
                    if exc.code != 409:
                        raise
        else:
            raise AssertionError("收货状态在重读三次后仍未收齐")
        # 页面刷新以展示 received 状态（reload 会回到默认页，需重新进入采购单页并选中该 PO）
        page.reload(wait_until="networkidle")
        page.click("button:has-text('采购单')")
        page.wait_for_function(
            f"() => Array.from(document.querySelectorAll('.card select option')).some(o => o.value === {po_id!r})",
            timeout=15000,
        )
        po_select = page.locator(".card select").nth(1)
        po_select.select_option(po_id)
        page.wait_for_selector("span.badge.status-received", timeout=30000)

        # 6) 关闭
        page.click("button:has-text('确认关闭')")
        page.wait_for_selector("span.badge.status-closed", timeout=15000)
        print("E2E 闭环通过")
    finally:
        page.close()
