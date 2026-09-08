"""规则知识库的管理员资料导入 e2e。"""

from __future__ import annotations

import os
import uuid

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


def test_admin_can_import_and_search_local_evidence(browser):
    page = browser.new_page()
    try:
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.click("button:has-text('规则知识库')")
        page.wait_for_selector("input[placeholder='资料标题']", timeout=15000)
        title = f"E2E 本地资料导入验证 {uuid.uuid4().hex[:8]}"
        page.fill("input[placeholder='资料标题']", title)
        page.fill(
            "textarea[placeholder^='粘贴 Markdown']",
            "# E2E 证据\n\n本地资料导入后，应能按关键词检索并显示真实来源。",
        )
        page.click("button:has-text('导入并建立检索索引')")
        page.wait_for_function(
            "() => document.body.textContent.includes('资料导入完成') || document.body.textContent.includes('资料已存在')",
            timeout=30000,
        )
        page.fill("input[placeholder^='检索规则证据']", "本地资料 导入")
        page.keyboard.press("Enter")
        page.wait_for_function(f"() => document.body.textContent.includes({title!r})", timeout=15000)
    finally:
        page.close()
