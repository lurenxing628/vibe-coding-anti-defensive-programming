---
name: limcode-dual-model-pipeline
description: VS Code Limcode Test 专属的双模型反转流水线（Gemini 4 Argon explore 正交分片纯读深挖与 worker 不重叠文件分片并发编码、返修 ↔ Codex GPT-6 Astra reviewer 多轮只读反方严审 ↔ 主代理亲核源码终局裁决与门禁验证）。融合《认知一致性》、《stop-that-shit》与 140 项反过度防御黑名单，用于新功能开发、复杂故障根治、架构重构与代码审查。
---

# Limcode Test 双模型反转流水线（Argon 编码 ↔ Codex 严审）

本技能仅面向 **VS Code Limcode Test**，所有调度直接使用原生可靠内核 **`ReliableKernelApplication`**：
- **Gemini 4 Argon**：`explore` 正交分片纯读深挖；`worker` 按严格不重叠文件分片并发编码与返修。
- **Codex GPT-6 Astra**：`reviewer` 只当反方，开放式追查契约破损、漏改调用点与回归，多轮只读严审，不写业务或测试代码。
- **主代理**：亲读源码、核验线索、裁决意见、划定分片、统管门禁；不把审查建议直接当成修改命令。

角色是职责与工具策略，不是模型选择参数。实际模型必须按下述设置路由确认；不要仅凭角色名宣称已经完成异模型审查。

## 一、核心原则（必须先读）

1. **《认知一致性》**：[../认知一致性/SKILL.md](../认知一致性/SKILL.md)。从共同语义出发，在源头统一数据、行为与职责，让同一种事情只有一条路。问题源于职责分散、重复链路或下游补偿时，直接重构共同机制、迁移全部实际调用者并清理旧实现与旁路；只在旧结构外包一层不算完成。评价长期理解和修改成本，不以 diff 大小代替设计判断。
2. **《Stop That Shit》**：[../stop-that-shit/SKILL.md](../stop-that-shit/SKILL.md)。完整完成已授权工作，限制测试膨胀、无用哈希、过度防御与任务越界，鼓励有意义的重构。真实边界保护应保留，重复校验、静默兜底和只为测试续命的代码应从源头清理。
3. **《4 条第一性原理拷问 + 15 大母题 / 140 项反过度防御黑名单》**：[references/blacklist.md](references/blacklist.md)，案例库为 [references/blacklist-cases.md](references/blacklist-cases.md)。存在性、单一事实源、信任边界、语义保真四条拷问贯穿全程，A～O 母题不是穷尽清单。

路径全部相对于加载本技能时返回的 **`Base directory for this skill`**。派发任何研究、编码或审查子代理时，必须在提示词中给出主表与案例库的**绝对路径**，要求其先用 `read` **完整阅读 `blacklist.md`**（包括四条拷问与 A～O 识别信号；返回截断则继续读取），需要具体案例时按编号读取 `blacklist-cases.md`。不能用主代理已读代替子代理阅读。

共同原则可通过 `skills` 工具加载，或通过 `run_agent(..., skills=["认知一致性", "stop-that-shit", "limcode-dual-model-pipeline"])` 由控制面在子代理首轮预加载。预加载技能正文不等于读取其 references，主表仍须显式 `read`。不另造报告协议、包装脚本或重复事实源。

## 二、Limcode Test 模型与子代理路由规则（Model Routing & Kernel Primitives）

### 模型配置

`backend/reliableKernel/vscodeConfigurationAuthority.ts` 的 `resolveModelSelection` 按以下优先级解析：

**agent 级 ModelProfile（`model-profile:agent:<agentId>`） > 父会话继承模型（`modelFallback`） > 全局默认渠道**。

建议在 Limcode 设置页绑定：
- `explore` → `gemini-4-argon`；也可不绑定而继承 Argon 主会话。
- `worker` → `gemini-4-argon`。
- `reviewer` → `gpt-6-astra-minor`（Codex GPT-6 Astra）。

`run_agent` 的 `agent.type` 选择角色配置，不接收臆造的 model 参数。使用工具当前实际公开的 schema；能力或深度限制阻止派发时，明确报告阻塞，不伪造子代理结果或改用其他宿主控制面。

### 1. 第一性原理纯读深挖（Research Subagents）

```text
run_agent(operation="spawn", agent={ type: "explore" }, forkTurns="none",
          skills=["认知一致性", "stop-that-shit"],
          taskName="正交研究分片", prompt="完整目标、只读范围、黑名单绝对路径、证据与输出要求")
```

`backend/world/modules/agent/blueprints.ts` 将 `explore` 工具策略强制收窄为 `READONLY_TOOLS`，物理剔除 `edit` / `write` / `delete` / `transfer`。提示词同时禁止借 `shell` / `bash` 修改工作区或测试文件；报告在最终回复返回，不为只读研究落盘修改项目。

单轮父会话最多并发准入 **8 个 spawn**。常规按 **3～4 个正交分片**并发；复杂深水区可分为 **4×4 共 16 分片**，分批或完成一片补一片流式推进。派发前查看现有 roster，避免重复责任。若因限流或中断停下且 `status="interrupted"`，用 `run_agent(operation="send", childRef=..., prompt="继续完成剩余分析……")` 原地续跑，绝不丢弃重开或由主代理代写研究报告。

### 2. 不重叠文件分片并发编码与返修（Coding & Fix Subagents）

```text
run_agent(operation="spawn", agent={ type: "worker" }, forkTurns="none",
          skills=["认知一致性", "stop-that-shit"],
          taskName="编码分片", prompt="裁决单、互斥文件白名单、黑名单绝对路径、验证与交付要求")
```

`worker` 具备读写与执行权限。每个 worker 必须拿到**完全不重叠的文件白名单（Disjoint File Slices）**，严禁跨分片改文件，严禁执行 `git commit` / `git checkout` / `git reset`；未获授权也不得 `git push`。遇到跨片共享机制变更，先报告主代理重划所有权，不能边写边抢同一文件。

只有针对**上一轮同一批已确定问题未修全的追补/微调**，才用 `run_agent(operation="send", childRef=..., interrupt=false, prompt=...)` 复用该 worker。新一轮审查发现的新问题，或切换到不同文件范围，必须重新 `spawn` 干净 worker，`forkTurns="none"`，重新给完整裁决单与边界。

### 3. 多轮只读反方严审（Codex Review Subagents）

每轮审查都启动**全新**会话，保持上下文干净、避免锚定偏见：

```text
run_agent(operation="spawn", agent={ type: "reviewer" }, forkTurns="none",
          skills=["认知一致性", "stop-that-shit"],
          taskName="本轮只读反方严审", prompt="双重指令、黑名单绝对路径、只读取证与签字要求")
```

`blueprints.ts` 将 `reviewer` 强制设为 `READONLY_TOOLS`，移除写文件工具。这不代表 shell 天然无副作用：提示词必须明令 **严禁通过 `shell` / `bash` 修改任何工作区或测试文件**，只做只读代码审查与命令取证，不能借修测试、加兜底或刷新基线自行修复。需要写产物的验证交主代理或授权 worker 执行。

### 4. 运行中流式补片与异步协作（Steer & Wait）

若需向运行中会话补充已经完成的独立研究片或上下文，可用 `list_agents` 找到目标 `C#`，再调 `send_agent_message(conversationRef="C#", text="……")`；目标在下一个安全输入边界自动消费邮箱消息。也可用 `run_agent(operation="send", childRef=..., interrupt=false, prompt="……")` 排队跟进。`C#` 是协作会话引用，`childRef` 是子任务控制引用，不相互猜测转换；消息工具未公开时直接使用 `run_agent` 已有能力。

子代理运行期间主代理并行推进独立工作。仅剩等待时直接结束当前轮次，完成答案会自动回传唤醒主代理；需要观察可按工具约定使用有界 wait。严禁同一轮内循环调用 `run_agent` / `read_agent_answer` 高频轮询催促，也不因运行缓慢中断重开。流式补充上下文不解除“当前送审块必须停止写入”的约束。

## 三、标准四阶段工作流（4-Stage Flipped Pipeline）

### Stage 1：正交分片第一性原理纯读深挖 + 交叉核验

1. 从用户目标、真实入口与调用链划分正交研究范围，常规 3～4 路；大范围调查才展开 4×4 分片。每片输出真实源码位置、调用关系、具体合法输入或失败场景、影响与源头方案，明确事实和待证假设。
2. 主代理亲自 `read` 报告涉及源码，必要时派只读 reviewer 对照源码和已有测试契约交叉核验；剔除假阳性、低收益折腾与靠内部非法输入推导的“漏洞”，收敛实施清单。
3. **直通路径**：常规中小开发/修复若需求与模块已经非常明确，可跳过 Stage 1 直通 Stage 2。只读审查请求保持只读，发现问题先报告，不擅自进入编码。

### Stage 2：主代理亲核源码 + 备份快照 + 4 路 worker 不重叠分片并发编码

1. 修改前查看 `git status --short` 与 `git diff`，将现有状态与 diff 保存到系统临时目录作为备份快照，不覆盖用户改动、不生成哈希清单。未跟踪且将受影响的现有文件需要另存原件，diff 不包含其内容；显式文件范围限制优先。
2. 主代理亲用 `read` 核实真实函数签名、字段名、调用链与测试断言；必要时用 `python -m tools.symbol_locator` 查询全仓定义、调用者和被调用者，不依靠报告猜接口。
3. 制定**分片裁决单**：列出目标、前后语义、文件白名单、全部实际调用者迁移、旧实现清理、验证与已裁决不改的 **by-design 边界**。共享机制由一个分片负责，其他分片按明确接口接入；依赖未稳定时先串行收敛，不能强凑并发。
4. 通常按 4 路 worker 派发；文件少则减少分片。提示词必须附黑名单绝对路径与严防“马大哈”自检清单：核对签名和字段；迁完调用点；删未用导入、旧出口与旁路；查内嵌回调/闭包残留；同步测试观察点和说明；确认未把旧兜底搬到下层、未添加伪可选或双轨开关。
5. worker 完成对应范围必要门禁并回报改动文件、语义变化、验证结果与阻塞。复用已有测试，只补真实覆盖缺口；纯文档改动用阅读和 diff 验证，不造字面量测试。

### Stage 3：reviewer（Codex）多轮只读严审 ↔ 主代理终局裁决与 worker 返修循环

#### 1. 整块对齐送审 + 跨块只读深挖并发

当前块所有 worker 完成且门禁通过、**工作区静止**后，spawn 全新 reviewer 整块审查，不边改边审。块 K 处于 Stage 3 时，块 K+1 只有文件与调用链均不相交才可并发启动 Stage 1 explore 只读深挖；不得用补片消息掩盖被审代码仍在变化。

#### 2. Dual Prompt 双重指令（每轮缺一不可）

- **明确靶心与已裁决边界**：列出具体问题、改动文件、预期语义、验证结果、已裁决不改项及依据。
- **强制开放式全链路猎杀**：明确要求：“严禁把审查视野局限于已知问题清单。除逐行核查改动与 A～O 母题，还须顺着上下游调用链、跨入口和测试契约主动寻找未提及的同类漏洞、口径分裂、漏迁调用点与回归。每条结论必须给源码位置、真实可达输入、行为后果及证据；不要靠臆造非法内部状态要求加兜底。”

#### 3. 主代理铁腕终局裁决

- **【主代理 0 动刀铁律】**：首轮编码、初筛修错及每轮返修，主代理严禁自己调用 `edit` / `write` 直接修改业务代码或测试代码，也不得借 shell 绕过；100% 派发或按复用规则唤醒 worker 执行。主代理负责读源码、裁决和运行门禁。
- **【着眼整个项目未来的《认知一致性》裁决标准】**：对每条审查意见先 `read` 亲核证据，不盲从 reviewer，也不为小 diff 保住坏结构。真实 Bug、契约分裂、漏迁调用者、双轨旁路与死代码，若源头统一的长期收益大于成本，果断**接受**，重构共同机制并迁完调用者。保守防御习惯、0 调用死入口、为残缺测试桩加反射/默认值、无实际收益的吹毛求疵，凭源码和实测证据果断**驳回**。合法数据语义、真实外部边界保护与既有强契约不能当作垃圾顺手删掉。
- **【强制向用户汇报逐条裁决清单】**：每轮展示表格：**条目 | 接受/驳回 | 源码位置 | 基于认知一致性与项目未来价值的理由**。驳回项带依据写入下一轮 by-design 边界；接受项形成明确返修裁决单，不把模糊建议直接塞给 worker。

#### 4. 循环直至签字通过

按路由规则派 worker 返修。每轮修完并通过对应门禁后，主代理用 `git status --short` / `git diff` 复核实际改动；随后 spawn **全新 reviewer**，`forkTurns="none"`，再次附双重指令、黑名单与已裁决边界，直到全新 reviewer 明确输出 **0 问题通过（Sign-off）**。不能用旧会话的“看起来好了”代替签字，也不为制造新问题无限追加无收益检查。

### Stage 4：全量门禁验证与交付

签字后执行目标项目适用的全量门禁（命令使用项目现有环境与真实源码目录）：

1. `ruff check <src_dirs> tests/`
2. `python -m pyright`
3. `python -m tools.scan_over_defense --fail-on-new`
4. `python -m tools.scan_dead_code_islands`
5. 涉及前端时执行前端构建与全套契约测试。
6. 全量 `python -m pytest -q`。

这些是具备相应工具链的业务项目门禁；纯文档/技能改动不虚构 Python 或前端运行要求，按实际影响阅读、检查 diff 与验证接入。已通过且相关输入未变的结果直接复用；只有相关改动、新失败、真实覆盖缺口或明确验收要求才重跑。失败先定位，必要修复仍交 worker；有实际代码变更则重新履行相关验证与新 reviewer 签字。

交付说明修改清单、逐条裁决、实际验证证据与尚未解决的阻塞。不擅自提交、推送、扩大范围，不新增证明文件、哈希或防御性包装脚本。请求完整实现、必要验证通过即交付。
