import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { formatBoolean, formatDecimal, formatRuleType, formatScope, shortId } from "../ui/format";

interface DocumentDto {
  document_id: string;
  title: string;
  doc_type: string;
  version: number;
  enabled: boolean;
  chunks: Array<{ chunk_id: string; seq_no: number; content: string }>;
}

interface RuleDto {
  rule_id: string;
  code: string;
  name: string;
  rule_type: string;
  scope: string;
  safety_stock?: string | null;
  review_period_days?: number | null;
  version: number;
  enabled: boolean;
  source_document_id: string;
  source_chunk_id: string;
}

export function RuleKnowledge() {
  const [docs, setDocs] = useState<DocumentDto[]>([]);
  const [rules, setRules] = useState<RuleDto[]>([]);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Array<{ source_chunk_id: string; document_title: string; content: string; score: number }>>([]);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api
      .get<DocumentDto[]>("/api/v1/knowledge/documents")
      .then(setDocs)
      .catch((e) => setError((e as Error).message));
    api
      .get<RuleDto[]>("/api/v1/rules")
      .then(setRules)
      .catch((e) => setError((e as Error).message));
  }, []);

  useEffect(load, [load]);

  const search = async () => {
    if (!query.trim()) return;
    setError("");
    try {
      const result = await api.get<typeof hits>(
        `/api/v1/knowledge/search?q=${encodeURIComponent(query.trim())}&top_k=6`
      );
      setHits(result);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="card">
      <h2>规则知识库（只读）</h2>
      <div className="chat-input">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="检索规则证据，例如：安全库存 华东仓"
        />
        <button onClick={search}>检索</button>
      </div>
      {error && <p className="error">{error}</p>}
      {hits.length > 0 && (
        <div className="evidence">
          <h3>检索结果（引用必须映射到真实启用分块）</h3>
          {hits.map((h, i) => (
            <div key={i} className="hit">
              <span className="hit-title">
                [{i + 1}] {h.document_title} · <span title={h.source_chunk_id}>{shortId(h.source_chunk_id, 12)}</span>
              </span>
              <pre>{h.content}</pre>
            </div>
          ))}
        </div>
      )}
      <h3>结构化规则（已校验，可进计算）</h3>
      <table>
        <thead>
          <tr>
            <th>编码</th>
            <th>类型</th>
            <th>作用域</th>
            <th>安全库存</th>
            <th>复查周期</th>
            <th>版本</th>
            <th>来源</th>
          </tr>
        </thead>
        <tbody>
          {rules.map((r) => (
            <tr key={r.rule_id}>
              <td>{r.code}</td>
              <td>{formatRuleType(r.rule_type)}</td>
              <td>{formatScope(r.scope)}</td>
              <td>{formatDecimal(r.safety_stock)}</td>
              <td>{r.review_period_days ?? "-"}</td>
              <td>v{r.version}</td>
              <td>
                <code title={r.source_chunk_id}>{shortId(r.source_chunk_id, 12)}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>知识文档</h3>
      {docs.map((d) => (
        <details key={d.document_id} className="doc">
            <summary>
            {d.title}（v{d.version}，{formatBoolean(d.enabled)}）
          </summary>
          {d.chunks.map((c) => (
            <pre key={c.chunk_id} className="doc-chunk">
              [{c.seq_no}] {c.content}
            </pre>
          ))}
        </details>
      ))}
    </div>
  );
}
