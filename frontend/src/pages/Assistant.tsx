import { useEffect, useState } from "react";
import { api, getActor } from "../api/client";
import { formatErrorCode, shortId } from "../ui/format";

interface BlockedLine {
  product_id?: string;
  blocked_code?: string;
  blocked_reason?: string;
  next_step?: string;
  plan_id?: string;
  plan_status?: string | null;
  order_qty?: number | null;
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  offline?: boolean;
  outcome?: string; // draft_created / blocked / clarified / answered / approved
  blocked_lines?: BlockedLine[];
  missing_params?: string[];
  error_code?: string;
}

// 按演示用户缓存会话，避免 StrictMode 双挂载竞态和切换用户串线。
const threadPromises = new Map<string, Promise<string>>();
function getThread(actorId: string): Promise<string> {
  let promise = threadPromises.get(actorId);
  if (!promise) {
    promise = api
      .post<{ thread_id: string }>("/api/v1/conversations")
      .then((data) => data.thread_id);
    threadPromises.set(actorId, promise);
  }
  return promise;
}

// 读取 SSE 流并按事件回调；返回最后一个事件 id（供断线续传定位游标）
async function readStream(
  resp: Response,
  onEvent: (event: string, data: any) => void,
  initialLastId: string | null,
): Promise<string | null> {
  if (!resp.ok || !resp.body) throw new Error("SSE 连接失败");
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastId = initialLastId;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      if (!part.trim()) continue;
      const lines = part.split("\n");
      const idLine = lines.find((l) => l.startsWith("id: "));
      const eventLine = lines.find((l) => l.startsWith("event: "));
      const dataLine = lines.find((l) => l.startsWith("data: "));
      if (!dataLine) continue;
      const event = eventLine ? eventLine.slice(7) : "message";
      if (idLine) lastId = idLine.slice(4);
      let data: any;
      try {
        data = JSON.parse(dataLine.slice(6));
      } catch {
        continue;
      }
      onEvent(event, data);
    }
  }
  return lastId;
}

const MISSING_LABEL: Record<string, string> = {
  warehouse: "仓库",
  warehouse_id: "仓库",
  product: "SKU 或商品范围",
  products: "SKU 或商品范围",
  requested_window: "规划周期（7/14/30 天）",
};

// 可直接点击的示例请求（业务自然语言；后端 V1 离线/LLM 路径均可处理）
const EXAMPLES: string[] = [
  "帮我检查华东仓未来14天需要补货的紧固件",
  "帮我检查华南仓 SKU-E08 未来7天库存",
];

export function Assistant({
  onDraft,
  onOpenExistingPlan,
}: {
  onDraft?: () => void;
  onOpenExistingPlan?: (line: BlockedLine) => void;
}) {
  const [threadId, setThreadId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const actorId = getActor();
    getThread(actorId).then((id) => {
      if (!cancelled) setThreadId(id);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const send = async (textOverride?: string) => {
    const text = (textOverride ?? input).trim();
    if (!text || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text }]);
    setBusy(true);

    // 线程可能尚未就绪：按需等待（模块级单例，只创建一次）
    const actorId = getActor();
    const tid = await getThread(actorId);
    if (tid !== threadId) setThreadId(tid);

    let assistantText = "";
    let lastMeta: Partial<ChatMessage> = {};
    let lastEventId: string | null = null;
    let reachedDone = false;

    const handleEvent = (event: string, data: any) => {
      if (event === "draft_created") {
        assistantText = `已生成补货草稿（计划 ${shortId(data.plan_id)}）并提交待审批。`;
        lastMeta = { outcome: "draft_created" };
        onDraft?.();
      } else if (event === "interrupted") {
        assistantText += "\n[会话已暂停，等待审批人处理]";
      } else if (event === "message") {
        assistantText = data.content;
        lastMeta = {
          offline: data.offline,
          outcome: data.outcome,
          blocked_lines: data.blocked_lines,
          missing_params: data.missing_params,
        };
      } else if (event === "error") {
        assistantText = data.message || "发生错误";
        lastMeta = { error_code: data.code, outcome: "blocked" };
      } else if (event === "done") {
        reachedDone = true;
      }
    };

    try {
      // 首次请求 + 断线后携 Last-Event-ID 续传（最多 3 次补流）
      for (let attempt = 0; attempt < 4; attempt++) {
        const headers: Record<string, string> = {
          "Content-Type": "application/json",
          "X-Actor-Id": actorId,
        };
        if (lastEventId) headers["Last-Event-ID"] = lastEventId;
        const resp = await fetch(`/api/v1/conversations/${tid}/messages`, {
          method: "POST",
          headers,
          body: JSON.stringify({ content: text }),
        });
        try {
          lastEventId = await readStream(resp, handleEvent, lastEventId);
          if (reachedDone) break;
          if (!lastEventId) break; // 首帧都未收到，无法续传
          // 未收 done 即流结束：携游标续传，重放未收到的进度
        } catch (err) {
          if (!lastEventId) throw err; // 无进度可续，直接失败
          // 否则下一轮循环携 Last-Event-ID 续传
        }
      }
    } catch (err) {
      setMessages((m) => [
        ...m,
        { role: "assistant", content: `请求失败：${(err as Error).message}`, error_code: "NETWORK_ERROR" },
      ]);
      setBusy(false);
      return;
    }

    if (!assistantText.trim()) assistantText = "（无回复）";
    setMessages((m) => [
      ...m,
      {
        role: "assistant",
        content: assistantText,
        offline: lastMeta.offline,
        outcome: lastMeta.outcome,
        blocked_lines: lastMeta.blocked_lines,
        missing_params: lastMeta.missing_params,
        error_code: lastMeta.error_code,
      },
    ]);
    setBusy(false);
  };

  const renderMeta = (m: ChatMessage) => {
    if (!m.outcome && !m.offline && !m.error_code) return null;
    return (
      <div className="chat-meta">
        {m.offline !== undefined && (
          <span className={`badge ${m.offline ? "status-offline" : "status-llm"}`}>
            {m.offline ? "OFFLINE 演示模式" : "真实 LLM"}
          </span>
        )}
        {m.outcome === "draft_created" && <span className="badge status-success">已生成草稿</span>}
        {m.outcome === "clarified" && <span className="badge status-pending_approval">需要补充参数</span>}
        {m.outcome === "blocked" && <span className="badge status-blocked">已阻断</span>}
        {m.error_code && (
          <span className="badge status-blocked" title={m.error_code}>
            {formatErrorCode(m.error_code)}
          </span>
        )}
        {m.missing_params && m.missing_params.length > 0 && (
          <div className="chat-detail">
            需要补充：{m.missing_params.map((p) => MISSING_LABEL[p] ?? p).join("、")}
          </div>
        )}
        {m.blocked_lines && m.blocked_lines.length > 0 && (
          <div className="chat-detail">
            {m.blocked_lines.map((b, i) => (
              <div key={i} className="chat-block-line">
                <div>
                  <strong>{b.product_id || "请求"}</strong>
                  {b.blocked_code ? ` [${formatErrorCode(b.blocked_code)}]` : ""}：{b.blocked_reason}
                </div>
                {b.next_step && <div className="chat-next-step">下一步：{b.next_step}</div>}
                {b.blocked_code === "ACTIVE_REPLENISHMENT_EXISTS" && b.plan_id && onOpenExistingPlan && (
                  <button type="button" className="secondary-action" onClick={() => onOpenExistingPlan(b)}>
                    {b.plan_status === "pending_approval"
                      ? "查看待审批建议"
                      : b.plan_status === "approved" && (b.order_qty ?? 0) > 0
                        ? "前往采购单建单"
                        : "查看现有补货计划"}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="card assistant">
      <h2>补货助手</h2>
      <p className="hint">
        输入补货或库存查询请求，Agent 会澄清必要参数并生成待审批草稿。可点击以下示例直接体验：
      </p>
      <div className="example-chips">
        {EXAMPLES.map((ex) => (
          <button
            key={ex}
            type="button"
            className="example-chip"
            disabled={busy}
            onClick={() => send(ex)}
          >
            {ex}
          </button>
        ))}
      </div>
      <p className="hint">
        如需指定仓库、SKU/商品分类与规划周期（7/14/30 天），可直接在请求中说明；预算/成本约束暂不参与 V1 计算。
      </p>
      <div className="chat-log">
        {messages.map((m, i) => (
          <div key={i} className={`chat-msg ${m.role}`}>
            <span className="chat-role">{m.role === "user" ? "你" : "助手"}</span>
            <pre className="chat-content">{m.content}</pre>
            {m.role === "assistant" && renderMeta(m)}
          </div>
        ))}
      </div>
      <div className="chat-input">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="输入补货请求…"
          disabled={busy}
        />
        <button onClick={() => send()} disabled={busy || !input.trim()}>
          {busy ? "处理中…" : "发送"}
        </button>
      </div>
    </div>
  );
}
