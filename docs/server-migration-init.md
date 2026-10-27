# 迁移到新服务器时如何初始化本项目

这份文档面向“把当前仓库迁移到另一台服务器”这个场景，重点回答两件事：

1. 哪些文件和数据必须一起迁过去。
2. 新机器上应该按什么顺序初始化脚本。

请注意：部署时应以 `infra/env/*.env` 中的实际配置为准，历史设计文档里的默认值只能作为参考。

## 1. 先分清项目里的两类服务

这个仓库当前是“宿主机应用 + 容器化基础设施”的混合部署：

- 宿主机进程，由 `python3 devctl.py up` 管理：
  - `vllm`
  - `litellm`
  - `caps-ftp`
  - `chainlit`
  - `admin`
- Docker 容器，由 `scripts/deploy/init-infra.sh` 拉起：
  - `postgres`
  - `redis`
  - `nginx`
  - `prometheus`
  - `grafana`
  - `loki`
  - `alloy`
  - exporters

这也是为什么迁移时不能只执行 `python3 devctl.py up`。它只负责宿主机应用，不负责容器、数据库初始化和表结构引导。

## 2. 迁移前需要准备什么

至少要确认下面这些内容：

- 代码仓库本身
- 模型目录，也就是 `MODEL_PATH` 指向的目录
- 环境文件：
  - `infra/env/common.env`
  - `infra/env/host.env`
  - `infra/env/compose.env`
  - `infra/env/secrets.env`
- 如果启用了 HTTPS / FTPS，还需要对应证书文件：
  - `NGINX_SSL_CERT`
  - `NGINX_SSL_KEY`
  - `CAPS_FTP_TLS_CERTFILE`
  - `CAPS_FTP_TLS_KEYFILE`
- 如果要保留上传录像/文件，还要迁移 `CAPS_STORAGE_ROOT` 对应目录
- 如果要保留历史数据，还要迁移 PostgreSQL 中的业务数据库和 LiteLLM 数据库

推荐额外检查：

- 防火墙是否放行 `80`、`443`
- 如果启用 FTPS，是否放行 `CAPS_FTP_PORT` 和被动端口范围
- 新机器的 GPU、CUDA、驱动是否和 `vLLM` 运行要求匹配

## 3. 新服务器的前置依赖

`scripts/deploy/init-host.sh` 会直接检查这些命令或条件：

- `bash`
- `tmux`
- `docker`
- `python3`
- `uv`
- `nvidia-smi`
- `MODEL_PATH` 必须存在

如果新机器无法访问公网，还要提前准备：

- Python wheelhouse，并通过 `UV_PIP_EXTRA_ARGS` 指向本地依赖源
- Docker 镜像，至少覆盖 `infra/compose/images.manifest.txt`
- Node.js，或者保留已构建好的 Chainlit 前端并设置 `BUILD_CHAINLIT_FRONTEND=0`
- Playwright 浏览器，或者设置 `INSTALL_PLAYWRIGHT_BROWSERS=0`

## 4. 环境文件怎么准备

如果新服务器是全新初始化，建议从 example 生成：

```bash
bash scripts/deploy/prepare-env.sh
```

重点检查这些变量：

- 基础连接：
  - `DATABASE_URL`
  - `LITELLM_DATABASE_URL`
  - `REDIS_URL`
- 模型与 GPU：
  - `MODEL_PATH`
  - `CUDA_VISIBLE_DEVICES`
  - `TP_SIZE`
- 外部访问：
  - `NGINX_SERVER_NAME`
  - `NGINX_ENABLE_TLS`
  - `NGINX_SSL_CERT`
  - `NGINX_SSL_KEY`
- 上传服务：
  - `CAPS_ENABLED`
  - `CAPS_STORAGE_ROOT`
  - `CAPS_FTP_USERNAME`
  - `CAPS_FTP_PASSWORD`
- 管理与认证：
  - `CHAINLIT_AUTH_SECRET`
  - `ADMIN_APP_SESSION_SECRET`
  - `ADMIN_APP_USERNAME`
  - `ADMIN_APP_PASSWORD`
  - `LITELLM_MASTER_KEY`

生产迁移时建议重新生成并轮换 `infra/env/secrets.env` 里的所有敏感值，不要直接复用仓库里旧服务器的凭据。

另外要特别注意：

- `DATABASE_URL` 给 `chainlit` 和 `admin` 用
- `LITELLM_DATABASE_URL` 给 LiteLLM 用
- 生产环境不要把这两个变量指到同一个数据库

## 5. 旧服务器如何打包迁移状态

如果你想把旧机器上的数据库、上传目录、当前环境文件和可读证书一起打包出来，现在可以直接用仓库内脚本：

```bash
bash scripts/deploy/backup-state.sh
```

默认会生成两个产物：

- 目录：`.local/migration/ncoai-migration-<timestamp>/`
- 压缩包：`.local/migration/ncoai-migration-<timestamp>.tar.gz`

打包内容包括：

- `infra/env/*.env` 的快照
- 应用数据库 SQL dump
- LiteLLM 数据库 SQL dump
- `CAPS_STORAGE_ROOT` 压缩包
- 当前可读的 TLS / FTPS 证书文件
- 迁移 manifest

如果你想自定义输出目录：

```bash
bash scripts/deploy/backup-state.sh /path/to/output/ncoai-migration
```

把生成的 `.tar.gz` 带到新服务器即可。

注意：

- 这个迁移包包含 `secrets.env`
- 如果证书文件可读，还可能包含 TLS / FTPS 私钥
- 传输和存储时请按敏感资产处理

## 6. 初始化顺序

### 方案 A：迁移到新服务器，但不保留旧业务数据

这是最简单的空栈初始化方式。

1. 初始化宿主机、依赖和本地虚拟环境：

```bash
python3 devctl.py init
```

`python3 devctl.py init` 实际会顺序执行：

1. `bash scripts/deploy/init-host.sh`
2. `bash scripts/deploy/init-infra.sh`
3. `bash scripts/deploy/init-db.sh`

然后启动宿主机应用：

```bash
python3 devctl.py up apps
```

最后做验证：

```bash
bash scripts/deploy/validate.sh
```

### 方案 B：迁移到新服务器，并保留旧业务数据

如果你要保留用户、会话、配额账本、LiteLLM 数据等历史信息，推荐按下面顺序做：

1. 先初始化宿主机环境：

```bash
bash scripts/deploy/init-host.sh
```

2. 再启动容器化基础设施：

```bash
bash scripts/deploy/init-infra.sh
```

3. 用迁移包恢复旧数据：

```bash
bash scripts/deploy/restore-state.sh /path/to/ncoai-migration-<timestamp>.tar.gz
```

这个脚本会做几件事：

- 把迁移包里的 `infra/env/*.env` 恢复到当前仓库
- 恢复应用数据库和 LiteLLM 数据库
- 恢复 `CAPS_STORAGE_ROOT`
- 尝试把证书恢复到原路径
- 自动执行一次 `init-db.sh` 做缓存和引导同步

4. 启动宿主机应用：

```bash
python3 devctl.py up apps
```

5. 跑部署验证：

```bash
bash scripts/deploy/validate.sh
```

## 7. 每个初始化脚本到底做什么

- `scripts/deploy/prepare-env.sh`
  - 从 example 生成 `infra/env/*.env`
  - 保留已有文件不覆盖
  - 检查是否仍有占位符配置
- `scripts/deploy/backup-state.sh`
  - 导出数据库 SQL dump
  - 打包 `CAPS_STORAGE_ROOT`
  - 复制当前环境文件和可读证书
  - 生成迁移压缩包
- `scripts/deploy/restore-state.sh`
  - 恢复环境文件
  - 恢复数据库和 `CAPS_STORAGE_ROOT`
  - 尝试恢复证书
  - 自动执行 `init-db.sh`
- `scripts/deploy/init-host.sh`
  - 检查宿主机依赖
  - 创建 `.venv` 和独立的 `.venv-litellm`
  - 安装 Python 依赖
  - 按需安装 Node.js
  - 按需安装 Playwright Chromium
  - 构建 Chainlit 前端静态资源
- `scripts/deploy/init-infra.sh`
  - 用 compose 拉起 PostgreSQL、Redis、Nginx 和监控容器
  - 等待容器健康检查通过
- `scripts/deploy/init-db.sh`
  - 运行 `scripts/deploy/init_db.py`
  - 确保数据库存在
  - 引导表结构、管理员账号、全局配额和 Redis 热缓存
- `scripts/deploy/start-services.sh`
  - 实际调用 `python3 devctl.py up apps`
- `python3 devctl.py up`
  - 只负责宿主机应用进程
- `bash scripts/deploy/validate.sh`
  - 跑基础设施、代理、指标、前端 E2E 和数据链路校验

## 8. 初始化后的检查命令

```bash
python3 devctl.py status
python3 devctl.py logs vllm
python3 devctl.py logs litellm
python3 devctl.py logs chainlit
python3 devctl.py logs admin
```

常用访问入口通常是：

- Chat: `http://127.0.0.1:8088/chat`
- Admin: `http://127.0.0.1:8088/admin`
- Grafana: `http://127.0.0.1:8088/grafana/`

## 9. 最容易踩的坑

- 只执行了 `python3 devctl.py up`，但没有先做 `init-infra.sh` 和 `init-db.sh`
- `MODEL_PATH` 在新机器上不存在
- `DATABASE_URL` 和 `LITELLM_DATABASE_URL` 被错误地配置成同一个库
- 只迁了仓库代码，没有迁证书文件和 `CAPS_STORAGE_ROOT`
- 新机器无法拉 Docker 镜像或 Node 依赖，但没有提前准备离线资源
- 迁移包恢复后没有再执行 `init-db.sh`，导致 Redis 热缓存和引导配置未同步
- 老文档里的默认值和 `infra/env/*.env` 当前实际值不一致，尤其是 token 限额、端口和域名配置

## 10. 推荐的最短迁移口令

如果是“新机器空部署，不保留旧数据”，按这个顺序就够了：

```bash
bash scripts/deploy/prepare-env.sh
python3 devctl.py init
python3 devctl.py up apps
bash scripts/deploy/validate.sh
```

如果是“迁旧数据”，按这个顺序更稳妥：

```bash
bash scripts/deploy/backup-state.sh
# 把生成的 tar.gz 带到新服务器
bash scripts/deploy/prepare-env.sh
bash scripts/deploy/init-host.sh
bash scripts/deploy/init-infra.sh
bash scripts/deploy/restore-state.sh /path/to/ncoai-migration-<timestamp>.tar.gz
python3 devctl.py up apps
bash scripts/deploy/validate.sh
```
