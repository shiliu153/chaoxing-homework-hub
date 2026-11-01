# 学习通待办中心 · 部署记录

> 本文档记录一次真实部署的过程与命令，域名 / IP 已用占位符替代，请替换成自己的值。

## 1. 访问方式

| 项 | 值 |
| --- | --- |
| 地址 | https://<你的域名> |
| 初始访问口令 | 首次启动自动生成，见服务器 `/opt/chaoxing-reminder/data/web_password`（网页「设置」里可改） |
| 服务进程 | systemd `chaoxing-web.service`（uvicorn，仅监听 `127.0.0.1:8000`） |
| 反向代理 | 宝塔面板托管的 nginx（80 / 443 / 888） |
| 证书 | Let's Encrypt，certbot webroot，自动续期（certbot.timer） |

## 2. 部署拓扑

```
浏览器
  │  https://<你的域名>  (A → <服务器公网 IP>)
  ▼
宝塔 nginx  80  → ACME 校验 + 301 跳 https
           443  → proxy_pass 127.0.0.1:8000
  ▼
uvicorn (systemd: chaoxing-web)  127.0.0.1:8000  单 worker
  ├── app/main.py    FastAPI 路由（登录鉴权 / 概览 / 设置 / 触发）
  ├── app/engine.py  后台调度线程 + 事件历史
  └── cxreminder/    复用脚本版的采集与提醒内核
  ▼
学习通站点（httpx/requests 会话，Cookie 缓存于 data/cookies.json）
```

## 3. 服务器目录

```
/opt/chaoxing-reminder/
├── app/                  Web 服务（main.py / auth.py / engine.py / static/）
├── cxreminder/           采集与提醒内核（来自脚本版项目）
├── deploy/               systemd unit、nginx 配置、宝塔注册 SQL
├── venv/                 Python 虚拟环境
├── config.yaml           运行配置（chmod 600，账号密码在此）
└── data/
    ├── state.json        去重状态
    ├── cookies.json      学习通登录态缓存
    ├── events.jsonl      提醒事件历史（网页「提醒记录」）
    ├── reminder.log      运行日志
    ├── web_password      Web 访问口令
    ├── web_secret        会话签名密钥
    └── summaries/        每轮巡检汇总（latest.md / latest.json）
```

## 4. 实际执行的关键步骤

1. **SSH 连通性**：`ssh -i ~/.ssh/moonaws.pem ubuntu@<服务器公网 IP>` → Ubuntu 24.04.5 LTS，`sudo` 免密。
2. **环境勘察**：发现 80/443/888 由**宝塔面板的 nginx** 占用（`systemctl is-active nginx` 为 inactive 是因为面板用 init 脚本托管），另有 n8n 容器占 5678。
3. **关停 n8n**（按要求）：
   ```bash
   sudo docker update --restart=no n8n n8n-postgres
   sudo docker stop n8n n8n-postgres
   ```
   内存占用由 1033MB 降到 757MB。容器保留未删除，需要时可 `docker start n8n n8n-postgres` 恢复。
4. **上传代码**：`tar czf - app cxreminder requirements.txt deploy | ssh ... 'tar xzf - -C /opt/chaoxing-reminder'`。
5. **虚拟环境**：首次 `python3 -m venv` 报 `ensurepip is not available`（Ubuntu 24.04 默认不带 venv 组件），补装后重建：
   ```bash
   sudo apt-get install -y python3.12-venv python3-pip
   python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
   ```
6. **systemd 托管**：`deploy/chaoxing-web.service` → `/etc/systemd/system/`，`enable --now`，验证 `127.0.0.1:8000/api/health` 返回 200。
7. **站点创建**：`/www/wwwroot/<你的域名>`（ACME webroot）+ `/www/wwwlogs/<你的域名>*.log`，vhost 写入 `/www/server/panel/vhost/nginx/<你的域名>.conf`（面板 nginx 通过 `include .../vhost/nginx/*.conf` 加载）。
8. **证书**：
   ```bash
   sudo certbot certonly --webroot -w /www/wwwroot/<你的域名> \
     -d <你的域名> --non-interactive --agree-tos --keep-until-expiring
   ```
   先用 HTTP-only 配置通过校验，再切换为含 443 的最终配置并 reload。
9. **宝塔站点注册**：面板 `default.db` 的 `sites` / `domain` 表原本是**空的**（原有的 `<原站点域名>` 是手写 vhost，从未在面板注册）。执行 `deploy/bt-register.sql` 补登记，`PRAGMA integrity_check` 返回 ok。注意 SQLite 里 `index` 是保留字，必须写成 `` `index` ``。
10. **验证**：本机与公网双向验证 HTTPS 200、TLS 校验通过、未登录 401、登录后能取到概览数据、HTTP 301 跳转正常。

## 5. 日常运维

```bash
# 服务状态 / 日志
systemctl status chaoxing-web
sudo journalctl -u chaoxing-web -n 100 --no-pager

# 重启（改完 config.yaml 或代码后）
sudo systemctl restart chaoxing-web

# 更新代码（本地重传后）
tar czf - -C chaoxing-web app | ssh ... 'tar xzf - -C /opt/chaoxing-reminder'
sudo systemctl restart chaoxing-web

# nginx 校验与重载
sudo /www/server/nginx/sbin/nginx -t && sudo /www/server/nginx/sbin/nginx -s reload

# 命令行排查解析问题（在服务器上）
cd /opt/chaoxing-reminder
./venv/bin/python -m cxreminder doctor -c config.yaml
./venv/bin/python -m cxreminder dump   -c config.yaml --out data/dump
```

## 6. 注意事项

- **不要在面板 UI 里再「保存」这个站点**：面板会用数据库里的默认模板重写 vhost，把反向代理段覆盖掉。要改配置请直接编辑 vhost 文件后 reload。若确实被覆盖，重新 `sudo cp /opt/chaoxing-reminder/deploy/nginx-site.conf /www/server/panel/vhost/nginx/<你的域名>.conf` 即可。
- **证书续期**依赖 `/.well-known/acme-challenge/` 走明文 80，vhost 里这一段落不要删。
- `config.yaml` 含学习通账号密码（600 权限，属主 ubuntu）；`data/web_password` 是网页访问口令，两者都不应进版本库。
- 服务是**单 worker**：调度线程与内存态在同一进程，改成多 worker 会导致多份轮询。
- 停掉 n8n 后，旧站点 `https://<原站点域名>` 会返回 **502**（反代目标已下线）。如需改为跳转到新站，改 `/www/server/panel/vhost/nginx/<原站点域名>.conf` 的 `location /` 为 `return 301 https://<你的域名>$request_uri;`。

## 7. 回滚

```bash
sudo systemctl disable --now chaoxing-web
sudo rm /etc/systemd/system/chaoxing-web.service
sudo mv /www/server/panel/vhost/nginx/<你的域名>.conf /tmp/
sudo /www/server/nginx/sbin/nginx -s reload
# 恢复 n8n（可选）
sudo docker update --restart=always n8n n8n-postgres && sudo docker start n8n n8n-postgres
# 面板数据库（如需撤销站点登记）
sudo cp /www/server/panel/data/default.db.bak-<时间戳> /www/server/panel/data/default.db
```

## 8. 采集逻辑修正记录（2026-11-01）

**现象**：账号密码填写正确、能登录（拿到 25 门课程），但待办恒为 0。

**排查过程**：

1. `latest.json` 显示 `courses: 25, all: 0, errors: 0` —— 登录没问题，是列表页没解析出内容。
2. 直接请求 `/mooc2/work/list`、`/mooc2/exam/exam-list`，返回的都是 1149 字节的「提示」页：
   - 作业：`无权限的操作！`
   - 考试：`参数传递不正确！`
3. 课程列表 `backclazzdata` 和 `courselistdata` 里**都没有 enc 字段**，而 mooc2 接口需要课程级令牌。
4. 逐个试了参考实现常见的参数（`ut=s` / `enc` / `fid` / `t` / 移动端 UA / 复制 Cookie 到上级域）均无效。
5. 用无头浏览器打开课程页并真实点击「作业」，抓到实际请求：

   ```
   /mooc2/work/list?courseId=..&classId=..&cpi=..&ut=s&t=..&stuenc=..&enc=4ecb15f3...
   ```

   回放试验：**只有 `enc` 是必需的**，`t` / `stuenc` 可省；去掉 `enc` 立刻变回「无权限」。
6. 该 `enc` 值写在课程主页的隐藏域里：`<input id="workEnc" name="workEnc" value="...">`，
   **每门课不同**；考试对应 `examEnc`，另外还有 `openc`。

**最终实现**（`cxreminder/client.py`）：

```
GET https://mooc1-1.chaoxing.com/mooc-ans/visit/stucoursemiddle?courseid=..&clazzid=..&vc=1&cpi=..&ismooc2=1&v=2
    → 302 到 mooc2-ans 课程主页，从隐藏域取 enc / workEnc / examEnc / openc
GET /mooc2/work/list?courseId&classId&cpi&ut=s&enc=<workEnc>          (Referer=课程主页)
GET /exam-ans/mooc2/exam/exam-list?courseid&clazzid&cpi&ut=s&enc=<examEnc>&openc=<openc>&t=<ms>
```

解析按真实 DOM 走（`parsers.parse_mooc_list_html`）：

```html
<li data="...workId=57254804&answerId=..&enc=.." aria-label="标题 ; 状态">
  <p class="overHidden2 fl">标题</p><p class="status fl">待批阅</p>
</li>
```

列表页**不含截止时间**，只对状态为「未交/未完成」的条目再进详情页取（每课最多 12 次，避免请求过多）。

**结果**：25 门课 → 113 条记录 → 15 条未交作业（含 1 条已逾期），考试 0 条（各课均返回「暂无考试」）。

**若日后再次出现待办为 0**：先点页面上的「连接诊断」，它会分步显示账号/登录/课程数/抽样课程条数；
若登录与课程都正常而条目为 0，多半是 `workEnc` 隐藏域改名或列表 DOM 变了，
可临时 `pip install playwright && playwright install chromium` 抓一次真实请求再对照修正（排查完记得卸载）。

## 9. 作业内容获取 + 忽略功能（2026-11-01）

### 9.1 作业内容

- **按需拉取**：列表不预取详情（25 门课逐条拉会拖慢每轮巡检），点击任务行时才请求。
- 接口：`GET /api/task/{key}[?refresh=1]` → 从最近的 `latest.json` 里找回任务，用它的详情链接
  `/mooc-ans/mooc2/work/task?courseId=..&workId=..&answerId=..&enc=..` 抓取并解析。
- 解析（`parsers.parse_work_detail`）：

  ```html
  <h2 class="type_tit">一. 简答题（共1题，100分）</h2>
  <div typeName="简答题" id="question406044460">
    <h3 class="workTextWrap">1.<span class="colorShallow">(简答题)</span><p>题干</p></h3>
    <div class="stem_answer">…选项/输入框…</div>
  </div>
  ```

  产出：`groups`（大题结构）、`questions[]`（题号/题型/题干/选项/分值）、`attachments[]`、`total_score`、`text`。
  题型优先取所属大题标题（`typeName` 在 `id="question"` 之前，必须往前开窗口取，否则会抓到下一题）。
- 缓存：`data/task_cache/{key}.json`，TTL 6 小时；「刷新内容」按钮带 `?refresh=1` 强制重取。
- 失败语义：令牌过期时详情页会返回「无权限的操作」，接口返回 502 并提示先点「立即检测」。

### 9.2 忽略功能

- 存储：`data/ignored.json`，结构 `{"keys": {"<task_key>": {title, course, kind, deadline, ignored_at}}}`。
- 生效范围：
  - 网页：从统计与列表中剔除，单独归入「已忽略」标签页，可一键恢复；
  - 提醒：`Runner.ignored_keys()` 读取同一文件，被忽略的任务**不参与新任务/临期/逾期判定**，不再推送。
- 汇总文件（`summaries/latest.json`）仍保留全量待办，方便恢复与审计；过滤只发生在展示与提醒层。
- 接口：`POST /api/ignore {key,title,course_name,kind,deadline}`、`POST /api/unignore {key}`。

### 9.3 验证

- 详情：`循环神经网络作业` → `一. 简答题（共1题，100分）`、1 道题、100 分、题干含「LSTM 手写字识别」。
- 忽略：overview 待办 15 → 14，`ignored` 出现该条；忽略后详情仍可打开（200）；恢复后回到 15。
- 单测：`python -m unittest discover -s tests` → 23 passed（新增 `WorkDetailTest`）。
