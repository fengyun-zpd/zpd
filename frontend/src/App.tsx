import { useEffect, useState } from "react";
import { api, getActor, setActor, UserDto } from "./api/client";
import { ActorSwitcher } from "./pages/ActorSwitcher";
import { Assistant } from "./pages/Assistant";
import { Workbench } from "./pages/Workbench";
import { ApprovalBox } from "./pages/ApprovalBox";
import { PurchaseOrders } from "./pages/PurchaseOrders";
import { Schedules } from "./pages/Schedules";
import { Executions } from "./pages/Executions";
import { RuleKnowledge } from "./pages/RuleKnowledge";
import { DataView } from "./pages/DataView";

type PageKey =
  | "assistant"
  | "workbench"
  | "approval"
  | "purchase"
  | "schedules"
  | "executions"
  | "knowledge"
  | "data";

const NAV: Array<{ key: PageKey; label: string }> = [
  { key: "assistant", label: "补货助手" },
  { key: "workbench", label: "补货工作台" },
  { key: "approval", label: "审批箱" },
  { key: "purchase", label: "采购单" },
  { key: "schedules", label: "定时任务" },
  { key: "executions", label: "执行记录" },
  { key: "knowledge", label: "规则知识库" },
  { key: "data", label: "库存与数据" },
];

export default function App() {
  const [page, setPage] = useState<PageKey>("assistant");
  const [approvalPlanId, setApprovalPlanId] = useState<string>("");
  const [users, setUsers] = useState<UserDto[]>([]);
  const [actor, setActorState] = useState<string>(getActor());

  useEffect(() => {
    api
      .get<UserDto[]>("/api/v1/users")
      .then(setUsers)
      .catch(() => setUsers([]));
  }, []);

  const switchActor = (id: string) => {
    setActor(id);
    setActorState(id);
  };

  const currentUser = users.find((u) => u.actor_id === actor);
  const currentRoles = currentUser ? currentUser.roles : [];

  const refresh = () => {
    setActorState(getActor());
  };

  const openPlanForApproval = (planId: string) => {
    setApprovalPlanId(planId);
    setPage("approval");
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>StockMind · 可审计智能补货助手</h1>
        <ActorSwitcher users={users} current={actor} onChange={switchActor} />
      </header>
      <nav className="nav">
        {NAV.map((item) => (
          <button
            key={item.key}
            className={page === item.key ? "nav-btn active" : "nav-btn"}
            onClick={() => setPage(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>
      <main className="content">
        {page === "assistant" && <Assistant onDraft={refresh} />}
        {page === "workbench" && <Workbench onSelectPlan={openPlanForApproval} />}
        {page === "approval" && <ApprovalBox onChanged={refresh} initialPlanId={approvalPlanId} />}
        {page === "purchase" && <PurchaseOrders />}
        {page === "schedules" && <Schedules roles={currentRoles} />}
        {page === "executions" && <Executions />}
        {page === "knowledge" && <RuleKnowledge />}
        {page === "data" && <DataView />}
      </main>
    </div>
  );
}
