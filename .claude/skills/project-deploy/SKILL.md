---
name: project-deploy
description: 项目部署 skill — 把工作台部署到线上服务器，以及线上运行/重启/停止/查日志/更新。当用户说“部署到线上”“发布到服务器”“上线”“重启线上服务”“停掉线上”“看线上日志”“更新线上代码”“deploy”“部署一下”时使用。首次部署会装 Python、建 systemd 服务、配 nginx 反代；之后是增量更新代码并重启。
version: 0.1.0
---

# 项目部署 —— 线上部署与运维

把 `workbench_server` 部署到一台 Linux 服务器，用 systemd 常驻、nginx 反代对外。

前端就是 `.openharness/plugins/investment-research/workbench/`，但**它不是独立静态站**：
频道、私信、任务、文件、讨论每一个动作都在调 `/api/*` 和 SSE。只传静态文件上去
得到的是一个打开即报错的空壳。所以部署永远是**前端 + 后端一起**，由同一个进程在
同一个来源上提供。

---

## 每次部署前：先问凭据

**服务器 IP、账号、密码每次都向用户当场索取，不从任何文件读取，也绝不写进文件、
脚本、提交或日志。**

开始任何部署动作前，先问用户这三项：

```
请提供本次部署的服务器信息：
1. IP / 主机名
2. SSH 账号
3. SSH 密码
```

拿到后只以环境变量的形式传给单条命令：

```powershell
$env:DEPLOY_HOST='<用户提供的IP>'
$env:DEPLOY_USER='<用户提供的账号>'
$env:DEPLOY_PASSWORD='<用户提供的密码>'
.\.venv\Scripts\python.exe .claude\skills\project-deploy\scripts\deploy.py
```

用完清掉：

```powershell
Remove-Item Env:DEPLOY_PASSWORD, Env:DEPLOY_HOST, Env:DEPLOY_USER
```

需要 `paramiko`（Windows 上没有 sshpass，而 `ssh` 在非交互环境里会卡在密码提示）：

```powershell
.\.venv\Scripts\python.exe -m pip install paramiko
```

---

## 判断：首次部署还是增量更新

先连上去看一眼：

```bash
systemctl is-active <服务名> ; ls /opt/<项目名>/venv/bin/python
```

* 服务不存在 / 没有 venv → **首次部署**，按 `references/server-setup.md` 走一遍
* 都在 → **增量更新**，只跑下面的更新流程

---

## 增量更新（日常用这个）

```powershell
.\.venv\Scripts\python.exe .claude\skills\project-deploy\scripts\deploy.py
```

脚本做的事：打包 `src` + `.openharness/plugins` + `pyproject.toml`（不含 venv、
数据库、上传文件、`__pycache__`），上传解包，重启服务，最后分别探测本地端口和公网
域名的状态码。

**只替换代码，不动服务器上的数据库和上传目录**——线上的频道、消息、文件在更新后
仍然在。

---

## 线上运维

| 要做的事 | 命令 |
|---|---|
| 看状态 | `systemctl status <服务名> --no-pager` |
| 实时日志 | `journalctl -u <服务名> -f` |
| 最近日志 | `journalctl -u <服务名> --no-pager -n 50` |
| 重启 | `systemctl restart <服务名>` |
| 停止 | `systemctl stop <服务名>` |
| 启动 | `systemctl start <服务名>` |
| 开机自启 | `systemctl enable <服务名>`（首次部署已设） |
| 本地探活 | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<端口>/` |
| 公网探活 | `curl -s -o /dev/null -w '%{http_code}\n' https://<域名>/` |

`scripts/remote.py` 可以直接跑一串远程命令，凭据同样走环境变量：

```powershell
.\.venv\Scripts\python.exe .claude\skills\project-deploy\scripts\remote.py "systemctl restart <服务名>" "journalctl -u <服务名> -n 20 --no-pager"
```

---

## 部署完必须验证到这一步

状态码 200 只是起点，**不是**成功判据。逐条确认：

1. `systemctl is-active` 是 `active`
2. 本地 `curl http://127.0.0.1:<端口>/` → 200
3. 公网 `curl https://<域名>/` → 200
4. 浏览器打开域名，**控制台 0 报错**（js/css 404 是最常见的坑，见下）
5. 页面里 `state.channels` / `state.agents` 有数据（说明 `/api/workspace` 通了）
6. **实发一个话题，看到 Agent 真的回复**（说明模型凭据在服务器上可用、SSE 没被缓冲）

第 4 和第 6 步最容易被跳过，也最容易出问题。

---

## 三个必踩的坑

### 1. js/css 全 404，但 index.html 正常

宝塔/常见 nginx 模板里有这类规则：

```nginx
location ~ .*\.(js|css)?$ { expires 12h; }
```

**正则 location 的优先级高于前缀 `location /`**，所以 js/css 被它抢去站点根目录
（那里是空的）→ 404，而 `/` 走代理正常。

解法：在代理配置里也写一条同类正则并**让它先出现**（正则 location 之间谁先出现谁赢）：

```nginx
location ~ .*\.(js|css|png|jpg|jpeg|gif|svg|ico|woff2?|map)$ { proxy_pass ...; }
location / { proxy_pass ...; }
```

### 2. 老系统装不上新 Python

CentOS 7 的 OpenSSL 是 1.0.2，Python 3.11+ 拒绝用它编译；先编 OpenSSL 再编 Python
在小内存机器上非常慢。

解法：用预编译独立版（`astral-sh/python-build-standalone`，针对 glibc 2.17），解压即用。
详见 `references/server-setup.md`。

### 3. 国内服务器拉 GitHub 极慢

直连 github.com 实测约 28 KB/s（100 MB 要一小时）。先花 12 秒探测几个镜像再下：

```bash
timeout 12 curl -fL -s -o /tmp/probe.bin '<镜像URL>'; du -k /tmp/probe.bin
```

实测 `gh-proxy.com` 可达 8.8 MB/s。**别默认直连，先测速。**

---

## 依赖：装小不装全

服务端**只需要导入期真正用到的**几个包，不需要整套研究流程的依赖：

```
httpx pydantic pyyaml anthropic pypdf openai croniter mcp
```

CrewAI 那套（chromadb / lancedb 等）体积和内存都很重，小机器扛不住，且**群聊讨论、
私信、Skill、文件、任务都不依赖它**——只有 `@Planner` 的完整研究流程需要。

要确认导入期依赖，用 AST 只看模块级 import（比 `pip install -e .` 快得多也准得多），
或者直接迭代：跑一次导入，缺什么装什么，直到导入通过。`scripts/deploy.py` 之外的
首次安装流程见 `references/server-setup.md`。

---

## 安全：必须向用户说清楚的事

工作台**自身没有任何鉴权**。本机运行只绑 `127.0.0.1` 所以无所谓；一旦挂到公网域名，
任何知道地址的人都能：

* 发起讨论（消耗模型 API 配额）
* 读取全部消息、任务、文件、研究数据
* 增删 Agent 和频道、上传文件

**首次部署前必须把这一条明确告诉用户，让用户决定**是否加 HTTP Basic 认证或 IP 白名单
（nginx 层几分钟即可）。用户选择不加，就照做，但不要默默上线。

模型 API Key 写在 systemd unit 的 `Environment=` 里，unit 文件 `chmod 600`（仅 root 可读）。
**任何密钥、密码都不要写进仓库里的文件。**
