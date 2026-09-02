"""V1 收口功能优化 e2e：助手页错误可见性与状态展示（需求 3.1 / 9）。

验证：缺参澄清显示需补充字段徽标；对话消息区分 OFFLINE/真实 LLM；
阻断/防重场景显示"已阻断"徽标与下一步动作（若触发）。
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


def _send_and_wait(page, text: str, expect_in: str) -> None:
    page.fill("input[placeholder='输入补货请求…']", text)
    page.keyboard.press("Enter")
    page.wait_for_function(
        f"() => {{ const t = document.querySelector('.chat-log'); return t && t.textContent.includes('{expect_in}'); }}",
        timeout=60000,
    )


def test_assistant_clarifies_missing_params(browser):
    """缺参请求（仅"帮我补货"）应显示"需要补充参数"徽标并列出缺失字段。"""
    page = browser.new_page()
    try:
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        _send_and_wait(page, "帮我补货", "还需要补充")
        # 徽标：需要补充参数
        page.wait_for_selector(".badge.status-pending_approval", timeout=15000)
        # 缺失字段详情
        detail = page.text_content(".chat-detail") or ""
        assert "仓库" in detail and ("SKU" in detail or "窗口" in detail)
        print("缺参澄清展示 OK")
    finally:
        page.close()


def test_assistant_blocks_duplicate_suggestion(browser):
    """同一 SKU 重复发起（已有活动建议）应显示"已阻断"徽标与下一步动作。

    容器环境 Beat 可能已生成活动建议：首次对话即防重（正确）。测试容忍：
    首次生成草稿后再发触发防重，或首次直接防重。
    """
    page = browser.new_page()
    try:
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.fill("input[placeholder='输入补货请求…']", "帮我检查华东仓未来两周需要补货的紧固件")
        page.keyboard.press("Enter")
        # 首答：生成草稿 或 直接防重（两种情况都继续）
        page.wait_for_function(
            "() => { const t = document.querySelector('.chat-log'); return t && (t.textContent.includes('已生成补货草稿') || t.textContent.includes('无法生成草稿') || t.textContent.includes('已存在活动建议')); }",
            timeout=60000,
        )
        first = page.text_content(".chat-log") or ""
        if "已生成补货草稿" in first:
            # 再次发起触发防重
            page.fill("input[placeholder='输入补货请求…']", "帮我检查华东仓未来两周需要补货的紧固件")
            page.keyboard.press("Enter")
            page.wait_for_function(
                "() => { const t = document.querySelector('.chat-log'); return t && (t.textContent.includes('无法生成草稿') || t.textContent.includes('已存在活动建议')); }",
                timeout=60000,
            )
        # 阻断徽标或明确错误文本
        blocked = page.locator(".badge.status-blocked").count() > 0
        text = page.text_content(".chat-log") or ""
        assert blocked or "已存在活动建议" in text or "无法生成草稿" in text
        # 下一步动作存在（chat-next-step 或阻断详情）
        next_step = page.locator(".chat-next-step").count()
        assert next_step > 0
        print("防重阻断展示 OK")
    finally:
        page.close()


def test_assistant_shows_mode_badge(browser):
    """助手消息应显示 OFFLINE 或真实 LLM 徽标（页面明确区分对话模式）。"""
    page = browser.new_page()
    try:
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        _send_and_wait(page, "帮我补货", "还需要补充")
        badge = page.locator(".badge.status-offline, .badge.status-llm")
        assert badge.count() >= 1
        print("对话模式徽标 OK")
    finally:
        page.close()
