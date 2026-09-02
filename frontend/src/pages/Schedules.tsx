import { useCallback, useEffect, useState } from "react";
import { api, newIdemKey } from "../api/client";
import { formatBoolean, formatRole, shortId } from "../ui/format";

interface ScheduleDto {
  schedule_id: string;
  name: string;
  cron_expr: string;
  timezone: string;
  enabled: boolean;
  default_window: number;
}

export function Schedules({ roles }: { roles: string[] }) {
  const [schedules, setSchedules] = useState<ScheduleDto[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const canRun = roles.includes("admin");

  const load = useCallback(() => {
    api
      .get<ScheduleDto[]>("/api/v1/schedules")
      .then(setSchedules)
      .catch((e) => setError((e as Error).message));
  }, []);

  useEffect(load, [load]);

  const runNow = async (id: string) => {
    setError("");
    setNotice("");
    try {
      const result = await api.post<{ execution_id: string }>(
        `/api/v1/schedules/${id}/run`,
        {},
        newIdemKey()
      );
      setNotice(`已触发执行：${shortId(result.execution_id)}`);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="card">
      <h2>定时任务</h2>
      <p className="hint">
        Celery Beat 每 30 秒读取数据库配置并分发到期扫描（V1 最低周期 1 分钟）。
        {canRun
          ? " 当前用户（管理员）可执行“立即执行”。"
          : ` 当前用户（${roles.map(formatRole).join("/") || "未分配角色"}）只能查看；“立即执行”仅管理员可用。`}
      </p>
      {error && <p className="error">{error}</p>}
      {notice && <p className="hint">{notice}</p>}
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>Cron</th>
            <th>时区</th>
            <th>默认窗口</th>
            <th>启用</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {schedules.length === 0 && (
            <tr>
              <td colSpan={6} className="empty-cell">当前没有定时任务，请由管理员通过 API 配置。</td>
            </tr>
          )}
          {schedules.map((s) => (
            <tr key={s.schedule_id}>
              <td>{s.name}</td>
              <td>
                <code>{s.cron_expr}</code>
              </td>
              <td>{s.timezone}</td>
              <td>{s.default_window} 天</td>
              <td>{formatBoolean(s.enabled)}</td>
              <td>
                <button
                  onClick={() => runNow(s.schedule_id)}
                  disabled={!canRun}
                  title={canRun ? "立即执行一次扫描（后台异步）" : "仅管理员可执行“立即执行”；操作员为只读查看"}
                >
                  立即执行
                </button>
                {!canRun && <span className="hint"> · 仅管理员</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
