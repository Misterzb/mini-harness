# MiniHarness — 一个从零写的 Agent Runtime（可运行 / 可审计 / 可评测）

> **A minimal Agent Runtime built from scratch.** 自研 Harness 内核（Agent Loop / 任务编排 / 四层记忆 / 工具注册表 / Token 预算 / 事件溯源 / 人在回路恢复），用一个电商运营 Agent 承载**五条能力**：查数（语义层 NL2SQL）、诊断、退款（HITL 人工接入）、安全拒绝、知识检索（RAG 向量召回）。读能力经 MCP 协议暴露给外部 Agent；写能力（退款结算）刻意不经 MCP，只能由人工入口批准。
>
> **不是 Dify/Coze 那种平台级 Runtime**，而是把 Agent Runtime 的核心组件做出来、用真实场景验证的应用级内核。

> 🎬 **在线演示**：http://101.201.199.212:8501 （访问口令 `000000`，可点侧栏 6 个演示问题；服务为按需启动的演示实例，若临时不可用见 §12 自行部署）

| 维度 | 实测 |
|---|---|
| 端到端评测 | **500/500** 金标全通过（旧百炼 `qwen4-max` 与新 MaaS 网关 `qwen4-max` 均实测 500/500，逐项一致；报告见 §8） |
| 双内核一致性 | 自研 Loop 与 LangGraph 内核**都 500/500**，九层分布、硬控制逐项一致 |
| 本地测试 | **36/36** pytest 全过（22 加固 + 9 双内核 + 5 UI 契约），不访问网络 |
| 口径回归判别力 | 故意改坏时间窗 → 准确率 1.0 跌到 **0.742** |
| 模型兼容 | 任意 OpenAI 兼容网关（默认 MaaS 中转，旧百炼亦可用）；无 Key 自动降级本地 mock，全演示可跑 |
| 在线演示 | 双进程部署 + UI 口令闸门，真实网关端到端已验证（链接见顶部，一键脚本见 §12） |

---

## 1. 五条演示能力

| # | 能力 | 示例输入 | 看点 |
|---|---|---|---|
| 1 | 查数（NL2SQL） | `上个月GMV是多少` | SemanticPlan → SQL 编译器 → AST 白名单；时间窗写进轨迹参数，可逐条核对 |
| 2 | 诊断 | `分析GMV下降的原因` | 扫描相邻两月 → 点名跌幅最大平台 → 同窗漏斗 + 折扣率，结论来自可核对证据 |
| 3 | 退款 HITL | `给订单O000123退款300元` | 提案落 `proposals` → Loop 挂起 → 人工审批 → 恢复按主键幂等只写一次 |
| 4 | 安全拒绝 | `净利润率是多少` | 执行前校验拦截编造指标，不查库、不写账本 |
| 5 | 知识检索（RAG） | `什么是GMV口径` | 字符 n-gram 向量召回口径文档 → 按出处引证；无依据则拒答，不自由生成 |

多轮追问（如 `淘宝平台GMV是多少` → `订单量呢`）复用上一轮平台过滤，验证 L3 语义记忆生效。

**四幕之外的新增能力**：RAG 知识检索作为独立的第五种意图，走 `search_knowledge` 步骤，**不经 LLM 规划**——证据来自文档库而非模型，因此答案可溯源、可复现。

---

## 2. 快速开始

```bash
# 1) 依赖（Python 3.11+）
python -m pip install -r requirements.txt

# 2) 造数据 + 建库（几秒）
python scripts/gen_mock_data.py
python scripts/build_db.py

# 3) 启动 API
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4) 另开终端启动界面
python -m streamlit run ui/streamlit_app.py
```

浏览器打开 Streamlit 给出的地址（默认 `http://localhost:8501`），左侧有五条演示问题可直接点选。

**配置真实模型（可选）**：复制 `.env.example` 为 `.env`，填入 `MINI_HARNESS_API_KEY`（默认 MaaS 网关可改 `MINI_HARNESS_BASE_URL`）。不配也能用本地 mock 跑通全部演示。

**MCP Server（挂到 Cursor / Claude Desktop）**：

```bash
python scripts/setup_mcp.py          # 首次：装隔离依赖到 .mcp_lib2
python mcp_server/test_client.py     # 自测：列工具 / 查数 / RAG / 退款闸门与幂等
```

挂接配置（`command` 用本机绝对路径）：

```json
{ "mcpServers": { "mini-harness": {
  "command": "python",
  "args": ["<项目绝对路径>/mcp_server/server.py"]
} } }
```

---

## 3. 它到底证明了什么

市面上多数 Agent 作品是"调一个聊天 API + 几个 function call"。这个项目刻意做难的部分——**控制流、口径、失败处理、可审计性**：

- **Agent Loop 是一等公民**：`OBSERVE→ROUTE→PLAN→PRECHECK→EXECUTE→VERIFY→TRACE` 固定循环，按步骤名推进，不靠 prompt 自觉。失败只重规划一次，第二次失败即拒答。**循环有两种可切换实现（自研 while / LangGraph StateGraph），共用同一套步骤语义，500 金标逐项一致——见 §6。**
- **执行前校验（Hard Controls）**：未知指标、非法字段、未批准写在进工具之前被拦，**不查库、不写账本**。这是幻觉防御的第一道闸。
- **口径唯一来源**：LLM 只输出 `SemanticPlan`（fact/dimension/filter/time），SQL 由声明式 `FactDef` 编译，字段白名单 + 参数绑定 + sqlglot AST 校验三层把关。模型不能自己写 SQL。
- **人在回路是一等公民**：退款提案落盘 → 挂起 → `state.json` 原子替换 → 进程重启后仍能批准 → 执行按 `proposal_id` 主键幂等。
- **权限在协议层之外**：MCP 只暴露 7 个只读/提案工具，**没有批准或执行工具**。写权限写在账本状态 + 执行出口的硬检查里，外部 Agent 拿到 MCP 访问权也最多提案，无法自批自消。
- **评测闭环**：500 条金标由**独立脚本**从原始表聚合算出（不经过被测编译器），端到端跑 Loop 后逐值比对；真实模型首轮 420/500 抓出三类口径缺陷，修复后才到 500/500。

---

## 4. 关键设计：原话高于模型输出

真实模型在 500 条金标上暴露的三类问题，根因都是**模型擅自改了用户的口径**。修复方式是统一的：Loop 在写入 `ctx.plan` 前，用用户原话做最后一道约束，重规划后再约束一次。

1. **时间**：`上个月 / 这个月 / 上上个月 / 2025年3月 / 2025-03` 覆盖模型漏填或填错的 `time_range`；原话没有"按月/每月"就去掉 `month`。所以"上个月GMV"是一条单月合计，不是 12 行分组。相对时间锚定数据集最大日期，不猜系统今天。
2. **指标**：原话点名"毛利润/净利润率"等未知指标时，即使模型映射成 `gmv` 也打回未知指标，执行前拒绝。
3. **维度**：原话"按品类/分城市/按平台/按用户等级/按月份"点名的维度不能被重规划悄悄丢掉；"按品类折扣率"两次被编译器拒绝后正常拒答，不会答成总折扣率。

**诊断证据同窗**：漏斗只查 `user_behavior`，按会话先浏览后加购（不再 `orders LEFT JOIN user_behavior` 的笛卡尔积）；浏览会话为 0 或加购数反超时 `cart_rate` 置空，绝不输出 >1 的转化率。折扣率与 GMV 用同一个闭开区间。

**权限标签与写入边界一致**：`propose_refund` 标 `proposal`（不是 `read_only`），只能写 `proposals`；`write_gated` 只留给 `execute_refund`。所有账本写入走 `Executor.ledger_write(tool, table, ...)`，越权直接 `PermissionError`。

---

## 5. RAG 知识检索模块

```
app/rag/
  embed.py       字符 n-gram 哈希 → 256 维稠密向量 + L2 归一化（确定性、离线、无随机）
  knowledge.json 16 条口径/规则/边界文档，每条带 id / 标题 / 正文 / 标签
  store.py       字段加权余弦检索（标题标签 0.65 + 正文 0.35）+ 专名命中重排
```

**为什么本地哈希向量而不是外部 embedding API**：作品要在无网络、无 Key 的现场开箱即跑；评测要求同一问题每次得分一致。它仍是真实的稠密向量余弦检索，不是关键词 if/else。接口预留：`embed()` / `search()` 换成正经 embedding 模型或向量库时，上层工具不动。

**防幻觉**：答案只拼接命中文档原文并标注出处 `[K01 ...]`，**不调用 LLM 自由生成**；命中低于阈值（0.28）直接拒答，不用常识脑补。这个阈值来自实测：域内查询得分 0.43~0.87，域外查询 ≤0.21，中间有明显间隔。

**接入方式**：`search_knowledge` 注册进统一 Executor（access=`read_only`），因此内部 Loop、外部 MCP、评测三者共用同一实现；路由层用问答式强信号（`什么是/口径/定义/规则/怎么算/有什么区别`）识别知识意图，这些词在 500 条金标里出现次数为 0，不影响查数/诊断/退款路径。

> 边界：这是"检索增强"的最小骨架，没有 HNSW、重排模型或 chunk 切分流水线。

---

## 5.1 双编排内核：LangGraph 只是"一层皮"

很多 Agent 作品把 LangGraph 当卖点，但说不清它到底做了什么。这里给出一个**可切换、可验证**的答案：

> LangGraph 只负责"循环怎么驱动"，不是 Harness 本身。把编排层换掉，业务语义一行不用改。

```
                    ┌──────────────────────────────┐
                    │  共享内核（两种编排都调用）    │
                    │  Context / Router / Orchestrator│
                    │  steps.run_step()             │
                    │    ├ 原话约束链（时间/指标/维度）│
                    │    ├ _precheck 硬控制           │
                    │    ├ Executor 注册表            │
                    │    └ Verifier 幻觉防御          │
                    └───────────────┬──────────────┘
                     ▲                             ▲
        ┌────────────┴────────┐       ┌────────────┴────────┐
        │ 内核A：自研 Loop     │       │ 内核B：LangGraph     │
        │ while + step_idx    │       │ StateGraph + 条件边  │
        │ 成功推进/失败重规划   │       │ 同样的推进/重规划    │
        └─────────────────────┘       └─────────────────────┘
                  └────── 500 金标逐项一致 ──────┘
```

**切换只要一个环境变量**，上层（FastAPI / Streamlit / MCP / 评测）零改动：

```bash
MINI_HARNESS_RUNTIME=loop        # 默认：自研 while 状态机
MINI_HARNESS_RUNTIME=langgraph   # LangGraph StateGraph 编排
```

**一致性不是口头承诺，是实测**：

| 内核 | 金标 | 总体 | SQL / 行为 | 九层分布 | 硬控制 |
|---|---|---|---|---|---|
| 自研 Loop | 500 | 500/500 | 280/280 · 220/220 | 全部 100% | 3/3 True |
| LangGraph | 500 | **500/500** | **280/280 · 220/220** | **逐层相同** | **3/3 True** |

`eval/baseline_langgraph.json` 是 LangGraph 内核的完整跑分报告；`tests/test_dual_runtime.py` 有 9 项断言，覆盖 6 类问题的**状态 + 逐步轨迹序列**一致、查数数值一致、退款挂起零写入一致。

**LangGraph 在这里的真实价值**（不吹不黑）：
- 编排路径显式成图，节点/条件边可在 LangSmith 可视化，循环与重试路径一眼看清；
- "成功推进 / 失败重规划一次 / 拒答终结"由条件边表达，而不是散落在 while 里的 `if`；
- 若接真实检查点后端，编排状态可托管给框架。

**刻意不用的部分**：没有接 LangGraph checkpointer——本项目的挂起恢复走 `Context.save_state()`（账本状态 + 多轮口径），两套检查点会分叉。框架只做编排，不接管业务状态。这也正是"它是编排层"的边界。

---

## 6. 架构

```
两条调用入口，权限边界不同：
  入口A(内部)  Streamlit → FastAPI(SSE /run, /approve) → Loop → 提案 + 人工批准后执行
  入口B(外部)  Cursor/Claude → MCP(stdio) → 7 个只读/知识/提案工具（无批准/执行工具）

  编排内核(可切换，见 §5.1)：factory.build_runtime()
      内核A 自研 Loop.run()    : while + step_idx
      内核B LangGraphLoop.run(): StateGraph + 条件边
      两者都调用同一份 steps.run_step()：
        llm_plan → [PRECHECK] → 工具步(注册表) → VERIFY → TRACE(JSONL)
        预校验拦截未知指标/非法字段 → 记失败轨迹，不查库不写账本
        失败只重规划一次(注入 hint)，第二次即拒答
        查询步 / 诊断步 / 知识步 / 提案步按步骤名分派
        退款步挂起 → state.json 落盘 → /approve 恢复续跑
```

**四层记忆**：L1 工作态（`ConversationState`）/ L2 事件（JSONL 可回放）/ L3 语义口径（`last_plan` + FactDef）/ L4 偏好。会话状态落 `traces/{session_id}.state.json`（临时文件 + 原子替换），进程重启可恢复。

**账本**：`ledger.db` 两张表——`proposals`（pending→approved/rejected，人工入口才能改）与 `refunds`（`proposal_id` 主键，执行幂等）。

**终止条件**：任务完成 / `MAX_STEPS=8` / Token 预算耗尽 / 连续 2 次失败拒答。

---

## 7. 目录

```
app/
  main.py                 FastAPI + SSE + /approve
  llm.py                  OpenAI兼容网关统一出口 + 成本埋点 + mock 降级
  rag/                    ★ RAG：确定性向量检索 + 引证回答（§5）
  runtime/
    models.py             统一契约（Pydantic v2，Task.type 含 knowledge）
    steps.py              ★ 共享步骤语义（两内核共用的唯一真相源）
    loop.py               内核A：自研 while 状态机 + 挂起恢复
    langgraph_loop.py     内核B：LangGraph StateGraph 编排（同接口）
    factory.py            内核切换（MINI_HARNESS_RUNTIME）
    executor.py           工具注册表 + 单一执行出口 + access 权限 + ledger_write 表白名单
    router.py             竞争打分 + 知识意图强信号
    orchestrator.py       Task DAG（知识步不经 LLM 规划）
    verifier.py           ★ 幻觉防御检查 + RAG 无依据不可重试
    budget.py             Token 预算 + 上下文裁剪
    memory.py             四层记忆
    trace.py              事件溯源 JSONL（可用 MINI_HARNESS_TRACE_DIR 重定向）
  semantic/
    facts.py              5 个 FactDef（口径唯一来源）
    compiler.py           SemanticPlan → SQL（字段白名单 + 参数绑定）
    guard.py              sqlglot AST 白名单
    normalize.py          别名归一 + 原话时间/指标/维度约束
  tools/builtin.py        查数 / 诊断 / RAG检索 / 提案 / 执行退款
mcp_server/
  server.py               ★ MCP Server：7 个只读/知识/提案工具，无批准/执行工具
  test_client.py          MCP 官方 client 自测
scripts/                  数据生成 + 建库 + MCP 依赖安装
eval/                     golden_set(500) + build_golden + run_eval + 多轮 baseline
                            baseline_bailian / baseline_maas / baseline_mock / baseline_langgraph
tests/test_harness.py     22 项加固断言，不访问网络
tests/test_dual_runtime.py 9 项双内核一致性断言，不访问网络
ui/streamlit_app.py       对话 + 轨迹 + 审批卡片
traces/                   每次请求的完整执行录像（.gitignore）
```

---

## 8. 评测：500 条金标（可核对、分层、非自证）

```bash
python eval/build_golden.py                    # 重建 500 条金标（独立真值，可复现）
python eval/run_eval.py                        # 跑 500 条，按 .env 自动选 mock/真实网关
# 真实模型单次 10~15s；并发只加速评测，不改被测 Runtime：
#   PowerShell:  $env:EVAL_CONCURRENCY="4"; python eval/run_eval.py
# 在线服务常驻占用 ledger.db 时，用独立数据副本（Linux，无需停服）：
#   cp -a data /tmp/mh-eval-data
#   MINI_HARNESS_DATA_DIR=/tmp/mh-eval-data EVAL_OUTPUT=eval/baseline_maas.json EVAL_CONCURRENCY=4 python eval/run_eval.py
# 切到 LangGraph 内核再跑一遍（应同样 500/500）：
#   PowerShell:  $env:MINI_HARNESS_RUNTIME="langgraph"; python eval/run_eval.py
python -m pytest tests/ -q                     # 36 项断言，不访问网络
# 从 UI 视角走完整链路（真实网关，复刻 SSE + 前端渲染，逐环节打印）：
python eval/ui_flow_check.py
#   （离线自测：先设 MINI_HARNESS_MOCK=1）
```

**金标不是"系统输出当答案"**：`build_golden.py` 用纯 Python 直接聚合原始表算 gold（不走被测的 compiler/FactDef），两条独立实现——compiler 口径错了会被抓到。评测把端到端结果按维度键对齐后逐值比对（金额容差 0.01，比率 1e-5）。

| 层 | 内容 | 条数 |
|---|---|---|
| L1–L5 | 查数：裸查 / 平台城市过滤 / 相对+绝对时间 / 单维度 / 时间×过滤×维度组合 | 280 |
| L6 | 陷阱：编造指标拒答、比值型带品类拒答、相对时间单月 vs 按月分组 | 75 |
| L7 | 诊断：同窗折扣率、加购率≤1、叙述与证据一致 | 36 |
| L8 | 退款 HITL：不同订单/金额/风险、口语"退N元"、挂起零写入 | 83 |
| L9 | 多轮：平台/城市过滤继承 | 26 |

**实测报告都在 `eval/` 下可复核**：

| 文件 | 模式 | 结果 |
|---|---|---|
| `baseline_maas.json` | 新 MaaS 中转 `qwen4-max`，`kimi-k2.6` 备用 · 自研 Loop · **部署服务器实测** | **500/500**，overall/sql/behavior 全 1.0，九层全 100%，三个硬控制全 True（`llm_mode=api`） |
| `baseline_bailian.json`（当前 `baseline.json`） | 旧百炼 `qwen4-max`，`qwen-turbo` 备用 · 自研 Loop | **500/500**，九层分布、硬控制与新网关**逐项一致**（UI 契约修复后重跑） |
| `baseline_mock.json` | 本地 mock · 自研 Loop | 500/500，回归基线 |
| `baseline_langgraph.json` | 本地 mock · LangGraph 内核 | **500/500**，九层分布与硬控制与自研内核逐项相同 |

> **网关切换说明**：当前 `.env` 默认指向 MaaS 中转网关（`https://maas-api.simple-server.cn/v1`），其 key 有 **IP 白名单**——开发环境出口 IP 未获授权，因此真实评测在部署服务器上完成：先跑 `python deploy/verify_gateway.py` 自检（key/白名单/模型名/JSON 输出），再用 `MINI_HARNESS_DATA_DIR=/tmp/mh-eval-data EVAL_OUTPUT=eval/baseline_maas.json EVAL_CONCURRENCY=4 python eval/run_eval.py` 复现成绩。评测指向 `data/` 的独立副本，**不停在线服务、不污染演示账本**；两个真实网关的九层分布与硬控制逐项一致，说明成绩不依赖特定网关。

**首轮真实模型跑分 420/500（84%）**，金标抓出三类真实缺陷，修复后才到 100%：
1. 绝对月"2025年3月"漏填 `time_range` → 52 条查成全年总量；
2. 重规划把"按品类"丢掉 → 24 条把"按品类折扣率"答成总折扣率；
3. "毛利润"被映射成 `gmv` → 1 条本该拒答却答出数字。

**判别力自测**（不靠"全绿"自证）：把 compiler 时间窗改成全年 → 准确率 1.0 掉到 0.742（129 条失败）；首轮真实模型 84% 又是一次独立证明——mock 全对不等于真实模型全对。

**UI 契约端到端验证**（`eval/ui_flow_check.py`，真实百炼）：金标只测 Runtime，不测前端。这个脚本从 UI 视角走真正的一条链——复刻 `httpx.stream POST /run` 收 SSE、再用 `streamlit_app.py` 里**真实的 `summarize` 函数**渲染，六个演示问题 + 退款审批逐环节打印"后端 status / result 结构 / 聊天区渲染 / 轨迹面板"。它抓出并钉死了金标覆盖不到的四类前后端裂缝：

| 裂缝 | 症状 | 修复 |
|---|---|---|
| `done.result.data` 是 ToolResult 包装 | 前端渲染成原始 JSON 块 | `_finish` 统一解包，run/resume 两条路径口径一致 |
| 诊断渲染字段名对不上 | 写了 `gmv_by_platform`，真实是 `cur_gmv/worst_platform` | 前端按真实字段重写，补查数/终态分支 |
| 失败 hint 跨请求遗留 | 问完"净利润率"后再问"淘宝GMV"被带偏拒答 | 每轮开始清空 hints（两内核同步） |
| Token 预算跨请求累计 | 连续演示 5 轮触发 `budget_exhausted` | 预算改为约束单轮循环，每轮重置 |

`tests/test_ui_contract.py`（5 项，双内核覆盖）把这四个不变量钉成断言，防止再漂移。真实百炼逐环节输出存于 `eval/ui_flow_report.txt`，六个演示的 status、渲染文本、轨迹全部可核对。

> 引用数字时请注明**样本量 500、真实网关实测（旧百炼 / 新 MaaS 均为 qwen4-max）、单机生成数据**，不编百分比。

---

## 9. 边界（诚实声明）

单机、演示用固定审批人（`/approve` 无身份认证）、无多租户、无登录、无双模型热备、不自动放行退款。RAG 是本地哈希向量的最小检索骨架，不是生产级向量库。相对时间锚定数据集最大日期而非系统时间。这是可演示的应用级内核，**不是可直接接真实资金的结算系统**。

---

## 10. 数据来源与法律声明

- 数据结构与阿里云天池「电商运营分析数据」（dataset 222889，CC0）等价；`data/raw/*.csv` 由 `scripts/gen_mock_data.py` **随机生成**（平台/状态/行为编码一致），不来自任何真实业务。
- `auth_user` 等无关表未导入；退款流水写入独立 `ledger.db`，与只读分析库物理隔离。
- 无竞业限制，仅有保密协议；本项目为换域独立实现，不含任何原业务的字段、口径、数据或 prompt。

---

## 11. 模型接入

OpenAI 兼容网关，默认指向 `https://maas-api.simple-server.cn/v1`（模型清单见控制台：auto 智能路由 / qwen4-max / qwen3.8-flash / deepseek-v4 / kimi-k2.6 / glm-5.3 等）。主模型 `qwen4-max`，429/5xx 时降级 `kimi-k2.6`。四项全部环境变量可配：`MINI_HARNESS_BASE_URL`、`MINI_HARNESS_API_KEY`（兼容旧名 `DASHSCOPE_API_KEY`）、`MINI_HARNESS_MODEL`、`MINI_HARNESS_MODEL_FALLBACK`——换服务商只改 `.env`，不改代码。未配置 Key 时自动用本地确定性 mock，全部演示仍可跑通。所有 LLM 调用只走 `app/llm.py` 一个出口 → usage 自动埋点 → Token 预算与成本可追溯到每一步。

## 12. 部署（双进程 + 口令闸门）

面向"给面试官一个能点开的在线链接"。设计要点：**FastAPI 只绑 `127.0.0.1:8000`，不暴露公网**；Streamlit 绑 `0.0.0.0:8501` 对外，访问口令做在 UI 层。陌生人过不了口令，就绕不过 UI 直接刷你的网关 token 额度。

```
公网 ──8501──> Streamlit(口令闸门) ──本机127.0.0.1:8000──> FastAPI ──> LLM / DuckDB
```

一键脚本（Ubuntu 22.04/24.04，需 root 或 sudo）：

```bash
export MINI_HARNESS_API_KEY="网关key（swas.xxx）"   # 不填则 mock 模式，演示仍可跑
export DEMO_PASSWORD="给面试官的口令"
bash deploy/setup_server.sh
```

脚本做 8 件事：装依赖 → 建服务用户 → clone 仓库 → venv 装 requirements → 固定种子重建 db → 写 `.env`（key 与口令，权限 600）→ 装并启动两个 systemd 服务 → UFW 放行 8501。

**阿里云轻量服务器额外一步**：在控制台【防火墙/安全组】放行 TCP 8501；**不要放行 8000**（API 只供本机 UI 调用）。访问 `http://<公网IP>:8501`，输入口令即可。

运维：`systemctl status miniharness-api|ui`、`journalctl -u miniharness-api -f`、`tail -f /var/log/miniharness/*.log`。数据重建用 `bash deploy/setup_server.sh --force-db`。口令改后 `systemctl restart miniharness-ui`。

> 诚实边界：这是演示级部署（单 worker、无 HTTPS、口令是共享字符串非账号体系）。未接域名/证书、未做多租户隔离——面试时如实说明即可，不必包装成生产部署。
