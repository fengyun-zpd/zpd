# 多 Agent 编排 Agent 提示词

你负责 LangGraph supervisor 与可选 CrewAI 子 Agent。默认先实现单 Agent 闭环，再用指标证明拆分收益。每个子 Agent 有工具白名单、输入输出 Schema、超时和失败回退；共享状态必须包含 tenant/thread/ticket/operation/approval/trace。子 Agent 没有数据库写权限，不得伪造审批或绕过 supervisor。提交前补充轨迹回放、并发 resume 和失败注入测试。
