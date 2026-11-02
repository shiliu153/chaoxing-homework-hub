# 多用户改造设计（前后端分离）

本文说明这次改造后的职责划分、注册与账号识别流程、数据隔离方式，以及关键模块与接口的变化。
实现代码见 `app/`（后端）与 `web/`（前端），部署脚本见 `deploy/`。

---

## 1. 目标与边界

| 目标 | 做法 |
| --- | --- |
| 前后端分离 | 后端只提供 `/api/v1/**` JSON；前端是纯静态文件，由 nginx 直接托管 |
| 多用户 | 用户名 + 密码注册登录，每个用户独立配置、独立采集、独立提醒 |
| 账号匹配 | 登录态 → `user_id` → 该用户自己的 `config.yaml` → 对应的采集引擎 |
| 数据隔离 | 认证层、路由层、引擎层、文件层、扫码会话层五道隔离 |

不改变的部分：`cxreminder/` 采集内核（登录、加密、解析、去重、推送）保持原样，只是从「全局一份配置」变成「每个用户一份配置」。

---

## 2. 前后端职责划分

| 层 | 位置 | 职责 | 不负责 |
| --- | --- | --- | --- |
| 前端 | `web/`（index.html / app.js / style.css / config.js） | 渲染、表单校验、路由状态、轮询展示、二维码展示 | 任何采集、任何凭据存储 |
| 后端 API | `app/main.py` | 注册登录、会话校验、参数校验、限流、编排引擎 | 页面渲染 |
| 租户层 | `app/tenancy.py` | 用户目录、配置读写、历史数据迁移、引擎注册表 | HTTP 语义 |
| 身份层 | `app/store.py` / `app/security.py` | 用户表、会话表、scrypt 口令哈希、令牌指纹 | 业务数据 |
| 引擎层 | `app/engine.py` | 单用户采集调度、状态、汇总、忽略、详情缓存 | 跨用户逻辑 |
| 采集内核 | `cxreminder/` | 学习通/雨课堂登录与解析、去重、提醒通道 | 用户概念 |

前端与后端之间只有 HTTP：前端不读配置文件，后端不生成 HTML。

---

## 3. 部署拓扑

同源（默认，推荐）：

```
浏览器 ──https──> nginx ──┬── /            静态文件（/www/wwwroot/<域名>/web）
                          └── /api/  ────> uvicorn 127.0.0.1:8000（FastAPI）
```

跨域（可选）：前端放任意静态托管，`web/config.js` 里把 `window.CX_API_BASE` 指到后端域名，
后端设置 `CX_CORS_ORIGINS=https://前端域名`（需 https + `SameSite=Lax` 的 Cookie 才能带上）。

---

## 4. 注册与账号识别流程

### 4.1 注册

```
① 前端 POST /api/v1/auth/register {username, password, display_name?, invite_code?}
② 后端校验：用户名 3-32 位 / 密码 ≥8 位 / 邀请码（若开启）/ 同名冲突 → 409
③ store.create_user()：scrypt 哈希入 users 表
④ tenancy.ensure_workspace(uid)：建 data/users/<uid>/ 与初始 config.yaml
⑤ manager.start_user(uid)：为该用户拉起采集线程（账号为空时不会真的请求）
⑥ 生成会话令牌 → Set-Cookie: cx_session（HttpOnly, SameSite=Lax, 30 天）
⑦ 返回 {user, need_bind: true}
```

注册后前端自动打开「设置」，引导绑定学习通账号——**没有绑定之前不会产生任何采集请求**。

### 4.2 登录与「账号识别」

```
① 前端 POST /api/v1/auth/login {username, password}
② store.authenticate()：查 users → scrypt 校验 → 更新 last_login_at
③ 失败按 IP + 用户名双键计数，超过 6 次锁定 120 秒（AttemptLimiter）
④ 成功签发新令牌（库里只存 sha256 指纹）
⑤ 之后每个请求：Cookie/Authorization → token_hash → sessions → user_id
⑥ current_user 依赖拿到 user_id → manager.engine(user_id) → 载入该用户 config.yaml
```

「根据身份匹配账号信息」就发生在第 ⑥ 步：**账号不是从请求参数里取的，而是由会话推导出来的**，
所以客户端无法通过改参数访问别人的账号。

### 4.3 绑定学习通 / 雨课堂

| 平台 | 入口 | 落盘位置 |
| --- | --- | --- |
| 学习通 | 设置 → 账号 / 密码 / Cookie | `data/users/<uid>/config.yaml` → `account.*` |
| 雨课堂 | 设置 → 扫码（微信）或粘贴 sessionid | 同上 → `yuketang.cookie` |

扫码是**按用户隔离的**：`/yuketang/login/start` 把 `user_id` 写进内存会话，
`qr` / `status` 只允许发起人读取，别人的 sid 一律 404。

---

## 5. 多用户数据隔离

五层，任何一层单独失效都不会导致串号：

| 层 | 机制 |
| --- | --- |
| 认证层 | 无有效会话 → 401；令牌不可猜测（32 字节随机），库里只有指纹 |
| 路由层 | 所有数据接口的 user 都来自 `Depends(current_user)`，不接受 `user_id` 入参 |
| 引擎层 | `EngineManager` 按 `user_id` 缓存 `Engine`，每个 Engine 绑定一份配置和一个数据目录 |
| 文件层 | 全部路径由 `Tenancy.user_dir(uid)` 拼接，用户目录 0700、配置 0600；`write_raw` 会强制把 `storage.data_dir` 覆写为本用户目录 |
| 扫码层 | 二维码会话记录 `user_id`，非本人查询直接当作不存在 |

目录结构：

```
data/
├── app.db                  # users / sessions（SQLite，0600）
├── users/
│   ├── 1/
│   │   ├── config.yaml     # 该用户的学习通/雨课堂账号、阈值、通道
│   │   ├── state.json      # 去重状态
│   │   ├── ignored.json    # 忽略清单
│   │   ├── cookies.json    # 学习通登录态
│   │   ├── task_cache/     # 详情缓存
│   │   ├── summaries/      # 每轮汇总
│   │   ├── events.jsonl    # 提醒历史
│   │   └── reminder.log    # 采集日志
│   └── 2/ …
└── web_password            # 仅历史迁移时读取，之后不再使用
```

---

## 6. 关键模块

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `app/store.py` | 新增 | SQLite 用户/会话表；`authenticate`、`create_session`、`resolve_session`、`revoke_all` |
| `app/security.py` | 新增 | scrypt 口令哈希、令牌指纹、`AttemptLimiter`、`SecretBox`（可选加密，见 §9） |
| `app/tenancy.py` | 新增 | `Tenancy`（目录/配置/迁移）+ `EngineManager`（引擎注册表 + 全局并发闸门） |
| `app/main.py` | 重写 | 全部接口迁到 `/api/v1`，加 `current_user` 依赖；不再托管页面 |
| `app/engine.py` | 改造 | 增加 `run_semaphore`（全局最多 N 个用户同时采集）；其余逻辑不变 |
| `web/` | 新增 | 登录/注册页 + 仪表盘；`config.js` 提供 `CX_API_BASE` |
| `deploy/nginx-site.conf` | 改造 | `root` 指向前端目录，`/api/` 反代后端 |
| `deploy/chaoxing-web.service` | 改造 | 新增 `CX_DATA_DIR` / `CX_LEGACY_CONFIG` / `CX_ALLOW_REGISTRATION` |

旧文件 `app/auth.py`（单口令 + 本地令牌文件）已被 `store.py` + `security.py` 取代，不再使用。

---

## 7. 接口调整

| 旧（单用户） | 新（多用户） | 说明 |
| --- | --- | --- |
| `POST /api/login {password}` | `POST /api/v1/auth/register` | 新增注册 |
| — | `POST /api/v1/auth/login {username,password}` | 账号密码登录 |
| `POST /api/logout` | `POST /api/v1/auth/logout` | 吊销当前令牌 |
| `GET /api/session` | `GET /api/v1/auth/me` | 返回用户 + 绑定状态 |
| — | `POST /api/v1/auth/password` | 校验原密码，改密后吊销全部会话 |
| `GET /api/overview` | `GET /api/v1/overview` | 按当前用户返回 |
| `POST /api/refresh` | `POST /api/v1/refresh` | 只触发自己的采集 |
| `GET /api/task/{key}` | `GET /api/v1/tasks/{key}` | 只能查自己汇总里的任务 |
| `POST /api/ignore` / `unignore` | `POST /api/v1/ignore` / `unignore` | 写自己的 ignored.json |
| `GET/POST /api/settings` | `GET/POST /api/v1/settings` | 读写自己的 config.yaml |
| `POST /api/diagnose` | `POST /api/v1/diagnose` | 只诊断自己的账号 |
| `POST /api/test-notify` | `POST /api/v1/notify/test` | 只用自己的通道 |
| `GET /api/logs` | `GET /api/v1/logs` | 只读自己的 reminder.log |
| `POST /api/yt/login/*` | `POST /api/v1/yuketang/login/*` | 增加会话归属校验 |
| `GET /`（返回 HTML） | nginx 静态 | 后端不再托管页面 |

`GET /api/health` 保留（同时提供 `/api/v1/health`），返回 `mode: multi-user`。

---

## 8. 调度与并发

- 每个用户一个 `Engine`，各自一个 daemon 线程，轮询间隔取该用户配置（≥60s）。
- `EngineManager.poll_slots`（`CX_MAX_CONCURRENT_POLLS`，默认 2）是**全局**信号量：
  同一时刻最多 2 个用户真正在跑采集，其余排队，避免多人同时整点轮询把学习通/雨课堂打满。
- 单用户内部仍是串行：`_run_lock` 保证同一用户不会重复采集。

容量参考：单用户一轮（25 门学习通课 + 54 门雨课堂课）约 3 分钟。10 个活跃用户、15 分钟间隔、
并发 2 的情况下，理论占用约 30 分钟/小时，仍在安全区内；用户更多时应调大间隔或加机器。

---

## 9. 迁移与回滚

首次启动时若 `users` 表为空且检测到旧 `config.yaml`：

1. 用旧网页访问口令（`data/web_password`）创建 `admin` 账号；
2. 旧配置复制到 `data/users/1/config.yaml`，`storage.data_dir` 改写为该目录；
3. `state.json` / `ignored.json` / `cookies.json` / `events.jsonl` / `task_cache/` / `summaries/` 移入 `users/1/`；
4. 旧配置改名为 `config.yaml.migrated`，避免重复迁移。

回滚：停止服务 → 解压备份 → 恢复旧版代码 → `systemctl start`。

---

## 10. 已知限制与后续

1. **账号密码仍是明文存在 0600 的 `config.yaml`**（与 CLI 版一致）。`security.SecretBox` 已实现
   Fernet 加解密，但尚未接入引擎加载链路；接入方式是把 `account.password` 改成 `password_enc`，
   并在 `load_config` 前解密。
2. **没有配额**：单个用户可把轮询间隔调到 60 秒，会占用全局并发名额。生产环境建议加
   `min_interval` 限制或按用户限流。
3. **注册默认开放**：设置 `CX_ALLOW_REGISTRATION=0` 关闭注册，或设置 `CX_INVITE_CODE` 启用邀请码。
4. **单 worker**：扫码会话存在进程内存，因此必须 `--workers 1`；要多副本需把会话挪到 Redis。
5. **没有找回密码/邮件验证**：忘记密码需要管理员在服务器上重置（见部署文档）。
