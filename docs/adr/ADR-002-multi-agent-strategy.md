# ADR-002：多 Agent 分阶段引入

## 状态

提议，V2 规划，未实现。

## 决策

LangGraph 保持主 supervisor 和业务状态机；CrewAI 仅用于可验证的领域协作子流程。先完成单 Agent 基线，再用任务完成率、工具正确率、P95、Token 和人工接管率证明拆分收益。

## 约束

子 Agent 不拥有数据库写权限；所有写命令经过领域服务、权限、幂等和审批。拆分失败时回退到 supervisor 的单 Agent 路径。
