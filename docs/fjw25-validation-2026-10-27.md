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

以上为 vLLM 直连的整次请求耗时。首 token 延迟（TTFT）的补测见下表；未对 Nginx → Chainlit → LiteLLM → vLLM 完整链路做同规模并发压测。浏览器 E2E 单独验证了完整链路的功能。

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

## 功能与依赖验证

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
