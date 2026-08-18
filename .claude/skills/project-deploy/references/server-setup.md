# 首次部署：从空服务器到能访问

只在服务器上还没有这套服务时走一遍。之后用 `scripts/deploy.py` 增量更新即可。

以下所有 `<占位符>` 都由本次部署当场确定，**不要写死在任何文件里**：

| 占位符 | 含义 | 示例形态 |
|---|---|---|
| `<项目名>` | 服务端目录名 | 一个短的英文名 |
| `<服务名>` | systemd 服务名 | 同上 |
| `<端口>` | 后端监听端口，只绑回环 | 8788 |
| `<域名>` | 对外访问的域名 | 站点已配好证书的那个 |
| `<站点目录>` | 面板里该域名的根目录 | `/www/wwwroot/<域名>` |

---

## 0. 先摸清服务器

```bash
cat /etc/os-release | head -2      # 发行版
ldd --version | head -1            # glibc 版本，决定能不能用预编译 Python
nproc; free -m | head -2; df -h /  # 核数、内存、磁盘
nginx -v; systemctl is-active nginx
python3 --version                  # 多半没有
curl -sI -m 15 https://<模型API域名> | head -1   # 出网能不能到模型服务
```

内存是关键：小于 1 GB 要谨慎，systemd 里记得设 `MemoryLimit`。

---

## 1. 装 Python 3.12（预编译，不编译）

老发行版（CentOS 7 等）的 OpenSSL 太旧，源码编译 Python 3.11+ 会失败。用
`astral-sh/python-build-standalone` 的 `install_only` 包，针对 glibc 2.17 构建，解压即用。

先测速再下（国内直连 GitHub 通常只有几十 KB/s）：

```bash
for m in "https://github.com" "https://gh-proxy.com/https://github.com" "https://ghfast.top/https://github.com"; do
  timeout 12 curl -fL -s -o /tmp/probe.bin "$m/<资源路径>"
  echo "$m -> $(du -k /tmp/probe.bin 2>/dev/null | cut -f1) KB / 12s"; rm -f /tmp/probe.bin
done
```

取最快的那个下载并解压：

```bash
mkdir -p /opt/py312
curl -fL --retry 3 -o /tmp/py312.tar.gz '<最快镜像的完整URL>'
tar -xzf /tmp/py312.tar.gz -C /opt/py312 --strip-components=1
/opt/py312/bin/python3 --version
/opt/py312/bin/python3 -c "import ssl, sqlite3; print(ssl.OPENSSL_VERSION)"
```

最后一行要能打印出 OpenSSL 3.x —— 说明 ssl 和 sqlite3 都可用。

**下载放后台跑**：几十兆的下载超过工具超时会被打断，留下半截文件，解压时报
`unexpected end of file`。用 `nohup ... &` + 轮询，或确保命令超时足够长。

---

## 2. 传代码

只传服务器真正要跑的：`src`、`.openharness/plugins`、`pyproject.toml`。
**不传** venv、数据库、上传文件、`__pycache__`。

`scripts/deploy.py` 已经实现这个打包上传逻辑，首次部署直接复用它。

---

## 3. 建虚拟环境，按需装依赖

```bash
/opt/py312/bin/python3 -m venv /opt/<项目名>/venv
/opt/<项目名>/venv/bin/python -m pip install -q --upgrade pip \
  -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
/opt/<项目名>/venv/bin/python -m pip install -q \
  httpx pydantic pyyaml anthropic pypdf openai croniter mcp \
  -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
```

验证导入（这一步过了才继续）：

```bash
cd /opt/<项目名> && PYTHONPATH=/opt/<项目名>/src \
  /opt/<项目名>/venv/bin/python -c "import openharness.invest_research.workbench_server; print('ok')"
```

缺什么装什么，循环到通过为止。注意包名和模块名不一定一致（`yaml` → `pyyaml`）。

---

## 4. systemd 服务

写 `/etc/systemd/system/<服务名>.service`：

```ini
[Unit]
Description=<项目名> workbench
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/<项目名>
Environment=PYTHONPATH=/opt/<项目名>/src
Environment=PYTHONUNBUFFERED=1
Environment=OPENHARNESS_DATA_DIR=/opt/<项目名>/.openharness/data
Environment=ARK_API_KEYS=<逗号分隔的模型Key，由用户当场提供>
Environment=ARK_API_KEY=<其中第一把>
Environment=OPENAI_API_KEY=<同上，Ark 是 OpenAI 兼容接口>
Environment=TAVILY_API_KEY=<可选，联网检索用>
ExecStart=/opt/<项目名>/venv/bin/python -m openharness.invest_research.workbench_server --port <端口>
Restart=always
RestartSec=3
MemoryLimit=900M

[Install]
WantedBy=multi-user.target
```

```bash
chmod 600 /etc/systemd/system/<服务名>.service   # 里面有密钥，只给 root
mkdir -p /opt/<项目名>/.openharness/data
systemctl daemon-reload && systemctl enable --now <服务名>
systemctl is-active <服务名>
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<端口>/
```

`MemoryLimit` 不是可选项：并发讨论会同时开几个模型连接，小机器上一次失控就把整台
机器拖垮。

---

## 5. nginx 反代

后端只绑回环，nginx 负责对外和 TLS（面板通常已经给该域名签好证书）。

新建 `/www/server/panel/vhost/nginx/proxy/<域名>/<项目名>.conf`：

```nginx
    location ~ .*\.(js|css|png|jpg|jpeg|gif|svg|ico|woff2?|map)$ {
        proxy_pass http://127.0.0.1:<端口>;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        client_max_body_size 32m;
    }

    location / {
        # 同上，内容完全一样
    }
```

三处都是必须的：

* **静态正则块**：面板模板里有 `location ~ .*\.(js|css)?$` 缓存规则，正则优先级高于
  前缀 `location /`，不写这块 js/css 会被它抢去空的站点根目录 → 全 404
* **`proxy_buffering off` + 长 `read_timeout`**：SSE 实时事件流，缓冲会让消息卡住不出，
  短超时会把正在进行的讨论掐断
* **`client_max_body_size`**：上传文件用

让 vhost 包含它（**改 include 而不是重写 vhost**，这样面板以后再编辑也不会丢）：

```bash
cp -n /www/server/panel/vhost/nginx/<域名>.conf /www/server/panel/vhost/nginx/<域名>.conf.bak
grep -q 'proxy/<域名>' /www/server/panel/vhost/nginx/<域名>.conf || \
  sed -i 's#include /www/server/panel/vhost/rewrite/<域名>.conf;#&\n    include /www/server/panel/vhost/nginx/proxy/<域名>/*.conf;#' \
  /www/server/panel/vhost/nginx/<域名>.conf
nginx -t && systemctl reload nginx
```

`include` 要插在那些静态缓存 location **之前**（rewrite 那行通常就在前面）。

---

## 6. 验证

按 SKILL.md 里“部署完必须验证到这一步”的 6 条逐条过，特别是浏览器控制台和实发一个
话题看 Agent 是否真的回复。

---

## 已知限制

* **`@Planner` 的完整研究流程需要 CrewAI**，依赖很重（chromadb / lancedb 等），小内存
  机器装不动也跑不动，默认不装。群聊讨论、私信、Skill、文件、任务都不受影响。
* **本地 Agent（Codex 等）仍然可用**：桥接器跑在访问者自己的电脑上，浏览器直连
  `127.0.0.1`。但启动桥接器时要把允许来源改成线上域名：
  `--allow-origin https://<域名>`。
