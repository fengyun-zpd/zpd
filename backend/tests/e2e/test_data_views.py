"""V1 功能化 e2e：只读数据页（库存/供应商/告警）与工作台计划详情。

验证：库存查询可读、供应商列表展示、告警页、工作台计划详情含 SKU/阻断原因。
需前端（:3000）与后端（:8000）已启动；标记 e2e，不在 CI 主流程运行。
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


def test_data_view_inventory_query(browser):
    """库存与数据页：选中仓库/SKU 查询库存，应展示现存量/可用量。"""
    page = browser.new_page()
    try:
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.click("button:has-text('库存与数据')")
        page.wait_for_selector("button:has-text('查询库存')", timeout=15000)
        page.click("button:has-text('查询库存')")
        page.wait_for_function(
            "() => document.body.textContent.includes('现存量') && Array.from(document.querySelectorAll('table')).some(t => t.textContent.includes('可用量'))",
            timeout=15000,
        )
        body = page.text_content("body") or ""
        assert "现存量" in body and "可用量" in body
        print("库存查询展示 OK")
    finally:
        page.close()


def test_data_view_suppliers_list(browser):
    """供应商页：应列出种子供应商及其 SKU 关系。"""
    page = browser.new_page()
    try:
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.click("button:has-text('库存与数据')")
        page.wait_for_selector("button:has-text('供应商')", timeout=15000)
        page.click("button:has-text('供应商')")
        page.wait_for_function(
            "() => document.body.textContent.includes('SUP-001')",
            timeout=15000,
        )
        body = page.text_content("body") or ""
        assert "SUP-001" in body and "交期" in body
        print("供应商列表展示 OK")
    finally:
        page.close()


def test_data_view_alerts_tab(browser):
    """告警页：应可打开并展示告警状态（可能有空态）。"""
    page = browser.new_page()
    try:
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.click("button:has-text('库存与数据')")
        page.wait_for_selector("button:has-text('告警')", timeout=15000)
        page.click("button:has-text('告警')")
        page.wait_for_function(
            "() => document.body.textContent.includes('当前没有告警') || document.body.textContent.includes('阻断码')",
            timeout=15000,
        )
        print("告警页展示 OK")
    finally:
        page.close()


def test_workbench_plan_detail(browser):
    """补货工作台：点击查看详情应显示 SKU 明细与阻断原因列。"""
    page = browser.new_page()
    try:
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.click("button:has-text('补货工作台')")
        # 等待计划列表加载完成：否则点击后立即取按钮数会把"加载中"误判成"空态"（时序竞态）
        page.wait_for_function(
            "() => document.body.textContent.includes('查看详情') || document.body.textContent.includes('当前没有计划')",
            timeout=15000,
        )
        # 若存在计划，展开详情
        detail_btn = page.locator("button:has-text('查看详情')")
        if detail_btn.count() > 0:
            detail_btn.first.click()
            page.wait_for_function(
                "() => document.body.textContent.includes('SKU 明细')",
                timeout=15000,
            )
            body = page.text_content("body") or ""
            assert "SKU 明细" in body and ("建议数量" in body or "阻断" in body)
            print("工作台计划详情 OK")
        else:
            # 无计划时页面应显示空态提示
            page.wait_for_function(
                "() => document.body.textContent.includes('当前没有计划')",
                timeout=15000,
            )
            print("工作台空态提示 OK")
    finally:
        page.close()
