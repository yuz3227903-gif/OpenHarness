# 把本机的 Codex 接进工作台

整条链路：

```
浏览器（工作台页面）
   │  只走 127.0.0.1，不经过任何服务器
   ▼
Local Bridge（本机，端口 18789）
   │  按 argv 启动子进程，不经过 shell
   ▼
codex exec --json   ← npm 装的 Codex CLI，跑在你选的工作目录里
```

Bridge 只做一件事：**在你自己的机器上启动 Codex，把它的输出翻译成工作台认识的事件**。
它不代理模型、不上传代码、不需要额外的网关。

---

## 一、装 Codex

前置：Node.js（`node -v` 能打印版本即可，你这台已经有了）。

```powershell
npm install -g @openai/codex
codex --version
```

装完必须先登录一次，让 Codex 拿到自己的凭据：

```powershell
codex login
```

> 这一步是 Codex 自己的登录，和工作台无关。Bridge 从头到尾不碰你的 OpenAI 凭据。

验证它能无界面跑一轮（**接入前一定要先跑通这一条**）：

```powershell
cd D:\your\project
codex exec --json --skip-git-repo-check "用一句话说明这个目录里有什么"
```

看到一行行 JSON 就对了。如果这条命令跑不通，工作台里也不会通——先把这里解决。

---

## 二、启动 Bridge

在项目目录下：

```powershell
cd D:\ideaProject\演示demo\agentopenclaw\duoagent\OpenHarness
.\.venv\Scripts\python.exe -m openharness.local_bridge.server --port 18789 --allow-origin http://127.0.0.1:8788
```

`--allow-origin` 写工作台的地址。**这个终端窗口别关**——配对码会打印在这里。

它只监听 `127.0.0.1`，局域网里的其他机器连不上。

---

## 三、在工作台里连接

1. 左栏「成员」→ 右上角 **创建 Agent** → 选 **连接本地 Agent**
2. **第 1 步 · 连接 Bridge**：页面自动探测 18789。
   - 找到但未配对 → 点「发起配对」→ **去 Bridge 的终端窗口看那 6 位数字** → 填进去 → 确认配对
   - 配对码只在 Bridge 自己的终端里出现，页面永远拿不到它——这就是"能看到这台机器"的证明
3. **第 2 步 · 选择 Agent**：列出本机检测到的 CLI。Codex 显示「已安装 + 版本号」才能连；显示「未检测到」就是 PATH 里没有
4. **第 3 步 · 配置**：
   - 名称：随便起，比如 `我的 Codex`
   - **工作目录**：Codex 实际操作的目录，只能填这个目录里的东西
   - 权限模式：`ask`（危险命令先问你）/ `accept_edits` / `read_only`
5. 创建完成后，它出现在成员列表里，带一个 `codex` 徽章

---

## 四、跟它对话

在「成员」页找到它 → 点「私信」→ 就是一个独立会话。

发一句话，会看到：

| 你看到的 | 对应 Codex 的事件 |
|---|---|
| 正文回答 | `agent_message` |
| 灰色斜体的思考 | `agent_reasoning` |
| `$ 命令` + 黑底输出 | `exec_command_begin` / `output_delta` / `command_end` |
| 「修改 xxx.py」 | `patch_apply` |
| 黄框「该操作需要你确认」 | 命中危险命令，等你点允许/拒绝 |
| 橙色「工作目录之外」 | Codex 碰了你指定目录以外的文件 |

Codex 会报告自己的 session id，下一轮自动带 `--resume` 接着聊，不是每次从头开始。

「中止当前任务」随时能停：Bridge 会终止那个子进程。

---

## 五、安全边界（都是代码里的，不是说明书上的）

- Bridge 只绑 `127.0.0.1`
- 配对前所有接口 401；配对码 6 位、3 分钟过期、错 5 次作废
- Token 绑定来源（origin），换个来源就失效
- CORS 只回显被允许的来源，**从不用 `*`**
- 子进程用 argv 数组启动，`shell=False`——提示词和路径不可能被当成命令
- 危险命令（`rm -rf`、`git push` 等）在 `ask` 模式下先要你确认，**超时默认拒绝**
- 工作目录外的文件操作会被打标高亮（Bridge 拦不住 CLI 自己的文件访问，但不让它无声通过）
- 密钥、token 从不写进日志，界面只显示 `key#1` 这种编号

---

## 六、出问题时

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 页面说「没有检测到本机 Bridge」 | Bridge 没起，或端口不对 | 回第二步，确认终端还开着 |
| 「拒绝了当前页面的来源」 | `--allow-origin` 和工作台地址对不上 | 用工作台真实地址重启 Bridge |
| Codex 显示「未检测到」 | PATH 里没有 `codex` | `npm install -g @openai/codex`，重开终端再重启 Bridge |
| 对话回「Codex 未安装或不在 PATH 中」 | Bridge 进程启动时的 PATH 里没有 | 在能跑 `codex --version` 的终端里重启 Bridge |
| 回复里是一行行 JSON 原文 | Codex 版本的事件名和这里的翻译对不上 | 把那几行发我，补一条映射即可（见下） |
| 一直转但没输出 | Codex 在等交互式输入 | 确认 `codex exec` 那条命令在终端里能独立跑完 |

---

## 七、版本漂移怎么办

Codex 的 JSON 事件名在版本之间改过好几次。所以翻译层是**按族匹配**的（含 `command_begin` 就当命令开始，含 `reasoning` 就当思考），认不出来的：

- 是 JSON 但不认识 → 安静忽略
- 不是 JSON → 当作正文显示出来

也就是说升级 Codex 最坏的情况是"少了几个活动标记"，不会变成空白回复。

要加新映射就改 `src/openharness/local_bridge/adapters/codex.py` 里的 `_events_for`，对应测试在 `tests/invest_research/test_local_bridge_codex.py`。

---

## 八、验证到什么程度

诚实说明，避免误会：

- ✅ 事件翻译、命令拼装、resume、容错：22 条单元测试覆盖，全绿
- ✅ Bridge 本身（配对、鉴权、SSE、会话、审批、取消、工作目录）：真实子进程测试，全绿
- ✅ 配对、检测、创建本地 Agent、错误如实上报：浏览器实测过
- ⚠️ **没有验证过的**：真实 `@openai/codex` 跑完一轮完整对话。这台机器上没装 Codex（`codex` 不在 PATH），装它需要你的 OpenAI 账号登录，所以这一步得你来。

按第一步装好之后，第四步跑一句话就能确认。如果输出和翻译对不上，把 Bridge 终端里的原始 JSON 发我。
