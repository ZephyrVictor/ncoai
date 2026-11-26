# fjw25 部署与测试记录（2026-10-27）

## 实测环境

- 主机：`fjw25`，4 × NVIDIA RTX A6000（每卡 49,140 MiB），驱动 535.247.01。
- 模型目录：`~/workspace/ncoai/models/Qwen3.8-27B`；vLLM 服务名沿用 `qwen35_27b`。
- vLLM 0.31.0，BF16，TP=4，`MAX_MODEL_LEN=4096`，`MAX_NUM_SEQS=32`，`MAX_NUM_BATCHED_TOKENS=8192`，`GPU_MEMORY_UTILIZATION=0.90`，启用 prefix caching。
- LiteLLM 1.106.4 使用独立虚拟环境与 `ncoai_litellm` 数据库；业务库为 `ncoai_chat`。
- 定制 Chainlit 2.12.0，FastAPI 0.162.0，uvicorn 0.54.0，psycopg 3.3.6，Redis Python 客户端 8.1.0。
- 本次 PostgreSQL 实际使用 18.6：服务器无法拉取默认的 19 镜像。Prometheus 端口为 9091。
- 系统 nvcc 11.5 无法编译当前 FlashInfer sampler，启动配置使用 `VLLM_USE_FLASHINFER_SAMPLER=0`。本次未升级 vLLM 至 0.32.0。
- CAPS 录像功能关闭，未纳入验收。

## 推理压测

使用 `tests/loadtest_100c_1000token.py` 直接访问 vLLM `/v1/chat/completions`，每次请求输出上限 880 token，所有成功请求均输出 880 token。实际平均输入约 199 token，平均总长度约 1,079 token。

| 并发数 | 请求数 | 成功 / 失败 | 输出吞吐（token/s） | 平均耗时（秒） | p95（秒） | p99（秒） |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 | 32 | 32 / 0 | 408.9 | 34.424 | 35.431 | 35.431 |
| 100 | 100 | 100 / 0 | 553.2 | 90.254 | 130.995 | 159.016 |

100 并发测试总耗时 159.088 秒，吞吐 0.629 请求/秒，GPU 满载，观测到每卡显存最高约 43,691 MiB。并发达到 100 时会产生排队；该数字不能作为 100 人同时流畅对话的保证。

以上为 vLLM 直连的整次请求耗时。首 token 延迟（TTFT）的补测见下表；前端完整链路的浏览器并发补测见后文，两次测量的输出长度不同。

原始请求结果保存在服务器与本地仓库的忽略目录中：

- `.local/acceptance/loadtest-16c-1000tok.json`
- `.local/acceptance/loadtest-100c-1000tok.json`

### 首 token 延迟补测

预热完成后，通过服务器本机 HTTP 直连 vLLM，使用 `stream=true` 和 SSE 测量。从发出请求前开始计时，到收到第一个非空生成内容为止，忽略仅含角色信息的空分片；同时检查 reasoning 与正文分片，本次所有请求的首个生成内容均为正文。计时包括 vLLM 内部排队和本机 HTTP 开销，未覆盖浏览器、Chainlit、LiteLLM 及外部网络。

| 并发数 | 请求数 | 成功 / 失败 | 平均 TTFT（秒） | p50（秒） | p95（秒） | 最慢（秒） |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 5 | 5 / 0 | 0.148 | 0.120 | 0.238 | 0.268 |
| 16 | 16 | 16 / 0 | 1.248 | 1.321 | 1.325 | 1.326 |
| 100 | 100 | 100 / 0 | 49.505 | 46.155 | 89.836 | 131.414 |

使用与原压测相同的提示构造方法，平均实际输入约 199–201 token。单请求组依次执行 5 次，每次输出 32 token；16 与 100 并发组各执行一轮，每次均完整输出 880 token，以保留生成期间的排队压力。单请求组范围为 0.117–0.268 秒；仅 5 次样本，其 p95 参考价值有限。上述结果均为预热后的有限样本测量，不代表持续负载或长上下文下的容量保证。

100 并发时日志显示同时生成 32 个请求、等待 68 个请求，与 `MAX_NUM_SEQS=32` 一致。后续批次需要等待前批生成结束，因此高并发的 TTFT 明显增加。

测量脚本、原始结果及关闭核查保存在本地忽略目录 `.local/acceptance/`：`measure_ttft.py`、`ttft-single.json`、`ttft-16c.json`、`ttft-100c.json`、`ttft-measurement.log`、`ttft-shutdown-verification.txt`。脚本、原始结果和测量日志在服务器同一路径也有副本。

## 前端 Chat 与自动预热补测

`devctl up apps` 默认等待 vLLM 模型就绪，使用生产 system prompt 执行 4 次长短输入预热，再启动下游应用。预热直接访问模型，不计入用户额度。重启模型后需重新预热；也可手动执行 `python3 devctl.py warmup`。预热减少启动开销，高并发下仍需等待前面的生成完成。

使用 `scripts/chat_e2e_latency.py` 在 fjw25 上运行真实 Chromium，经 `http://127.0.0.1:8088/chat` 登录独立用户，覆盖 Nginx → Chainlit → LiteLLM → vLLM。计时从点击发送开始，到浏览器 DOM 首次显示非空答案正文结束，包含应用排队及渲染；“首反馈”为前端占位或状态首次出现，不能当作模型首字。测量不包含用户外部网络延迟及页面首次加载。

浏览器环境为 Playwright 1.64.1 / Chromium 156.0.8078.4，并发会话使用同一浏览器中的独立 context。测试机的浏览器资源争用也会计入结果，与不同设备上各运行一个浏览器的负载并不完全相同。

| 并发数 | 请求数 | 成功 / 失败 | 平均首字（秒） | p95 首字（秒） | 最慢首字（秒） | 平均首反馈（秒） |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 3 | 3 / 0 | 0.244 | 0.282 | 0.288 | 0.085 |
| 16 | 16 | 16 / 0 | 1.580 | 1.789 | 1.802 | 0.160 |
| 100（认证修复后） | 100 | 100 / 0 | 53.724 | 100.397 | 100.447 | 1.004 |

表中单用户与 16 并发采用全部后台任务结算后执行的独立复测（`chat-latency-isolated-small.json`），100 并发采用完整最终验收（`chat-latency-accepted.json`）。完整验收开始时，修复前认证故障注入留下的一条后台生成尚在完成，因此额外复测低并发，排除该负载对结果的影响。

单用户组为短中文回答，实际输出 35–50 token，平均 43 token；16 并发组请求 40 行编号回答，每次实际输出 551 token，提交时间跨度 0.004 秒，平均整次耗时 24.760 秒。100 并发最终复测的提交跨度为 0.203 秒，每次输出 551 token，平均整次耗时 83.791 秒，最慢 131.132 秒；70 个页面观察到排队提示。该输出长度与 vLLM 直连测试的 880 token 不同，不能据此直接计算前后加速比例。单用户仅 3 个样本，p95 参考价值有限。

首轮 100 并发中有 94 个页面通过、6 个页面失败，失败截图显示白页，页面中的计时探针丢失；数据库中全部 100 个请求均完成并结算。随后仅增加导航、主文档响应、renderer crash、控制台和请求失败诊断，未改变生产源码，复测 100/100 通过，生成期间未观察到页面导航、crash、JavaScript 异常、HTTP 5xx 或 WebSocket 断开。

恢复停止控制后再次执行完整浏览器验收，单用户 3/3、16 并发 16/16 通过，但 100 并发有 99/100 通过、1 个页面再次失败。这次诊断明确记录：`/chat/user` 重新校验请求发生 `net::ERR_SOCKET_NOT_CONNECTED`，前端随后跳转至 `/chat/login`；截图捕获的是登录页加载时的空白阶段。`useUserManagement` 原来对任何 API 错误执行 `setUser(null)`，将临时网络失败误判为登录失效。现在仅 HTTP 401 清除登录用户，网络和 5xx 错误保留现有登录状态，并交由 SWR 重试。首次 6 个白页缺少同等诊断，无法逐个确认是否均为此原因，原始失败记录全部保留。

对修复前版本注入 `/chat/user` 连接失败，稳定复现 `Network error incorrectly signed out the user`；原始结果为 `chat-auth-fault-before-fix.json`。新增回归覆盖网络异常、503 后保留登录并恢复用户数据，以及 401 正常清除登录。浏览器生命周期脚本也加入同样的真实请求故障注入，要求生成控制仍可用且后续认证重试返回 200。

认证修复后的完整验收通过：100/100 并发页面完成流式回答，未出现生成期间的意外导航、renderer crash、JavaScript 异常、HTTP 5xx 或 WebSocket 断开，可见总用量与结算 API 一致。随后主动中断认证重新校验请求，Chat 保持登录、停止按钮仍可用，解除故障后真实 `/chat/user` 重试返回 200；停止、后续聊天、生成中刷新恢复、再次停止与复制均通过。

浏览器验收发现并修复了两处前端问题：首轮提问分配 thread ID 时，连接回调随之改变，导致多余的 WebSocket 重连；现在通过 ref 获取最新 thread ID，保持连接回调稳定。结算消息缺少用户配额版本号，导致右上角总用量被旧 API 缓存覆盖；现在保留版本号，并直接断言可见总用量与结算 API 一致。

独立生命周期验收还发现：生成中刷新并恢复 thread 后，正文继续增长，但生成状态和停止按钮没有恢复。现在从服务端活跃消息恢复状态，服务端终结消息会清除旧的本地生成快照；后端 registry 保存任务和用户归属，允许同一认证用户通过新 socket 取消原任务，重复取消不打断结算。该实现适用于当前单 worker 部署，未验证跨进程取消。

修复后的生命周期复测通过：首次停止后正文不再增长、预留归零；新会话继续聊天成功；最终验收中生成后导航恢复的正文从 119 字符增长至 1,713 字符；停止按钮恢复并成功停止原任务，预留再次归零、输入恢复、复制按钮确认成功。

启动流程还修复了进程存活即报告成功的问题：默认等待应用 HTTP 健康检查，LiteLLM 使用带认证的 `/health/readiness`。前端显式重建选项 `BUILD_CHAINLIT_FRONTEND=1` 现在会更新已有构建，源码部署同时更新前端 dist 和后端打包副本。

本轮回归：根目录 `tests` 与 `apps/chainlit/tests` 共 95 passed；React 客户端测试 27 passed，含首轮 thread ID 变化保持连接、恢复停止控制、服务端终结清理快照、配额版本和认证恢复回归；React 客户端及生产前端构建通过。随后加强重复取消保护，Chat 取消相关后端测试 7 passed，覆盖应用清除 cancellation 后仍不得再次打断结算的情况。

原始结果在本地与服务器 `.local/acceptance/` 保留：`100c-first/`、`chat-latency-100c-diagnostic.json`、`chat-latency-100c-diagnostic.log`、`lifecycle-first/`、`chat-latency-lifecycle-retest.json`、`chat-latency-final.json`、`chat-auth-fault-before-fix.json`、`chat-latency-accepted.json`、`chat-latency-isolated-small.json` 和对应日志、截图。登录前的匿名 API 401 与主动注入的网络错误会出现在控制台诊断中；上述 JavaScript 异常断言指 `pageerror`，不将正常认证跳转误记为生成失败。

## 功能与依赖验证

以下为本次前端延迟补测之前的完整部署验收记录：

- 项目 Python 测试：148 passed。
- 定制 Chainlit 选定后端测试：110 passed。
- 部署修复相关 devctl 测试：本地与服务器各 6 passed。
- 配额验收等待逻辑回归测试：本地与服务器各 6 passed。
- 应用与 LiteLLM 两个虚拟环境的 `uv pip check` 均通过。
- 11 个基础设施容器运行正常；Prometheus 9 个 job、Grafana 数据源与 5 个 dashboard、Loki readiness 检查通过。
- Nginx Cookie 粘性路由通过。
- 用户管理、禁用删除、CSV 导入、管理监控、流式恢复与复制、功能开关浏览器用例通过；流式恢复分别验证了代理和直连路径。
- 配额浏览器复测通过：单会话使用 4,040 / 4,096 token 后拦截后续上下文；用户总额度使用 19,944 / 20,000 token 后，剩余 56 token 小于最低请求预算 84 token，正确禁用输入与发送。
- 最终配额预留为 0；Redis 热缓存删除后成功重建；删除已消费会话后，用量仍为 19,944 token，数据库汇总、账本聚合与配额 API 一致。
- 数据检查通过：缺失表为 0，全局策略为 1 条，配额账本为 21 条，Redis 全局配额配置存在。

上述浏览器用例对应部署验收的全部 6 个应用脚本。修复后单独重跑配额用例和数据检查，其余已通过的阶段未重复执行。

最终配额和数据结果保存在 `.local/acceptance/quota-settled-retest.json` 与 `.local/acceptance/data-validation.json`，本地和服务器各保留一份。

配额验收脚本存在两处判定问题：只识别“额度已用尽”，会漏掉“剩余额度不足以容纳下一轮上下文”等合法拦截提示；生成中的预留也可能临时耗尽可用额度，被误判为最终拦截。本次补充提示匹配，等待预留归零且流式生成结束，再区分本轮完成和下一轮被拒绝。对于已禁用的输入框，直接验证禁用与拦截状态。保留用量、输入禁用、Redis 重建、数据库账本等实际断言。

## 服务关闭

2026-10-27 08:55:19 UTC 核查：

- `vllm`、`litellm`、`chainlit-1`、`admin` 均停止，CAPS 未运行，`ncoai-dev` tmux 会话退出。
- 11 个 ncoai 基础设施容器均已停止并移除，运行中的 `ncoai-*` 容器为 0。
- 应用、数据库和监控相关端口无残留监听。
- 4 张 GPU 均为 10 MiB 显存、0% 利用率。

通过 `devctl.py down apps` 和 Compose `down` 关闭，未删除模型或数据库卷。验收数据（含测试用户、账本与测试设置的 20,000 token 全局配额）随数据库卷保留。关闭核查记录位于 `.local/acceptance/shutdown-verification.txt`。

TTFT 补测仅重新启动 vLLM，脚本结束时自动执行 `devctl.py down vllm`。2026-10-27 09:37:15 UTC 再次核查：全部 ncoai 应用停止，`ncoai-dev` tmux 会话退出，vLLM 端口 12233 无监听，运行中的 `ncoai-*` 容器为 0；4 张 GPU 均恢复为 10 MiB 显存、0% 利用率。

真实 Chat 最终验收结束后，2026-10-27 10:46:39 UTC 数据核查：账本处于 reserved 状态的记录为 0，用户和会话汇总的 reserved tokens 均为 0；账本共 513 条 settled、1 条 expired。vLLM 运行中与等待中的请求均为 0。结果保存在 `.local/acceptance/chat-final-data-verification.json`。

随后执行 `devctl.py down apps` 与 Compose `down`，2026-10-27 10:48:31 UTC 核查：全部 ncoai 应用停止，`ncoai-dev` tmux 会话退出，运行中的 `ncoai-*` 容器为 0，应用、数据库和监控端口无残留监听。4 张 GPU 均为 10 MiB 显存，无计算进程；瞬时利用率为 0–1%。其他项目的 `n1ctf-bot-1`、`n1ctf-orchestrator-1`、`harmony_standby` 继续运行。模型和数据库卷保留，未使用 `down -v`。核查原始记录在本地与服务器 `.local/acceptance/chat-shutdown-verification.txt`。
