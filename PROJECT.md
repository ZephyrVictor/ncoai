# LLM 竞赛平台完整系统设计书

## 0. 文档目标

本文档用于冻结并细化当前项目的系统设计，覆盖以下目标：

1. 明确业务范围、角色权限、系统边界。
2. 提供可落地的整体架构与模块职责。
3. 定义请求排队与动态调度方案，避免高负载导致推理服务退出。
4. 定义按用户维度的对话记录、状态管理、Token 限制与计量规则。
5. 定义三类界面能力：选手聊天界面、管理员控制台、监控大屏。
6. 定义故障自愈与告警闭环。

当前阶段只做系统架构与设计，不包含测试脚本与发布流程设计。

---

## 1. 需求澄清与冻结

### 1.1 角色与账号

1. 仅两类角色：
   - 竞赛选手
   - 管理员
2. 管理员为单独固定账号（单账号管理入口）。

### 1.2 认证与 API Key 策略

1. 每个选手都有一个独立 API Key。
2. 选手对 API Key 无感，选手仅通过 Chat 界面使用系统。
3. API Key 仅在系统内部用于网关调用与用户身份映射。
4. 管理员可在后台查看、重置、禁用选手 key（不对选手展示明文）。

### 1.3 模型策略

1. 当前仅部署单模型：qwen35b（位于 models 目录）。
2. 用户可选择是否开启思考模式（Think On/Off）。
3. 虽然当前单模型部署，但架构必须兼容未来多模型扩展。

### 1.4 并发与吞吐策略

1. 不采用降级方案，不自动切换到低阶模型。
2. 采用请求排队策略，系统通过动态调度平衡负载。
3. 目标是优先保证稳定性和公平性，避免高负载导致服务崩溃。

### 1.5 Token 管控策略

1. 限制当前对话最大上下文 token。
2. 限制用户在周期时间窗内的最大 token 用量。
3. 必须记录并展示：
   - 本次请求 token
   - 当前会话累计 token
   - 当前用户周期累计 token

### 1.6 记录与状态管理

1. 对每个选手记录完整对话内容。
2. 对每个选手记录当前实时状态（在线、排队、生成中、异常等）。
3. 管理员可实时查看选手会话状态与用量。

### 1.7 监控与可观测性

1. 系统提供完整监控面板。
2. 监控范围包括：CPU、内存、磁盘、网络、GPU、队列、请求延迟、错误率。
3. 关键组件掉线后必须告警，并触发自动重启尝试。

---

## 2. 范围与非范围

### 2.1 本期范围

1. 系统架构设计。
2. 模块接口定义。
3. 数据模型设计。
4. 调度与容错方案。
5. 界面与权限能力定义。

### 2.2 非范围

1. 自动化测试脚本编写。
2. 发布流程、灰度策略、CI/CD 细节。
3. 外部审计模型与合规框架对齐。

---

## 3. 目标架构设计

### 3.1 分层架构

```text
[竞赛选手/管理员]
      |
      | HTTPS / WSS
      v
[Nginx 入口网关]
      |
      v
[Web 前端层]
  - 选手 Chat 界面
  - 管理员控制台
      |
      v
[应用与管控层]
  - Chat API 服务
  - Token/配额服务
  - 队列调度服务
  - 状态管理服务
      |
      +--------------------------+
      |                          |
      v                          v
[LiteLLM Gateway]          [PostgreSQL + Redis]
      |
      v
[vLLM 推理引擎 (qwen35b)]

================ 观测侧路 =================
[Node Exporter] [DCGM Exporter] [vLLM metrics]
             \      |      /
               [Prometheus]
                    |
                [Grafana]
                    |
              [Alertmanager]
```

### 3.2 网络隔离

1. 外部流量仅允许进入 Nginx。
2. LiteLLM、vLLM、数据库位于内部网络，禁止公网直接访问。
3. 管理界面仅允许管理员访问。

### 3.3 组件职责

1. Nginx：TLS 终止、反向代理、SSE 长连接保活。
2. Web 前端：提供选手与管理员可视化能力。
3. 应用与管控层：业务核心（配额、队列、状态、记录）。
4. LiteLLM：统一 OpenAI 兼容网关和模型调用适配。
5. vLLM：高吞吐推理执行。
6. PostgreSQL：业务主数据（用户、会话、消息、账本）。
7. Redis：实时计数与队列状态缓存。
8. Prometheus/Grafana：指标采集与可视化。
9. Alertmanager：告警路由与通知。

---

## 4. 请求生命周期与状态机

### 4.1 聊天请求主流程

1. 选手在 Chat 界面发送消息。
2. 前端调用 Chat API（选手无需感知 key）。
3. 服务端识别用户身份并执行配额预检。
4. 通过后进入请求队列。
5. 调度器根据当前资源占用决定出队节奏。
6. 出队请求经 LiteLLM 转发到 vLLM。
7. 流式返回响应给前端。
8. 请求结束后写入 token 账本、会话状态和消息记录。

### 4.2 用户状态机

1. offline：离线。
2. online：在线未请求。
3. queued：请求排队中。
4. generating：模型生成中。
5. rate_limited：触发 token 时间窗限制。
6. error：当前请求异常。

状态迁移约束：

1. online -> queued -> generating -> online。
2. queued 超时 -> error -> online。
3. 触发配额限制 -> rate_limited，窗口重置后回 online。

### 4.3 会话状态机

1. active：会话活跃。
2. waiting：本轮请求排队中。
3. streaming：模型流式返回中。
4. completed：本轮完成。
5. aborted：客户端中止。
6. timeout：超时结束。

---

## 5. Token 与配额设计

### 5.1 计量维度

1. request_input_tokens：本次输入 token。
2. request_output_tokens：本次输出 token。
3. session_total_tokens：会话累计 token。
4. user_window_tokens：用户时间窗累计 token。

### 5.2 限制规则

1. 对话上下文限制：
   - input_tokens + max_output_tokens <= max_context_tokens
2. 用户时间窗限制：
   - user_window_tokens + predicted_tokens <= window_limit_tokens
3. 会话累计限制：
   - session_total_tokens <= session_limit_tokens

### 5.3 预检与结算

1. 请求前预检：上下文是否超限、窗口是否超限。
2. 请求后结算：按实际生成 token 更新账本。
3. 流式中断结算：按已生成 token 计费与统计。

### 5.4 默认参数建议

1. max_context_tokens：8192（可配置）。
2. max_output_tokens：2048（可配置）。
3. window_duration：1h（可配置）。
4. window_limit_tokens：按赛事策略配置。

---

## 6. 排队与动态调度设计

### 6.1 设计原则

1. 高负载时优先排队，不降级。
2. 保证用户公平，避免单用户占满系统。
3. 动态调节并发，避免触发 vLLM OOM 或进程退出。

### 6.2 队列结构

1. 全局主队列（按到达时间排序）。
2. 用户子队列（每个用户独立排队）。
3. 调度采用加权公平轮询（用户默认同权重）。

### 6.3 动态并发控制

1. 维护动态并发上限 C。
2. 根据以下指标实时调整 C：
   - GPU 显存占用
   - GPU 计算利用率
   - 平均队列等待时长
   - vLLM 当前活动请求数
3. 当资源占用接近阈值，降低出队速率。
4. 当资源回落，逐步提升出队速率。

### 6.4 排队超时与保护

1. 请求设置最大排队时长。
2. 超时后返回系统繁忙提示并记录超时事件。
3. 队列长度超过硬阈值时，暂停新请求入队并提示稍后重试。

### 6.5 调度伪代码

```text
loop every 200ms:
  metrics = collect_runtime_metrics()
  C = adjust_concurrency(metrics)

  while running_requests < C:
    req = pick_next_request_fairly()
    if req is None:
      break
    dispatch_to_litellm(req)
```

---

## 7. 故障容忍、自愈与告警

### 7.1 自愈目标

1. 关键组件异常后自动恢复。
2. 降低人工干预频率。
3. 在高负载前提前削峰，避免服务崩溃。

### 7.2 健康检查

1. vLLM：liveness + readiness。
2. LiteLLM：liveness + readiness。
3. 应用服务：liveness + readiness。
4. PostgreSQL/Redis：连接探针。

### 7.3 自动重启策略

1. 掉线后自动重启。
2. 指数退避重启（5s/10s/20s/40s，封顶 5min）。
3. 连续失败达到阈值后触发高优先级告警。

### 7.4 告警策略

1. 服务离线告警。
2. 重启频繁告警。
3. 队列堆积告警。
4. P95 首 token 延迟超阈值告警。
5. GPU OOM 告警。

### 7.5 不采用回退策略

1. 不自动切换到备用低模型。
2. 通过排队与动态调度避免过载。
3. 通过自动重启和告警恢复可用性。

---

## 8. 数据模型设计（PostgreSQL）

### 8.1 users

字段建议：

1. id
2. username
3. role（player/admin）
4. status（offline/online/...）
5. created_at
6. updated_at

### 8.2 api_keys

字段建议：

1. id
2. user_id
3. key_hash
4. is_active
5. created_at
6. rotated_at
7. last_used_at

说明：仅保存哈希，不保存明文。

### 8.3 sessions

字段建议：

1. id
2. user_id
3. model_name
4. think_enabled
5. status
6. total_input_tokens
7. total_output_tokens
8. total_tokens
9. last_active_at
10. created_at

### 8.4 messages

字段建议：

1. id
2. session_id
3. user_id
4. role（system/user/assistant）
5. content
6. input_tokens
7. output_tokens
8. latency_ms
9. seq_no
10. created_at

### 8.5 token_ledger

字段建议：

1. id
2. user_id
3. session_id
4. request_id
5. input_tokens
6. output_tokens
7. total_tokens
8. window_key
9. created_at

### 8.6 request_queue_events

字段建议：

1. id
2. request_id
3. user_id
4. queue_status（enqueued/dispatched/timeout/finished）
5. queue_wait_ms
6. created_at

---

## 9. Redis 设计

### 9.1 用途

1. 用户窗口 token 计数器。
2. 请求队列实时状态。
3. 在线用户状态缓存。

### 9.2 key 设计建议

1. quota:{user_id}:{window}
2. queue:global
3. queue:user:{user_id}
4. user_status:{user_id}

---

## 10. API 设计

### 10.1 选手侧 API

1. POST /api/chat/completions
   - 功能：发送聊天请求（支持 stream、think 开关）。
2. GET /api/sessions/{session_id}
   - 功能：获取当前会话状态与会话 token。
3. GET /api/me/usage
   - 功能：获取用户周期用量与剩余额度。

### 10.2 管理员侧 API

1. GET /api/admin/users
   - 功能：用户列表、当前状态、用量概览。
2. GET /api/admin/users/{user_id}/sessions
   - 功能：查看用户会话与实时状态。
3. PATCH /api/admin/users/{user_id}/quota
   - 功能：修改用户配额。
4. POST /api/admin/users/{user_id}/key/reset
   - 功能：重置用户 API Key（用户无感）。
5. GET /api/admin/queue
   - 功能：查看队列长度、等待时延、并发状态。
6. POST /api/admin/system/restart/{component}
   - 功能：手动触发组件重启。

### 10.3 系统探针 API

1. GET /healthz
2. GET /readyz
3. GET /metrics

---

## 11. 前端界面设计（3 个界面）

### 11.1 界面一：选手 Chat 界面

目标：提供稳定、低学习成本、赛事友好的对话界面。

能力：

1. 基础体验与 Open WebUI 一致。
2. 定制化视觉样式（参考 chatjimmy.ai 设计语言）。
3. 显示本次请求 token。
4. 显示当前会话累计 token。
5. 显示当前用户周期累计 token 与剩余 token。
6. 显示请求状态（排队中、生成中、完成、超时）。
7. 提供思考模式开关。

### 11.2 界面二：管理员控制台

目标：集中配置、运维操作、用户观察。

能力：

1. 用户管理：启停账号、查看当前状态。
2. key 管理：重置/禁用用户 key。
3. 配额管理：修改用户周期配额和上下文限制。
4. 会话观察：实时查看用户正在进行的会话状态。
5. 队列管理：观察排队深度与等待时长。
6. 系统操作：触发服务重启、查看近期故障。
7. 参数管理：
   - 热更新参数：temperature、top_p、max_output_tokens。
   - 冷更新参数：模型加载参数（需重启生效）。

### 11.3 界面三：监控大屏

目标：系统健康度可视化和告警定位。

能力：

1. GPU 负载、显存、功耗。
2. CPU、内存、磁盘、网络。
3. 请求吞吐、排队时延、首 token 延迟。
4. 错误率、超时率、重启次数。
5. 告警状态与恢复状态。

---

## 12. 开源选型与二次开发

### 12.1 选型

1. 推理：vLLM。
2. 网关：LiteLLM Gateway。
3. UI：Open WebUI 二开（或前端自研）。
4. 数据库：PostgreSQL。
5. 缓存与实时状态：Redis。
6. 监控：Prometheus + Grafana + Alertmanager。

### 12.2 二开任务清单

1. 统一用户状态机实现。
2. 请求队列与公平调度器实现。
3. Token 预检与结算模块实现。
4. 选手 token 可视化组件实现。
5. 管理员实时会话观察面板实现。
6. 自愈重启与告警联动实现。
7. 单模型到多模型的模型注册表抽象实现。

---

## 13. 配置与参数管理规范

### 13.1 配置分层

1. 基础环境配置：端口、数据库连接、缓存连接。
2. 推理配置：模型路径、并发上限、上下文长度。
3. 业务配置：配额策略、窗口时长、队列超时阈值。
4. 告警配置：阈值、告警通道、抑制规则。

### 13.2 包管理与工程管理

1. 项目采用包管理维护依赖版本。
2. 配置文件使用环境变量 + 配置文件双轨方案。
3. 不同环境（dev/staging/prod）采用独立配置。

---

## 14. 详细实施计划（5 阶段）

### 阶段 0：环境与基础能力（Day 1）

目标：完成 GPU 与容器基础环境，具备模型运行条件。

1. 安装 NVIDIA 驱动与容器工具。
2. 配置 Swap（用于模型加载缓冲，不用于长期内存替代）。
3. 准备目录结构与挂载卷。

### 阶段 1：数据与管控中枢（Day 2）

目标：建立用户、会话、token、队列事件的存储与服务。

1. 部署 PostgreSQL 与 Redis。
2. 完成核心表结构初始化。
3. 完成 Token 预检与结算服务。
4. 完成用户状态管理服务。

### 阶段 2：推理与排队调度（Day 2-3）

目标：点亮 qwen35b，打通排队到推理的主链路。

1. 下载并挂载 qwen35b 模型。
2. 部署 vLLM 并验证推理可用。
3. 实现请求入队、调度出队、流式回传。
4. 接入动态并发控制逻辑。

### 阶段 3：前端与管理能力（Day 3-4）

目标：交付选手界面与管理员控制台。

1. 完成选手 chat 定制化样式。
2. 增加 token 三段显示与会话状态显示。
3. 完成管理员用户管理、配额管理、会话观察界面。
4. 完成 key 重置与禁用操作界面。

### 阶段 4：监控、告警与自愈闭环（Day 4-5）

目标：形成可观测与高可用闭环。

1. 部署 Exporter 与 Prometheus。
2. 构建 Grafana 监控大屏。
3. 配置 Alertmanager 告警策略。
4. 验证组件掉线自动重启与告警通知。

---

## 15. 验收标准（架构阶段）

1. 选手可通过 chat 界面完成流式对话。
2. 用户无感 API Key，全链路可追踪到具体用户。
3. 用户 token 限制生效，超限可阻止新请求。
4. 系统高负载时进入排队，不触发自动降级。
5. 管理员可实时看到用户状态、会话状态、队列状态。
6. 关键组件掉线后可自动重启并触发告警。
7. 监控大屏可展示资源、吞吐、时延与错误趋势。

---

## 16. 待确认事项

1. 用户时间窗默认策略（分钟、小时、天）的具体配额值。
2. 请求排队最大等待时长阈值。
3. 告警通知通道（邮件、企业微信、钉钉等）。
4. 管理员是否需要二次认证。
5. Think 模式是否计入额外 token 预算或采用同一预算。

---

## 17. 最终结论

本设计在当前单模型前提下，优先保证系统稳定性、公平性和可运维性：

1. 用排队和动态调度替代降级策略。
2. 用用户级 token 管控与状态机保证可控和可见。
3. 用自动重启与告警形成故障闭环。
4. 在不改变当前单模型部署目标的前提下，保留未来多模型扩展能力。

---

## 18. 当前阶段实施模式（已落地）

为提升研发效率并避免容器网络问题阻塞开发，当前阶段采用“本地优先、容器兜底”的实施模式：

1. vLLM 固定本机进程运行，不进入 Docker。
2. LiteLLM 与 Open WebUI 以本地源码方式接入，便于二次开发。
3. PostgreSQL、Redis、Prometheus、Grafana 作为边缘组件，容器化为可选方案而非前置条件。
4. 若 Docker 镜像拉取或容器网络出现问题，允许直接切换到本机安装运行边缘组件。

### 18.1 本地主链路

```text
Open WebUI (local source)
   -> LiteLLM (local source)
   -> vLLM (local process)
```

### 18.2 当前仓库组织

1. `middleware/litellm/`：LiteLLM 上游源码。
2. `middleware/open-webui/`：Open WebUI 上游源码。
3. `scripts/vllm/start-qwen35b.sh`：vLLM 本机启动脚本。
4. `infra/litellm/config.yaml`：LiteLLM 到本地 vLLM 的模型映射配置。
5. `infra/compose/dev-services.yml`：边缘组件容器辅助编排（可选）。
6. `docs/dev-setup.md` 与 `docs/runbook/dev-runbook.md`：开发与排障操作手册。

