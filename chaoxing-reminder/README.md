# 学习通（超星）待办提醒

定时登录学习通，汇总**未完成作业**与**待参加考试**，在「新增任务 / 临近截止 / 已逾期」时主动推送提醒。

> 仅用于你自己账号的待办整理。请遵守学校规定与平台服务条款，不要用于批量爬取、代刷或任何违规用途；脚本默认带请求限速与随机抖动，请勿把轮询间隔调到 30 秒以下。

## 1. 能做什么

| 能力 | 说明 |
| --- | --- |
| 自动登录 | 复刻网页端口令 AES 加密后调用登录接口；登录态缓存到本地，重启不需重登；缓存失效自动重登 |
| 待办采集 | 遍历全部课程，抓取作业列表与考试列表，提取课程名、任务名、截止时间、当前状态 |
| 定时轮询 | 间隔 + 随机抖动，单轮异常自愈（登录失效会清 Cookie 重登），Ctrl+C 优雅退出 |
| 智能提醒 | 新任务即时提醒；剩余 72h / 24h / 3h 各提醒一次（可配）；逾期提醒一次；每日定时汇总 |
| 去重 | 状态文件按「任务指纹」记录，跨进程重启也不会重复轰炸 |
| 多渠道 | 控制台、企业微信/钉钉机器人、Server 酱、PushPlus、通用 Webhook、SMTP 邮件 |
| 结果汇总 | 每轮写 `data/summaries/latest.md` / `latest.json` 与带时间戳的历史文件 |
| 自检排障 | `doctor` 一键检查依赖/配置/登录/解析；`dump` 保存原始页面便于适配改版 |

## 2. 目录结构

```
chaoxing-reminder/
├── cxreminder/
│   ├── __main__.py       python -m cxreminder 入口
│   ├── cli.py            子命令：run / once / doctor / dump / test-notify
│   ├── config.py         YAML 配置加载与校验
│   ├── crypto_aes.py     登录口令 AES-128-CBC 加密
│   ├── client.py         登录、Cookie 缓存、课程与待办抓取
│   ├── parsers.py        HTML/JSON 解析（纯函数，多策略容错）
│   ├── models.py         Course / Task 数据模型
│   ├── state.py          去重状态持久化（原子写入 + 自动备份）
│   ├── notify.py         多通道推送
│   ├── runner.py         轮询主循环、提醒决策、汇总落盘
│   └── utils.py          时区与时间格式化
├── tests/                23 个单元测试（解析 + 提醒决策）
├── config.example.yaml   配置模板
├── requirements.txt
└── run.bat               Windows 双击启动
```

## 3. 依赖与安装

需要 Python 3.9+（已在 3.13 验证）。

```bash
cd chaoxing-reminder
python -m pip install -r requirements.txt
# 或手动安装：pip install requests PyYAML pycryptodome tzdata
```

- `requests`：HTTP 会话与重试
- `PyYAML`：读取配置
- `pycryptodome`：口令 AES 加密（登录必需）
- `tzdata`：Windows 时区数据，保证「剩余时长」按北京时间计算

解析层不依赖 BeautifulSoup，全部用标准库 `re`，减少安装成本与版本冲突。

## 4. 配置

```bash
cp config.example.yaml config.yaml     # Windows: copy config.example.yaml config.yaml
```

主要配置项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `account.username` / `account.password` | — | 手机号/学号 + 密码。**推荐改用环境变量** `CX_USERNAME` / `CX_PASSWORD` |
| `account.cookie` | 空 | 从浏览器复制的 Cookie 整串；出现验证码/异地登录拦截时用它，优先级高于密码 |
| `poll.interval_seconds` | 900 | 轮询频率（秒），最小 30 |
| `poll.jitter_seconds` | 120 | 随机抖动幅度，让请求节奏不固定 |
| `poll.daily_summary_at` | `08:30` | 每天该时刻推送完整清单，留空关闭 |
| `remind.thresholds_hours` | `[72, 24, 3]` | 剩余多少小时各提醒一次，自动去重 |
| `remind.notify_new` | true | 基线之外的新任务立即提醒 |
| `remind.notify_overdue` | true | 逾期提醒一次 |
| `remind.ignore_status_keywords` | 已交/已完成… | 命中即跳过，避免催已完成的任务 |
| `remind.expire_grace_days` | 3 | 截止超过 N 天的旧任务不再提醒 |
| `notify.channels` | console | 可叠加多个通道，逐个发送，互不影响 |
| `storage.data_dir` | `data` | 状态、Cookie 缓存、汇总、日志目录 |
| `dry_run` | false | true 时只打印不真正发外部消息，方便调通道 |

完整字段见 `config.example.yaml` 内注释。

## 5. 运行

```bash
# 1) 先自检：依赖 / 配置 / 登录 / 课程数 / 能否解析出待办
python -m cxreminder doctor -c config.yaml

# 2) 测试提醒通道是否通
python -m cxreminder test-notify -c config.yaml

# 3) 只跑一轮，打印汇总后退出（适合先看效果）
python -m cxreminder once -c config.yaml

# 4) 守护式轮询（常驻）
python -m cxreminder run -c config.yaml
#   调试用：跑 3 轮就退出
python -m cxreminder run -c config.yaml --cycles 3

# 5) 解析不出待办时，把原始响应存下来排查
python -m cxreminder dump -c config.yaml --out dump/
```

Windows 观众可直接双击 `run.bat`（内部为 `python -m cxreminder run -c config.yaml`）。

**开机常驻**

- Windows：任务计划程序 → 创建任务 → 触发器「登录时」→ 操作「启动程序」`pythonw.exe`，参数 `-m cxreminder run -c D:\path\config.yaml`，起始于项目目录。
- Linux：`nohup python -m cxreminder run -c config.yaml >/dev/null 2>&1 &`，或用 systemd 托管（`Restart=always`）。

## 6. 提醒规则（避免刷屏的关键）

1. **首轮基线**：第一次运行（或删除状态文件后）只登记、不逐条播报，仅发一条「当前待办 N 条」的汇总；已经进入的阈值会被封存，尚未进入的阈值后续照常触发。
2. **新任务**：基线之后才出现的任务发「新任务」提醒；若发现时已临近截止，直接按最紧阈值播报，不会「新任务 + 即将到期」连发两条。
3. **阈值去重**：每个任务每个阈值只提醒一次；若错过若干轮一次性跨了多个阈值，只播报最紧的那个。
4. **逾期**：过了截止时间且仍为待办，提醒一次。
5. **每日汇总**：到点推一次完整清单，与增量提醒互不替代。

## 7. 输出产物

```
data/
├── state.json            任务指纹、已发提醒记录（去重置靠它，删掉等于重置基线）
├── cookies.json          登录态缓存（含敏感 Cookie，注意权限）
├── reminder.log          滚动日志，5MB × 3
├── dump/                 dump 子命令产出的原始页面
└── summaries/
    ├── latest.md         最近一轮可读汇总
    ├── latest.json       最近一轮结构化结果
    └── summary-20261101-2045.md
```

## 8. 常见问题

**登录失败：返回非 JSON / 提示需要验证**  
平台对频繁登录、异地 IP 会插入验证码或滑块，脚本无法绕过。请在浏览器正常登录一次，F12 → Network → 任意请求的 `Cookie` 整串复制到 `account.cookie`；或降低登录频次（复用 `cookies.json`，不要每次 `doctor` 都强制登录）。

**能登录但待办为空**  
先 `python -m cxreminder dump -c config.yaml`，查看 `dump/work-*.html` 是否真有内容：

- 文件里有数据但解析为 0 条：说明页面结构变了，调整 `parsers.py` 顶部的 `_ID_PATTERNS` / `_DATETIME_RE` 等正则即可（解析层全是纯函数，可用 `tests/` 里的样例快速验证）。
- 文件本身是登录页或空壳：登录态失效或该课程接口需要额外参数，检查 `cookies.json` 是否过期。

**剩余时间差 8 小时**  
学习通返回北京时间。Windows 缺 `tzdata` 时会退回固定 UTC+8，一般无偏差；若系统时区非东八区且你希望严格对齐，请安装 `tzdata` 并把 `timezone` 显式设为 `Asia/Shanghai`。

**重复提醒 / 想重置基线**  
删除 `data/state.json` 即重置（下一轮重新建立基线，不会补发历史提醒）。

**只想收微信不想看终端**  
保留 `console` 通道便于本地留痕即可；`run` 模式的标准输出可以重定向到文件或 `pythonw.exe` 静默运行。

## 9. 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：时间解析（含中文格式、跨年、关键字优先级）、课程列表 JSON/HTML、作业与考试条目抽取、任务指纹稳定性、首轮基线、阈值去重、逾期只报一次、状态跨实例恢复、汇总渲染。
