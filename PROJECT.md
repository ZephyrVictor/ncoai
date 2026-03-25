# LLM 竞赛平台当前设计与实施基线

## 0. 文档目的

本文档不再描述“理想化全量系统”，而是用于冻结当前仓库的真实实施基线，明确：

1. 当前已经落地的能力与系统边界。
2. 已经确认的业务口径，避免产品和实现继续漂移。
3. 尚未完成但必须继续分析的主题。
4. 后续阶段的优先级和验收方向。

---

## 1. 当前真实状态

当前仓库已经不是“只有 LiteLLM + vLLM 的最小链路”，而是包含以下主路径：

1. 选手聊天端：`apps/chainlit`
2. 管理后台：`apps/admin`
3. 主数据与账本：PostgreSQL
4. 热路径与限流缓存：Redis
5. 推理网关：LiteLLM
6. 推理引擎：vLLM

当前已经实现的核心能力：

1. 管理员登录。
2. 选手用户管理与 CSV 导入。
3. 单个全局 token 策略管理。
4. Chat 端 token 展示。
5. 会话与用户额度预检、预占、结算。
6. PostgreSQL 账本 + Redis 热缓存联动。

当前尚未纳入本期主线交付的能力：

1. 统一排队调度器。
2. QPS 500 级别压测验收。
3. 监控大屏正式页。
4. 自动重启与自愈闭环。
5. API Key 管理闭环。

---

## 2. 已冻结的业务口径

### 2.1 角色与登录

当前只有两类角色：

1. 管理员
2. 选手用户

当前登录口径：

1. 管理员通过独立后台账号登录 `apps/admin`。
2. 选手通过 `managed_users` 用户名密码登录 Chat。
3. 当前系统没有把“每个选手一个可操作 API Key”作为主交互模型。

说明：

1. “用户 API Key”保留为未来可选内部映射能力。
2. 本期的真实身份模型是“后台用户管理 + 前端密码登录”。

### 2.2 模型策略

当前模型口径：

1. 单模型运行，主路径为 LiteLLM -> vLLM。
2. 默认单会话累计额度上限为 `8192 token`。
3. 当前会话额度不是后台可编辑项，属于系统运行参数。
4. 思考模式是否开启，取决于当前应用配置。

### 2.3 配额策略

当前配额口径已经收敛为：

1. 只允许一个全局配额策略。
2. 全局策略字段为：
   - `total_token_limit`
   - `window_start`
   - `window_end`
   - `is_active`
   - `policy_version`
3. 默认用户总额度为 `100000 token`。
4. 管理员可修改用户总额度和生效时间窗。

当前不再支持：

1. 多条并存策略。
2. 用户级覆盖策略。
3. 在后台修改单会话 `8192` 上限。

### 2.4 统计口径

当前 token 统计口径必须严格一致：

1. `request_input_tokens`：本次请求输入 token。
2. `request_output_tokens`：本次请求输出 token。
3. `session_total_tokens`：同一 thread 累计已结算 token。
4. `user_window_tokens`：当前全局时间窗内，该用户所有已结算 assistant 回复的累计 token。

已冻结规则：

1. 用户总额度只统计 `quota_usage_ledger.status='settled'` 且 `settled_at` 落在当前窗口内的记录。
2. 正在进行中的请求会形成 `reserved_tokens`，用于下一次请求的准入判断。
3. 准入判断看的是“已使用 + 已预占”，不是只看已结算值。
4. 删除会话、线程、消息展示数据，不返还已消耗额度。
5. 额度正确性的最终来源是 PostgreSQL 账本，不是前端状态。

---

## 3. 当前系统架构

### 3.1 主链路

```text
[Admin UI]
    |
    v
[apps/admin] ------------------------------+
    |                                      |
    v                                      v
[PostgreSQL] <----------------------> [Redis]
    ^                                      ^
    |                                      |
[apps/chainlit] -> [QuotaService] ---------+
    |
    v
[LiteLLM]
    |
    v
[vLLM]
```

### 3.2 组件职责

1. `apps/chainlit`
   - 提供聊天 UI。
   - 管理选手登录。
   - 在发送前调用配额预检。
   - 在回复完成后结算 token 并更新前端展示。
2. `apps/admin`
   - 提供管理员登录。
   - 提供用户 CRUD、CSV 导入、全局配额配置。
   - 通过管理接口修改全局策略并同步 Redis 缓存。
3. PostgreSQL
   - 保存用户、策略、账本、汇总、Chainlit 会话数据。
4. Redis
   - 作为额度热路径主读源。
   - 保存全局策略缓存、用户热汇总、会话热汇总、请求预占信息。
5. LiteLLM
   - 统一模型调用入口。
6. vLLM
   - 执行推理。

### 3.3 当前边界

当前系统边界是：

1. Chat 与 Admin 已经落地。
2. 配额控制已进入主链路。
3. 监控、自愈、统一排队尚未进入当前交付闭环。

---

## 4. 当前请求生命周期

### 4.1 聊天主流程

1. 选手登录 Chat。
2. 页面首次加载获取当前用户额度摘要。
3. 用户发送消息前，前端会再获取一次最新额度摘要。
4. 后端基于 Redis 热数据执行预检与原子预占。
5. 预检通过后，请求进入 LiteLLM，再转发到 vLLM。
6. 流式回复完成后，优先使用模型 usage；缺失时回退到本地 tokenizer 统计。
7. 后端把本次结果结算到：
   - `quota_usage_ledger`
   - `quota_thread_summary`
   - `quota_user_summary`
   - Redis 热缓存
8. assistant message metadata 回写前端，用于刷新：
   - 本次请求 token
   - 当前会话累计 token
   - 当前用户累计 token

### 4.2 阻断场景

当前 Chat 端需要明确处理 3 类阻断：

1. `当前对话额度已用尽`
2. `用户总额度已用尽`
3. `当前不在配额生效时间内`

前后端职责分工：

1. 前端负责展示和提交前拦截。
2. 后端 `reserve()` 负责最终硬拦截。
3. 即使前端状态陈旧，也不能绕过后端配额判断。

---

## 5. 配额与计量设计

### 5.1 当前默认值

1. 单会话累计上限：`8192 token`
2. 单用户时间窗累计上限：`100000 token`
3. 单次请求输出上限：受模型运行参数控制

### 5.2 预检规则

当前请求准入至少受以下约束：

1. 本次输入 token + 本次允许输出 token 不得超过模型上下文限制。
2. 当前 thread 的累计 token 不得超过 `8192`。
3. 当前用户在当前窗口内的 `used_tokens + reserved_tokens` 不得超过全局总额度。
4. 当前时间必须落在全局策略时间窗内，且策略为启用状态。

### 5.3 结算规则

1. 预占成功后，先写请求级 reservation。
2. 请求完成后按实际 token 结算。
3. 流式中断时，只结算已生成部分。
4. 未使用完的预占额度必须释放。
5. 账本记录一旦 `settled`，后续不因删除会话而回退。

### 5.4 显示规则

Chat 前端当前展示三段 token 数据：

1. 每条 assistant message 下显示本次请求 token。
2. 输入区显示当前会话累计 token。
3. 右上角用户区显示当前用户时间窗累计 token / 总额度。

---

## 6. 数据模型基线

### 6.1 当前主表

当前业务主数据已经收敛为：

1. `admin_accounts`
2. `managed_users`
3. `quota_policies`
4. `quota_usage_ledger`
5. `quota_thread_summary`
6. `quota_user_summary`

### 6.2 当前会话存储

Chat 内容与会话元数据由 Chainlit 数据层保存，重点包括：

1. `threads`
2. `steps`
3. `threads.metadata.token_usage_summary`

### 6.3 当前账本约束

1. `quota_usage_ledger` 是最终正确性来源。
2. `quota_thread_summary` 是 thread 维度热汇总。
3. `quota_user_summary` 是用户维度、当前策略窗口下的热汇总快照。
4. `quota_user_summary` 必须携带：
   - `window_start`
   - `window_end`
   - `policy_version`
5. 只要窗口或 `policy_version` 不一致，缓存必须失效并重建。

---

## 7. Redis 设计基线

当前 Redis 热路径 key 已经收敛为：

1. `quota:config:global`
2. `quota:user:{user_identifier}`
3. `quota:thread:{thread_id}`
4. `quota:req:{request_id}`

当前 Redis 设计原则：

1. 正常聊天热路径优先读 Redis，不做 SQL 聚合。
2. 仅在以下场景回源 PostgreSQL：
   - Redis miss
   - `policy_version` 过期
   - 窗口不一致
   - 服务重启后的冷启动重建
3. 配额相关原子操作走 Lua 或等价的原子更新路径。

---

## 8. 当前接口基线

### 8.1 Chat 侧

当前并没有对外暴露独立业务版 `/api/chat/completions` 作为正式产品接口；聊天主交互由 Chainlit 承载。

当前已明确的配额接口：

1. `GET /chat/api/quota/me`
   - 返回当前用户额度摘要
   - 数据源为 Redis-first，必要时回源 PostgreSQL 重建

### 8.2 Admin 侧

当前后台已落地接口包括：

1. `POST /admin/api/auth/login`
2. `POST /admin/api/auth/logout`
3. `GET /admin/api/users`
4. `POST /admin/api/users`
5. `GET /admin/api/users/{identifier}`
6. `PATCH /admin/api/users/{identifier}`
7. `DELETE /admin/api/users/{identifier}`
8. `POST /admin/api/users/import`
9. `GET /admin/api/users/{identifier}/usage`
10. `GET /admin/api/quota-policy`
11. `PUT /admin/api/quota-policy`

当前预留但未纳入本期交付验收：

1. `GET /admin/api/monitoring/summary`
2. `GET /admin/api/monitoring/alerts`

---

## 9. 当前界面基线

### 9.1 Chat 界面

当前 Chat 已落地的功能：

1. 用户登录。
2. 发起对话。
3. assistant 回复下展示本次 token。
4. footer 展示当前会话累计 token。
5. 右上角展示用户累计 token / 总额度。
6. 超限后禁用输入框与发送按钮。

### 9.2 管理后台

当前后台已落地页面：

1. 管理员登录页
2. 用户列表页
3. 用户创建/编辑能力
4. CSV 导入能力
5. 单个全局 quota 配置页

当前后台未落地页面：

1. 队列管理页
2. 会话实时观察页
3. 监控大屏
4. 自动重启运维页

---

## 10. 已完成与未完成

### 10.1 当前已完成

1. 管理员账号体系。
2. 选手用户 CRUD。
3. CSV 批量导入。
4. 单全局配额策略页。
5. Chat 端 token 三段显示。
6. PostgreSQL 账本与汇总表。
7. Redis-first 配额热路径基础实现。

### 10.2 当前未完成

1. 单会话 `8192` 边界的完整自动化验收仍需继续压实。
2. 把用户总额度临时调到 `20000` 后的跨多会话累计验收仍需继续压实。
3. QPS 500 场景的压测与性能结论还没有形成正式报告。
4. 统一排队调度还未进入主链路。
5. 监控、自愈、告警仍未进入本期交付。

---

## 11. 当前最需要继续分析的主题

### 11.1 配额精确定义

需要继续冻结以下细节，避免实现继续摇摆：

1. `blocked` 的判定是否仅看 `used_tokens`，还是看 `used_tokens + reserved_tokens`。
2. 当前用户额度摘要中，前端优先展示哪个值。
3. 当用户还未真正结算到总额度，但剩余额度已不足以支撑下一次最小请求时，产品提示文案如何定义。

### 11.2 QPS 500 热路径

需要专门输出工程级设计，而不是只停留在原则层面：

1. Redis key TTL
2. Lua 脚本幂等性
3. 冷启动回填策略
4. 失败请求回滚策略
5. 预占泄漏回收机制
6. PostgreSQL 索引与写放大评估

### 11.3 删除会话不返还额度

这条业务规则已经确认，但还要补充完整工程定义：

1. 删除哪些展示数据
2. 保留哪些账本数据
3. 历史归档和清理任务如何避免误删 quota 账本
4. admin 与 chat 两端如何保证口径一致

### 11.4 身份模型统一

当前系统以“后台用户 + Chat 登录”为主，但文档历史上仍残留 API Key 方案。后续需要二选一：

1. API Key 只保留为内部实现细节
2. 还是继续做成后台可管理能力

在此之前，不再继续扩展 API Key 页面和数据模型。

### 11.5 排队与调度

高负载保护仍需单独分析：

1. 请求在哪里入队
2. 用户公平如何定义
3. 和 LiteLLM / vLLM 的并发控制如何协同
4. 前端如何展示排队态

### 11.6 监控与自愈

这部分不是当前 P0 主线，但后续必须单列实施：

1. 指标采集链路
2. 告警通道
3. 自动重启策略
4. 监控大屏展示口径

---

## 12. 后续阶段建议

### 阶段 A：配额闭环压实

1. 完成 `8192` 单会话上限实测验收。
2. 完成 `20000` 总额度跨多会话实测验收。
3. 完成“删除会话不返还额度”的自动化验收。
4. 输出 Redis-first 配额链路的正式验收结论。

### 阶段 B：高负载保护

1. 完成 QPS 500 场景的压测脚本与报告。
2. 明确统一排队与公平调度方案。
3. 决定是否需要队列页和排队态前端展示。

### 阶段 C：监控与运维

1. 接入 Prometheus / Grafana / Alertmanager。
2. 落地 CPU / GPU / 服务状态监控。
3. 补全告警与自动重启闭环。

---

## 13. 当前结论

当前项目已经从“最小推理链路”进入“业务闭环建设”阶段，当前主线应当保持收敛：

1. 先把配额、用户、聊天展示三条主链路完全打透。
2. 再处理 QPS 500 下的热路径与调度问题。
3. 最后补监控、自愈、运维面板。

在下一轮文档更新前，本文件以“当前已实现基线 + 待分析项”作为唯一参考，不再沿用旧的多策略、多角色 API Key 主方案。
