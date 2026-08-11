# Investment Research Agent Plugin

本目录是七个上市公司投研 Agent 的 OpenHarness 项目级定义。角色、Prompt、目标工具权限和结构化合同已落地；运行时由 `InvestmentResearchRuntimeAdapter` 统一装载并在当前 Run 中执行。它是开发验证底座，不等于已经完成网页产品或生产投研系统。

## 当前包含

- `agents/*.md`：七个 Agent 的 OpenHarness 原生定义与 Role Prompt；
- `prompts/governance.md`：全队证据、协作、权限与合规规则；
- `schemas/*.json`：由 `src/openharness/invest_research/contracts.py` 生成的输入／输出 Schema；
- `plugin.json`：项目 Plugin 清单。

稳定业务 ID 为：

```text
planner
fundamental
industry_competition
market_catalyst
risk
reviewer_arbiter
report_writer
```

对应 OpenHarness 运行 ID 为 `investment-research:<business_id>`。后续 CrewAI、数据库、Task、Issue 和证据记录应始终使用业务 ID；只有 RuntimeAdapter 负责转换运行 ID。

## 运行边界

1. 项目 Plugin 默认不会被 OpenHarness 全局信任加载；Planner RuntimeAdapter 会按明确路径单独加载本 Plugin，不修改用户全局配置。
2. Planner 所需的 `tavily_search` 已在本 Plugin 实现，`web_fetch` 复用 OpenHarness 内置实现。Tavily 密钥只从当前进程的 `TAVILY_API_KEY` 读取，不写入 Prompt、结果或日志。
3. `read_uploaded_file`、`calculator` 和 `evidence_query` 已由运行时适配器提供；它们只在当前 Run 和当前 Agent 的权限范围内工作。
4. OpenHarness 0.1.9 的 AgentTool 尚不能完整执行 AgentDefinition 的工具白名单，因此七个角色统一使用独立 ToolRegistry；不启用 OpenHarness 子 Agent 委派。
5. 七个角色均已达到 `validation_ready` 的代码状态，但只有留下真实 DeepSeek 回执后，才能标记为“已实测”。这不等于生产可用，也不接 CrewAI。
6. 本阶段不启用子 Agent、任务委派、Shell、任意文件写入或配置修改。

## 七个 Agent 运行验证

仅检查配置、模型名称和每个角色的工具边界，不调用模型或网络：

```powershell
.\.venv\Scripts\python.exe -m openharness.invest_research.run_planner_smoke --preflight-only
```

当前终端已安全配置 `TAVILY_API_KEY` 后，可以验证单个角色：

```powershell
.\.venv\Scripts\python.exe -m openharness.invest_research.run_planner_smoke --prompt-for-tavily-key --company "宁德时代" --as-of-date 2026-08-11
```

统一验证入口为：

```powershell
.\run-investment-research.ps1 -AgentSmoke fundamental
.\run-investment-research.ps1 -ValidateAllAgents
.\run-investment-research.ps1 -FullChainValidation
```

每次真实验证都会记录模型、Token、工具名、Schema 状态和 Artifact ID；回执不保存原始模型输出，也不保存任何 API Key。验证输出位于 `.openharness/validation/`，正式运行数据位于 `.openharness/data/investment-research.sqlite3`。

不要把 API Key 粘贴到聊天、命令参数、源码或提交记录中。

### 持久配置（Windows 推荐）

不要把明文 Key 写入 `run-openh.ps1`。只需首次运行：

```powershell
.\setup-tavily-key.ps1
```

脚本会隐藏输入，并使用 Windows DPAPI 将凭据保存到当前用户专属的加密文件：

```text
.openharness\secrets\tavily-key.dpapi
```

以后正常启动 OpenHarness：

```powershell
.\run-investment-research.ps1
```

执行 Planner 首次真实验证：

```powershell
.\run-investment-research.ps1 -PlannerSmoke -Company "宁德时代" -AsOfDate 2026-08-11
```

启动验证网页：

```powershell
.\run-investment-research.ps1 -PlannerWeb
```

浏览器会打开 `http://127.0.0.1:8765/`。旧页面仍保留 Planner 入口；七角色验证结果以命令行回执为准，新的七 Agent 页面会在验证编排稳定后接入。关闭启动脚本的 PowerShell 窗口后，网页服务随之停止。

包装脚本只在当前进程及其子进程中设置 `TAVILY_API_KEY`，退出后恢复原值。加密文件不能跨 Windows 用户或跨电脑使用；更换账户或电脑后需重新运行配置脚本。

## Schema 更新

Pydantic 模型是合同唯一事实源。修改模型后运行：

```powershell
.\.venv\Scripts\python.exe -m openharness.invest_research.export_schemas
```

然后运行本地无网络测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests\invest_research -p 'test_*.py' -v
```
