"""健康检查：区分进程存活与 Agent 能力就绪状态。"""

from app.main import create_app


def test_health_and_ready_expose_agent_capability():
    app = create_app()
    endpoints = {route.path: route.endpoint for route in app.routes if hasattr(route, "path")}

    assert "/health" in endpoints
    assert "/ready" in endpoints
    health = endpoints["/health"]()
    assert health["status"] == "ok"
    assert health["capabilities"]["agent"] is True

    ready = endpoints["/ready"]()
    assert ready == {"status": "ready", "env": health["env"], "checks": {"agent": True}}
