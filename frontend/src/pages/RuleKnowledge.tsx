import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, newIdemKey } from "../api/client";
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

interface ImportResult {
  document_id: string;
  title: string;
  chunk_count: number;
  embedding_status: "vector_indexed" | "keyword_only" | "already_indexed";
  already_exists: boolean;
}

const DOC_TYPES: Array<{ value: string; label: string }> = [
  { value: "replenishment_policy", label: "补货策略资料" },
  { value: "safety_stock", label: "安全库存资料" },
  { value: "warehouse_rule", label: "仓库作业资料" },
  { value: "supplier_constraint", label: "供应商约束资料" },
  { value: "receiving_sop", label: "收货 SOP 资料" },
];

export function RuleKnowledge({ roles }: { roles: string[] }) {
  const [docs, setDocs] = useState<DocumentDto[]>([]);
  const [rules, setRules] = useState<RuleDto[]>([]);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Array<{ source_chunk_id: string; document_title: string; content: string; score: number }>>([]);
  const [error, setError] = useState("");
  const [title, setTitle] = useState("");
  const [docType, setDocType] = useState(DOC_TYPES[0].value);
  const [content, setContent] = useState("");
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const canImport = roles.includes("admin");

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

  const importDocument = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!title.trim() || !content.trim() || importing) return;
    setError("");
    setImportResult(null);
    setImporting(true);
    try {
      const result = await api.post<ImportResult>(
        "/api/v1/knowledge/documents/import",
        {
          title: title.trim(),
          doc_type: docType,
          content: content.trim(),
          effective_from: new Date().toISOString().slice(0, 10),
        },
        newIdemKey()
      );
      setImportResult(result);
      setTitle("");
      setContent("");
      load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setImporting(false);
    }
  };

  return (
    <div className="card">
      <h2>规则知识库</h2>
      {canImport ? (
        <form className="knowledge-import" onSubmit={importDocument}>
          <h3>导入本地资料</h3>
          <div className="knowledge-import-grid">
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="资料标题"
              maxLength={256}
              required
            />
            <select value={docType} onChange={(e) => setDocType(e.target.value)}>
              {DOC_TYPES.map((type) => (
                <option key={type.value} value={type.value}>
                  {type.label}
                </option>
              ))}
            </select>
          </div>
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="粘贴 Markdown 或 TXT 内容（最多 80,000 字符）"
            maxLength={80_000}
            required
          />
          <div className="knowledge-import-actions">
            <button type="submit" disabled={importing || !title.trim() || !content.trim()}>
              {importing ? "导入中…" : "导入并建立检索索引"}
            </button>
            <span className="hint">资料仅供检索引用，不会自动修改补货计算规则。</span>
          </div>
        </form>
      ) : (
        <p className="hint">切换到管理员账号后，可导入本地资料供 Agent 检索引用。</p>
      )}
      {importResult && (
        <p className="success-message">
          {importResult.already_exists ? "资料已存在，已复用原索引" : "资料导入完成"}：{importResult.title}，共 {importResult.chunk_count} 个检索片段（
          {importResult.embedding_status === "vector_indexed" ? "向量 + 关键词" : "关键词"}）。
        </p>
      )}
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
