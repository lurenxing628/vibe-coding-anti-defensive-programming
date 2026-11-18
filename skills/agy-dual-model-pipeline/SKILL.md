---
name: agy-dual-model-pipeline
description: 反重力专属的架构设计、开发实现、源头修复、充分重构与代码审查流水线（主代理亲核源码终局裁决与门禁验证 ↔ 反重力原生 Swarm 分阶段清场驱动 Gemini 4 Carbon Medium 广撒网、实证核验、首稿编码与对账返修 ↔ Codex GPT-6.2 Sol Ultra 当反方与多轮只读严审）。融合《认知一致性》与反过度防御原则，用于新功能开发、复杂故障修复、架构重构与代码审查。
---

# 反重力双模型集群流水线（主代理铁面裁判 + 原生 Swarm Carbon Medium 分阶段工人 + Codex Ultra 反方与严审）

本技能把 **主代理（用户选定）**、**Gemini 4 Carbon 集群工人** 与 **Codex GPT-6.2 Sol Ultra** 按各自长处严格分工，用于完成**架构设计、功能开发、故障源头修复、充分重构与代码审查**：
- **主代理（当前主会话模型，由用户在界面选定）**：全程铁面裁判（**0 动刀改代码，且严禁自行切换主会话模型**）。每阶段结束先清场销毁当前工人，再亲自读源码 / 审 `git diff` 逐条裁决、定稿方案、跑门禁验证并派发下一阶段；
- **原生 Swarm 分阶段工人（默认全员 `gemini-4-carbon-medium`，核心硬骨头按需单点升 `gemini-4-carbon-high`）**：通过反重力底层 `swarm` 工具按阶段起停，负责广撒网找线索、跑工具实证核验与深挖、按裁决单写首稿、全调用链对账与源头返修；
- **Codex GPT-6.2 Sol Ultra**（[scripts/codex_drive.py](scripts/codex_drive.py) 驱动本地 `app-server daemon`，锁定 `--mode review`）：唯一跨家族异构反方与多轮只读严审。

> [!IMPORTANT]
> **为什么这样分工？**
> - **裁判与跑腿分离（推理预算全砸在刀刃上）**：传统的 `invoke_subagent` 只能 `inherit` 主会话模型与档位；改用原生 `swarm` 工具显式指定 `model="gemini-4-carbon-medium"` 后，无论用户在主会话选了什么高阶模型坐镇裁判，广撒网、工具取证、改代码与跑多轮测试这类高频工具调用体力活全由快速省配额的 `Carbon Medium` 承担，主代理的推理预算 100% 留给识破假阳性、穿透症状找源头架构病根、写死精确裁决单与甄别审查意见真伪。
> - **只借原生 `swarm` 的“造人底座”，绝不加载 `/swarm` 的“大群聊制度”**：反重力内置 `/swarm` 斜杠命令默认鼓励多角色同群自由 `@` 传球、主代理当甩手掌柜；本流水线**不加载内置 `/swarm` 技能**，只直接调用底层 `swarm` 工具实行**「星型单线汇报 + 分阶段 `kill` 清场制」**——任何时刻集群中只存在当前这一个阶段的工人，收齐报告立即整组 `kill` 清空，绝不允许工人绕开主代理私相授受。
> - **GPT（Codex）不当找问题和写代码的人，专当跨家族异构反方与严审**：本仓库大多是 GPT 写的，且探索与实现层已统一为 Carbon 同家族模型。GPT 写代码心机重，删一处明面兜底容易偷偷在下层塞入“搬家式吞错、伪可选参数、自编假测试锁死死分支”（见 [references/blacklist.md](references/blacklist.md) 母题 L「删旧壳时搬家兜底」与 D「靠测试续命」）；但它做只读 Review 极其细致毒辣，擅长抓漏改调用点、跨入口契约分裂、边界漏洞与类型不一致。保留跨家族的 `Codex Ultra` 只回答“这样改会弄坏什么”，既破除 Carbon 同家族互评盲区，又由主代理严格过滤其保守防御建议，防止删过头或加废兜底。

实时监控画面：首轮 `swarm(action="add")` 后点开主代理回复中的 **Swarm 看板胶囊链接**，在反重力右侧边栏实时查看 Carbon 工人状态与发言；在反重力终端执行 `codex watch`，实时观看 Codex 的思考、工具调用与内部 subAgent 过程。

---

## 一、核心原则：立、桥、破、治

本流水线同时约束**怎么设计好结构（立）**、**怎么让下游无需设防（桥）**、**禁止写出什么垃圾代码（破）**与**怎么管住尺子和收尾（治）**：

### 1. 立：认知一致性（源头统一、同一件事只有一条路）
完整原则见 [../认知一致性/SKILL.md](../认知一致性/SKILL.md)（或 `~/.codex/skills/认知一致性/SKILL.md`），贯穿全流程的核心精神如下：
- **首要目标是降低长期理解与修改成本，而非追求单次 diff 行数最小**：禁用“最小补丁 / 最小改法”思维。不要为了少改几行代码去打局部特判、绕开现有结构或在下游做观察/同步/补偿。
- **让同一种事情遵循同一套规则**：把共同语义放在一处实现，复杂度花在**源头把路合成一条**。新增能力或修复问题时，先核对它属于哪个概念、能否进入已有共同路径，绝不引入第二个事实源或第二条并行链路。
- **把现有实现和设计当作可变项，用充分重构换后续简单**：若当前问题源于职责分散、状态重复或数据路径别扭，直接重构共同机制。先把共同机制做完整，再迁移全部实际调用者，同步删除被替代的旧实现和旁路——**只在旧结构外再包一层不算完成**。
- **靠推广规则而非追加特判消解问题**：发现漏洞时，先看能否推广已有规则或删除造成分歧的条件，而不是逐条叠加特例。
- **保护机制与真实职责相称**：在真正承担责任的边界完成检查；解决真实问题所需的机制弄清职责后完整保留，删去各层重复防御、无用兜底和阻碍正常工作的机制。

### 2. 桥：保证交付定律与重构四步收缩律
- **保证交付定律（准入即定型）**：下游满屏 `.get()`、判空与深拷贝，根源往往是上游“检查完没交出字据”。治过度防御必先治上游：① **边界准入即定型**（返回不可变定型结构而非 `bool` 或裸 `dict`，让原料与成品在函数签名上区分开）；② **持久事实契约化**（不变量落成表结构 `CHECK` 约束与唯一存储读取原语，不只留在写入口）；③ **写原语直交产物与明确所有权**（写操作当场返回新主键/时间戳/版本号消灭写后回读；只读作用域共享引用、改写点唯一复制、长生命周期共享状态在公开出口隔离一次）。
- **重构四步收缩律（少一步不算完工）**：① **立共同机制** → ② **迁全链调用方**（Python `symbol_locator` + 前端/测试/脚本跨语言 `rg`）→ ③ **归零清创**（被替代符号深层生产引用归零、只锁旧壳或绕开正常构造路径的测试连根删或迁同形构造器、拆模块私有名正名、旧原语附带的排序/去重/空集守卫剥离、严禁函数内延迟 `import` 躲环）→ ④ **单向收紧棘轮基线**。

### 3. 破：4 条第一性原理拷问 + 母题主表
坏习惯分两层维护（不塞进本文件，避免跨项目噪音与双份维护）：
- **主表** [references/blacklist.md](references/blacklist.md)：4 条第一性原理拷问的全文，以及按拷问分组的 15 个母题（通用代码形态识别信号、正确做法、代表例子与入库门槛）。阶段 1 / 2 / 3 / 5 派发时必须附上它的绝对路径，要求先读再动手。
- **案例库** [references/blacklist-cases.md](references/blacklist-cases.md)：已抓获的具体战例档案（以每条 `> 母题：` 标签为唯一事实源，历史编号永久不变；老套路换马甲不重复立号）。只给路径、按需查，不整份传给工人。

对每一处设计与每一行代码改动，用 4 条拷问主动过筛（全文见主表）：
1. **存在性**：删掉会怎样？真实调用链与数据契约下行为不变，就是废代码。
2. **单一事实源**：上游是不是已经算过、查过、证过了？拿到了就显式传下来复用。
3. **信任边界**：外部不可信边界只校验一次且校验到位；内部受控结构不写兜底、反射探测、防御拷贝和事后自检。
4. **语义保真**：比较、合并、过滤、去重、归一化、降级之后，合法数据的意思变了吗？前三条抓“多做了”，这一条抓“做错了”；删防御时用它确认删掉的不是真实业务保护。

审查或复核中抓到新套路，当场剥离，并按主表“四、入库规则”先归类再入库：能归进已有母题的只追加到案例库，归不进才新开母题。

### 4. 治：防门禁异化与终轮逐条结案制
- **先看抽样真垃圾率，禁拿原始信号数定 KPI**：扫描出的原始信号计数不等于可删垃圾数，动手前先分层抽样核清生产可达性与真实保护比例；门禁棘轮须覆盖逃逸通道（同时监管函数内延迟导入环、仓储层下沉代码与框架隐式调用协议）。
- **终轮逐条结案铁律**：严禁因“最后一轮已无大问题、不再空转”将剩余小问题整包放行或打进含糊待办——① 每一条发现（含多子项发现的每个子项）都必须逐条判**“修 / 推迟（必写明确重启条件）/ 驳回（必写源码事实依据）”**；② 以“被测试锁定”放行必须先证明锁住的行为在真实生产调用链可达；③ “非本轮引入”只记来源，绝不作放行理由；④ 推迟记账文字只写客观问题与事实，严禁预写“可选”“保留”等实现方案暗示。

---

## 二、角色、模型与阶段门禁总表

| 阶段与角色 | 调度实现 | 模型与强度 |
| :--- | :--- | :--- |
| **主代理（全程铁面裁判）** | 当前主会话（0 动刀改代码） | **主会话模型（用户在界面选定）** |
| 阶段 1 广撒网找问题 / 推导方案 | `swarm(action="add", name="s1-bN", model="gemini-4-carbon-medium", tasks=[...])`，3～4 正交分片并发 | `gemini-4-carbon-medium` |
| **阶段 1 后门禁：清场 + 亲核初裁** | **主代理 `swarm(action="kill", name="s1-bN")` 清空集群 → 亲自 `read_file` 初核源码、剔除假阳性、切分正交批次并输出阶段 1 初裁表** | **主会话模型** |
| 阶段 2 核验线索 + 深挖核心链路 | `swarm(action="add", name="s2-bN", model="gemini-4-carbon-medium", ...)`，只读工具实证 | `gemini-4-carbon-medium`（核心调度/领域算法、复杂跨层事务/并发深挖可单点升 `gemini-4-carbon-high`） |
| **阶段 2 后门禁：清场 + 亲核裁决** | **主代理 `swarm(action="kill", name="s2-bN")` 清空集群 → 亲自 `read_file` 复核证据与深挖新问题，输出阶段 2 逐条裁决表** | **主会话模型** |
| 阶段 3 当反方 | `codex_drive.py --lane counter`，`--mode review` 只读，**每次必先 `new` 开无上下文新会话** | `gpt-6.2-sol` + `ultra`（内部自行派发 subAgent） |
| **阶段 4 终局裁决（阶段 3 后门禁）** | **主代理亲自 `read_file` + `symbol_locator` 核对反方意见、辨别真问题与过度防御、定稿 `<work>/adjudicated_plan.md` 并输出阶段 4 裁决表** | **主会话模型** |
| 阶段 5.1 首稿编码 | `swarm(action="add", name="draft-bN", model="gemini-4-carbon-medium", ...)`，按严格互不重叠的文件分片派发 | `gemini-4-carbon-medium` |
| **阶段 5.2 首稿后门禁：清场 + 亲审 diff** | **主代理 `swarm(action="kill", name="draft-bN")` 清空集群 → 亲审 `git diff`（严查源头完成度 + 15 母题过度防御）并跑初筛门禁裁决** | **主会话模型** |
| 阶段 5.3 多轮严审（含反过度防御专项） | `codex_drive.py --lane review`，`--mode review` 只读，**每轮必先 `new` 开无上下文新会话** | `gpt-6.2-sol` + `ultra` |
| **阶段 5.4a 严审后门禁：亲核裁决** | **主代理亲自 `read_file` 核实每条严审意见，输出逐条裁决表（接受/驳回）与源头返修指令** | **主会话模型** |
| 阶段 5.4b 工具对账与源头返修 | `swarm(action="add", name="fix-bN-rM", model="gemini-4-carbon-medium", ...)`，每次必用新名字开全新会话 | `gemini-4-carbon-medium`（同一模块连续两轮未断根或跨模块复杂状态重构时升 `gemini-4-carbon-high`） |
| **阶段 5.4c 返修后门禁：清场 + 亲审 diff** | **主代理 `swarm(action="kill", name="fix-bN-rM")` 清空集群 → 亲审返修 `git diff` 与初筛门禁，再送 5.5 下一轮 Codex 复审直至清零** | **主会话模型** |
| 实时监控画面 | 右侧边栏打开原生 **Swarm 看板**（看 Carbon 工人）；终端执行 `codex watch`（[scripts/codex_view.py](scripts/codex_view.py)，看 Codex） | — |

> [!IMPORTANT]
> - **【每阶段清场 + 主代理亲核源码裁决铁律（Stage-Gate Referee Rule）】**：**绝不允许把上一阶段工人或外部模型的输出直接盲传给下一阶段，更严禁把不同阶段的工人同时放进集群互相传话！每一个阶段（阶段 1 广撒网、阶段 2 核验深挖、阶段 3 Codex 反方、阶段 5.1 首稿、阶段 5.3 Codex 严审、阶段 5.4b 返修）完成后，主代理必须先 `swarm(action="kill")` 清空当前工人（Codex 阶段除外），再亲自调用 `read_file` / `git diff` 核对目标源码，独立做出本阶段逐条裁决并向用户汇报，裁决通过后才允许亲手创建下一阶段工人或启动 Codex。**
> - **【全链路零上下文污染铁律（Swarm 阅后即焚 + 禁翻旧板 + Codex 每次必 `new`）】**：
>   1. **原生 Swarm 工人**：每一阶段、每一批次、每一轮返修都必须用**全新的递增名字**（如 `s1-b1`、`s2-b1`、`draft-b1`、`fix-b1-r1`、`fix-b1-r2`，`swarm` 中被 `kill` 的名字永久不可重用）调用 `swarm(action="add")` 新建干净无上下文会话；新工人的看板游标默认停在自身启动 `brief` 之后，**提示词必须明令禁止工人调用 `read_board{since:0}` 翻看历史旧帖、禁止工人之间互相 `@`、禁止创建常驻 `monitor` 或 `narrator`**，省配额且彻底隔绝旧轮次污染。
>   2. **Codex 会话（`counter` / `review` 车道）**：**每一次派发新任务（无论是阶段 3 反方还是阶段 5 每一轮代码严审）前，都必须先执行 `codex_drive.py new --lane <counter|review> --cwd <repo>` 开启全新、无上下文的独立会话**，严禁复用带有历史上下文的旧会话。
> - **【常规中小开发/修复任务的直通路径】**：需求与涉及模块已非常明确、无需大范围找问题时，跳过阶段 1～3，直接从**阶段 4（主代理亲核源码制定方案并裁决）→ 阶段 5（5.1 首稿 → 5.2 主代理初筛裁决 → 5.3 Codex 严审 → 5.4a 主代理严审裁决 → 5.4b 对账返修 → 5.4c 返修裁决 … → 5.6 终局门禁）**启动。
> - **【严禁轮询与自动拉起铁律（Reactive Wakeup）】**：
>   1. **首轮建群直接收尾休眠**：第一次调用 `swarm(action="add")` 的那一轮，`post_message` / `read_board` / `swarm_wait` 尚未挂载（下一轮自动出现）。主代理在回复中附上 `add` 返回的看板胶囊链接并简要说明分工后，**立即结束本轮（严禁去找看板工具或轮询状态）**；工人发帖点名 `@coordinator` 时宿主自动拉起主代理。
>   2. **后续轮次零消耗等待**：当并行分片还有工人处于 `working` 未交齐，或主代理在 `post_message` 答复工人提问后继续等结果时，调用 `swarm_wait{since: <已读最大序号>, mentions_only: false}`（用 `mentions_only: false` 双保险防工人粗心漏写 `@coordinator`）挂起，或直接回复用户结束本轮；启动 `codex_drive.py` 则用 `run_command(IsDaemon=false, WaitMsBeforeAsync=500)` 并输出简短说明后立即结束本轮。**严禁在循环中轮询 `swarm(action="status")`、`read_board`、`manage_task` 或 `schedule`！**

---

## 三、宿主机理与调用命令

### 1. 原生 Swarm 集群调度机理（Carbon 工人池）
1. **不要激活内置 `/swarm` 技能**：直接调用主代理自带的 `swarm`、`read_board`、`post_message`、`swarm_wait` 四个底层工具即可，不加载内置 `native_swarm` 的自由群聊规则，也不添加 `monitor` 与 `narrator`。
2. **显式锁死工人模型档位（禁改主代理自身）**：`swarm(action="add")` 若省略 `model` 会自动继承主会话模型；因此无论主会话当前由用户选了什么模型，派发常规工人时**必须显式传 `model="gemini-4-carbon-medium"`**（仅在阶段 2 高危核心链路深挖或阶段 5.4b 连续两轮未断根硬骨头时传 `model="gemini-4-carbon-high"`），且**严禁调用 `swarm(action="model")` 擅自切换主代理自身的模型**。
3. **单工人与并发分片组**：
   - 单工人：`swarm(action="add", name="s2-b1", model="gemini-4-carbon-medium", brief="...")`；完事 `swarm(action="kill", name="s2-b1")`。
   - 并发分片组（阶段 1 广撒网或阶段 5.1 多分片首稿）：`swarm(action="add", name="s1-b1", model="gemini-4-carbon-medium", brief="公共规则与必读路径...", tasks=["分片1职责与文件范围...", "分片2...", "分片3..."])`，一次调用并发拉起 `s1-b1-1`..`s1-b1-3`；收齐全部报告后调一次 `swarm(action="kill", name="s1-b1")` 整组销毁。
4. **工作区模式（`workspace`）**：
   - **默认传 `"inherit"`（或不传）**：所有编码/返修分片已在阶段 4 切为**严格互不重叠的文件集合**且留有修改前快照，直接在主工作区修改并跑针对性单测，主代理在门禁处可直接执行 `git status --short` 与 `git diff` 亲审，零合并损耗。
   - **按需传 `"share"`**：仅当多个并发分片同时运行测试会争抢同一临时资源或受彼此半成品干扰时使用；启用 `"share"` 时，工人必须在各自工作树 commit，且**主代理必须在调用 `kill` 前将其提交合入主工作区**（`kill` 会永久删除 `"share"` 工作树）。
5. **中途提问与互动对齐（平替原退出码 77）**：
   - 工人在核对方案或修改过程中发现漏算调用点、兄弟入口冲突或需触碰授权外文件时，在看板发帖 `@coordinator [对账疑点] <文件:行号证据与选项>` 后结束当前轮进入 `waiting`，宿主立即唤醒主代理；
   - 主代理亲自 `read_file` 核实源码做出裁决后，调用 `post_message{text: "@<工人名> 裁决如下：..."}` 唤醒该工人原地继续执行。
6. **配额监控与切换**：调用 `swarm(action="models")` 可查看实时 `quota_remaining`；若某工人因配额耗尽停在 `waiting`，向用户说明原因后按用户指示处理，严禁私自切换未经授权的模型家族。
7. **给每个 Swarm 工人的 `brief` 必含硬约束尾注**：
   - 先全文读取 `~/.codex/skills/认知一致性/SKILL.md` 与 `~/.gemini/config/skills/agy-dual-model-pipeline/references/blacklist.md`；
   - **严禁调用 `read_board{since:0}` 翻看历史看板旧帖，严禁 `@` 除 `@coordinator` 以外的任何成员，严禁自行调用 `swarm(action="add")` 派生子助手**；
   - 完工交报告或中途提问时，必须在 `post_message` 正文开头写明 `@coordinator`。

### 2. Codex 驱动（`codex_drive.py`）
1. **车道与实时观看**：
   - 每个命令都带 `--lane <名字>`：阶段 3 反方固定用 `counter`，阶段 5 严审固定用 `review`。不同车道可并行，同一车道内串行（忙时返回 `75 BUSY`）。
   - 每次执行 `turn` / `answer` / `wait` 都用 `run_command(IsDaemon=false, WaitMsBeforeAsync=500)`：本轮完成（退出码 `0`）、失败（`1`）或提问（`77`）时脚本退出，反重力原生通知立即拉起主代理。
   - 用户在终端执行 `codex watch`（或 `codex watch counter` / `codex watch review`）即可实时观看；观看器用 `--no-alt-screen` 内联模式运行，支持滚动翻看历史，主代理直接推进无需等待确认。
2. **后台服务与中途提问（`request_user_input`）**：
   - `codex app-server daemon start` 监听 `~/.codex/app-server-control/app-server-control.sock`（WebSocket JSON-RPC），`codex_drive.py` 仅用标准库直连。
   - 新建会话自动开启 `features.default_mode_request_user_input` 并注入 `ASK_RULE`：遇到必须由主代理裁决的边界不猜不扩，由根代理调 `request_user_input` 提问（subAgent 先向根代理汇报）。
   - Codex 提问后在后台原地等待，脚本把问题写入该车道 `question.json`、打印问题并以退出码 **`77`** 退出唤醒主代理；主代理 `read_file` 核实源码裁决后写好回答文件，执行 `answer --prompt-file <回答>`（`IsDaemon=false`）送回答案继续跟踪，直至下一问（`77`）或本轮完成（`0`/`1`）。驱动意外中断时用 `wait` 重新挂接。
3. **流式注入与换会话**：
   - 运行中可用 `steer` 调 `turn/steer` 把补充说明塞进当前轮（无运行轮返回 `76 IDLE`）。
   - `new --lane <counter|review> --cwd <repo>` 后，该车道下一次 `turn` 自动开启全新无上下文会话，观看器自动跟随切换。
4. **只读边界（配合纯 Skill 版 `$stop-that-shit`）**：
   - 本流水线中 Codex 全程固定传 `--mode review`（首行自动发送 `$stop-that-shit review` 声明只读审查意图），每轮结束后由主代理执行 `git status --short` 与 `git diff` 核验工作区未受污染。

```bash
S=~/.gemini/config/skills/agy-dual-model-pipeline/scripts
codex app-server daemon start                                                                           # 阶段开始时确保后台服务运行
python3 $S/codex_drive.py new    --lane counter --cwd <repo_root>                      # 换无上下文新会话（下一轮 turn 生效）
python3 $S/codex_drive.py turn   --lane counter --mode review --prompt-file <work>/prompts/counter.md   # IsDaemon=false, WaitMsBeforeAsync=500
python3 $S/codex_drive.py new    --lane review  --cwd <repo_root>
python3 $S/codex_drive.py turn   --lane review  --mode review --prompt-file <work>/prompts/review_r1.md # IsDaemon=false, WaitMsBeforeAsync=500
python3 $S/codex_drive.py steer  --lane <车道>  --prompt-file <work>/prompts/steer.md                   # 注入当前运行轮
python3 $S/codex_drive.py answer --lane <车道>  --prompt-file <work>/prompts/answer_N.md                # 退出码 77 后回答提问；IsDaemon=false
python3 $S/codex_drive.py wait   --lane <车道>                                                          # 驱动中断后重新挂接；IsDaemon=false
```

### 3. 上下文压缩强制重读门禁（`compaction_skill_gate.py`）
全局 `~/.gemini/config/hooks.json` 挂载 `compaction-skill-reload-gate`（[scripts/compaction_skill_gate.py](scripts/compaction_skill_gate.py)），以 `~/.gemini/antigravity/brain/<conversationId>/.system_generated/logs/transcript.jsonl` 为唯一事实源拦截 `CHECKPOINT` 压缩后的失忆：
- **必读集合与完成判据**：收集历史 `<SKILL>` 标签及 `DONE` 状态读取过的非内置 `SKILL.md` 与强绑定规范（`~/.codex/skills/认知一致性/SKILL.md`、[references/blacklist.md](references/blacklist.md)）；最后一个 `CHECKPOINT` 后对目标文件完成真实全文覆盖读取（`Showing lines 1-N of N`、分段行/字节区间完整拼齐 `1..N` 全字节，或 `view_file` 全文标记；被字节上限截断的半截 `Partial read` 不放行）即解锁。
- **三道门禁**：`PreInvocation` 注入 `ephemeralMessage` 提醒重读；`PreToolUse` 放行 `read_file`/`view_file` 并拒绝未在上一轮读齐前调用的任何其他工具（防同轮抢跑）；`Stop` 在 `NO_TOOL_CALL` 未读齐时最多拦截 1 次返回 `continue`（防死循环；压缩前已通过 `send_message` 或 `post_message` 交差完工且无新输入的收尾工人会话自动豁免）。

---

## 四、五阶段执行流程（每阶段必过主代理清场与亲核裁决门禁）

**每个阶段完成后，主代理必须先销毁当前阶段 Swarm 工人（`swarm(action="kill")` 清空集群）、亲自调用 `read_file` / `git diff` 核对源码并向用户输出本阶段逐条裁决表，裁决通过后才允许创建下一阶段工人或启动 Codex**。大批量问题按文件与调用链完全正交切分为多批（Block 1, Block 2, ...），严禁跨阶段保留旧工人或复用旧 Codex 会话：

```text
单批流程（主代理全程坐镇中间每一道关卡）：
[阶段 1: Swarm Carbon Medium ×3~4 广撒网] ──► kill 清场 ──► 【门禁 1: 主代理亲核源码初裁】
                                                                │
                                                                ▼
[阶段 2: Swarm Carbon Medium 实证核验+深挖] ──► kill 清场 ──► 【门禁 2: 主代理亲核源码裁决】
                                                                │
                                                                ▼
[阶段 3: Codex Ultra 反方 (--lane counter)] ───────────────► 【阶段 4 / 门禁 3: 主代理终局裁决 adjudicated_plan.md】
                                                                │
                                                                ▼
[5.1 Swarm Carbon Medium 首稿] ► kill ► 【5.2 亲审 diff 初筛】 ► [5.3 Codex 严审] ► 【5.4a 亲核严审裁决】 ► [5.4b Swarm Carbon Medium 对账+返修] ► kill ► 【5.4c 亲审 diff】 … ► [5.6 终局门禁]
```

### 阶段 1：Carbon Medium 广撒网（只读）与主代理初裁
- 按职责正交切分为 3～4 个分片，一次调用 `swarm(action="add", name="s1-b1", model="gemini-4-carbon-medium", brief=..., tasks=[...])` 并发派发（若用户已提供完整扫描线索清单，则直接进入主代理阶段 1 亲核初裁）。首轮回复向用户展示 Swarm 看板胶囊链接并结束回合。
- **强制注入触发**：`brief` 中必须附上 [../认知一致性/SKILL.md](../认知一致性/SKILL.md)（或 `~/.codex/skills/认知一致性/SKILL.md`） 与 `~/.gemini/config/skills/agy-dual-model-pipeline/references/blacklist.md` 的绝对路径、4 条第一性原理拷问、项目**已裁决不改的 by-design 清单**，以及禁翻旧板/只向 `@coordinator` 汇报的硬规矩。
- **任务要求（发挥全局扫描优势）**：
   - **新功能开发 / 重构 / 故障修复**：沿实际调用链追踪数据、状态和职责，找出共同语义与复杂度根源，给出**源头统一、同一种事只有一条路**的实现或重构方案（含需迁移的全部调用点与需清理的旧实现/旁路），严禁给局部打补丁的“最小改法”。
   - **代码审查 / 去冗余治理**：对照真实调用链、边界契约与 `references/blacklist.md`，重点找架构级问题：同一件事有两条路、重复事实源、职责放错地方、跨模块重复、重复防御与无用兜底、死代码。每条线索给出精确的文件:行号与调用链。
- **【主代理阶段 1 清场与亲核初裁】**：等 `swarm(action="status")` 确认全部分片均已交报告进入 `waiting` 后（未交齐时用 `swarm_wait{since: ..., mentions_only: false}` 挂起），主代理立即调用 `swarm(action="kill", name="s1-b1")` 整组销毁；**亲自 `read_file` 初核源码并向用户输出《阶段 1 初裁表》后，再派发阶段 2**。

### 阶段 2：工具实证核验 + 深挖核心链路（只读）与主代理阶段 2 裁决
- 调用 `swarm(action="add", name="s2-b1", model="gemini-4-carbon-medium", brief=...)` 启动全新无上下文核验工人（若任务 B 涉及核心调度/领域算法、复杂跨层事务或并发状态机深水区，允许单点传 `model="gemini-4-carbon-high"`）。
- **任务 A：逐条实证核验阶段 1 初裁保留的线索**，给出“成立 / 不成立 / 存疑”和证据：
  - 用 `python3 -m tools.symbol_locator`、`rg` 和实际运行探针取证：真的 0 调用吗？上游真的已经算过 / 校验过吗？是不是已裁决的 by-design？
  - 鸭子接口、框架钩子等**隐式调用必须动态实测**，静态零调用不算数。
  - “不成立”的条目**连同证据保留在报告里**，供主代理复核，不得擅自删除。
- **任务 B：独立深挖 1 条高风险核心链路**（主代理指定，如核心计算、事务、并发），专找逻辑 bug、状态 / 事务 / 契约问题。
- **强制注入触发**：提示词必须附上《认知一致性》、`references/blacklist.md`、by-design 清单、4 条第一性原理拷问及禁翻旧板规矩。
- **【主代理阶段 2 清场与亲核裁决】**：收到 `@coordinator` 报告后，主代理立即 `swarm(action="kill", name="s2-b1")` 清空集群；亲自用 `read_file` 核对目标源码与核验证据，向用户输出**《阶段 2 逐条裁决表》**（确认成立项、推翻误判项、补充新发现项、拟定每条的源头改法草案），裁决完毕后再送入阶段 3。

### 阶段 3：Codex Ultra 当反方（`--lane counter --mode review`）
- **必先执行 `codex_drive.py new --lane counter --cwd <repo>` 开启全新、无上下文的独立会话**，再发送经阶段 2 主代理裁决后的方案草案。
- **职责收窄为一个问题：如果照这条改，会弄坏什么？** 重点拦截：
  1. 漏看下游消费者与调用方（改了上游结构却漏改下游消费端）、签名 / 返回值 / 契约破损、跨入口语义分裂（表单入口 vs 文件导入入口、全局 vs 个人、预检 vs 确认）；
  2. 删掉的是否是**真实的业务保护**（快照一致性、完整范围覆盖、旧事实保护等）；
  3. 不安全的缓存键、不可移植调用（如 Windows 下不可用的 `MSG_DONTWAIT`）、过度复杂的状态机；
  4. 方案是否真在源头统一，是否留了第二条链路、兼容空壳或下游补偿。
- 对每条给出 `Confirm` / `Refine` / `Reject` 及源头解法；要求等待内部所有 subAgent 完成后再输出汇总。
- **它不负责判断“这段防御该不该留”**。凡是建议新增兜底、保留旧兜底的意见，主代理默认从严：必须给出真实的失败场景才采纳。
- 结束后主代理用 `git status --short` 确认工作区未被意外改动，进入阶段 4 终局裁决。

### 阶段 4：主代理亲核源码终局裁决
- **【铁律】主代理必须亲自用 `read_file` 核对每一处目标源码**：
  - 严禁照抄报告中的伪代码函数名、参数顺序或字段名；亲自核实真实签名、数据结构（区分全形态必有键与按判别字段区分的多态字典）及全部待迁移调用者（用 `python3 -m tools.symbol_locator` 查定义与引用）。
  - **严禁针对单点症状打表象补丁**：凡发现问题，必先追问“为什么模块设计允许这类分叉/状态失步存在”，在源头把多条散落路径、同模块兄弟入口与上下游契约一次性收口为唯一路径。
- **合并结论并编写 `<work>/adjudicated_plan.md`**：合并前期线索 + 实证核验 + Codex 反方意见（推翻或驳回须写明源码依据）；核对修改前工作区状态（`git status --short`）并保存受改文件备份快照；将方案切分为块与**严格互不重叠的文件分片**（含精确文件:行、真实签名、源头改法、待迁移调用点、待删旧旁路、已裁决不改项），向用户汇报**《阶段 4 终局裁决表》**。

### 阶段 5：首稿 → Codex 严审 → 对账与源头返修闭环（核心铁律！）

阶段 5 **严禁改一遍就交差，更严禁一轮又一轮只修表象症状**，必须严格执行以下分阶段清场闭环：

1. **5.1 首稿编码（`swarm add, name="draft-bN", model="gemini-4-carbon-medium"`）**：
   - 按 `adjudicated_plan.md` 严格互不重叠的文件分片派发（单分片直接 `add`，多分片传 `tasks=[...]`；默认 `workspace:"inherit"` 直接改主工作区，仅并发测试互扰时用 `workspace:"share"`），强制先读《认知一致性》与 `references/blacklist.md`（4 条拷问与 15 个母题）。
   - **严防漏改自检**：每删改一个函数/参数/字段，用 `symbol_locator` 与 `rg` 核查分片内所有调用点、导入、类型标注与测试，禁留未用 import、悬空调用、半截子死代码（母题 C）或违规兜底；跑 `.venv/bin/ruff check`、`.venv/bin/python -m pyright`（相关文件）与针对性 `pytest`，完成后 `@coordinator` 汇报。
2. **5.2 主代理清场、初筛与反过度防御裁决门禁（主代理严禁亲自改代码！）**：
   - 收齐 `draft-bN` 汇报后（若用了 `workspace:"share"` 先合入提交），立即调用 `swarm(action="kill", name="draft-bN")` 清空集群。
   - 主代理亲自用 `git status --short`、`git diff` 与 `read_file` 做**双重铁面裁决**：① **源头完成度与四步收缩**（是否交出定型结构保证，有无只改表层、漏改跨语言调用方/测试、残留旧符号引用/私有名/旧原语守卫或函数内延迟 `import` 躲环）；② **反过度防御专项**（逐行对照 4 条拷问与 15 个母题，严查无用判空/`.get()`、`getattr`/`hasattr`、宽泛 `except`、搬家兜底、无用拷贝、`=None` 伪可选、兼容旧壳或假测试）。
   - 跑 `ruff check`、`pyright` 与相关 `pytest`，向用户汇报 **《5.2 初筛裁决结论》**。**【主代理 0 动刀铁律】**：主代理严禁自己调 `edit_file`/`write_file` 改业务或测试代码；发现任何未完成或过度防御问题，先交 5.4b 返修剥离并重过 5.2，严禁带病送入 5.3！
3. **5.3 Codex Ultra 严审（`--lane review`，含反过度防御专项审查）**：
   - 每轮必先 `codex_drive.py new --lane review --cwd <repo>` 开启无上下文新会话，在工作区静止后整块送审。
   - **提示词必含三层内容**：
     1. **已知背景与已裁决边界**：本轮解决的问题清单、修改文件列表、裁决单路径、《认知一致性》与 `references/blacklist.md`、主代理已裁决不改的边界项（避免新会话重复提问或误报）；
     2. **反过度防御专项审查指令（必写！）**：“请对照《认知一致性》、4 条第一性原理拷问与 `references/blacklist.md` 的 15 个母题，专项严查本次改动及受影响上下游是否存在『过度防御之嫌』（无用判空/默认值兜底、受控结构 `.get()`/`getattr`、宽泛 `try/except` 吞错、母题 L 搬家兜底、母题 M 无意义拷贝、母题 A `=None` 伪可选、母题 F 重复校验、母题 B/J 兼容空壳或双轨开关、母题 D 靠编造非法输入测试续命），凡有嫌疑一律单列报出！”
     3. **强制开放式全链路猎杀指令（必写！）**：“严禁局限于已知问题清单！请主动向受改文件上下游调用链、同模块兄弟入口、跨入口（表单 vs 导入、预览 vs 提交）深挖猎杀跨入口语义分裂、漏改调用方/测试、契约不匹配、边界遗漏、次生死代码/死字段及违反 4 条第一性原理的坏习惯！”
4. **5.4a 主代理源头裁决（亲核源码拍板）**：
   - **【铁律】主代理亲自用 `read_file` 核对 Codex 每条意见，从源头根治而非头痛医头**：
     - **真 Bug、过度防御与源头统一项（必改且必连根拔起）**：一旦某处被抓出问题（尤其是同一模块连续两轮出问题），主代理必须深挖**结构性病根**（是否有多套状态副本、多处手写刷盘/回滚分支、按按钮打补丁而未收口共同入口、或守卫顺序倒置），将同文件与兄弟模块的同类入口一次性收口；
     - **保守防御伪问题与低收益吹毛求疵项（果断驳回或按规推迟结案）**：要求加回 0 调用死方法或内部兜底的直接凭源码事实驳回；不增进《认知一致性》的表面挑刺、人造极端篡改假设或无实质收益 Nit 逐条写明事实依据当场驳回（Reject）；确属授权范围外的独立存量问题记为推迟（写明客观病灶与明确重启条件，禁拿“非本轮引入”或无生产可达证据的“测试锁定”直接抹掉）。**严禁将终轮小问题整包放行；当某轮复审的全部条目（含子项）均已修完或逐条完成事实驳回/有条件推迟时，宣布闭环通过并进入 5.6 终局门禁，严禁无谓空转！**
   - **【强制向用户汇报逐条裁决清单】**：每轮 `CODEX ASKS` 与 `TURN COMPLETED` 均须向用户列出**《5.4a 严审逐条裁决表》**（条目、接受/驳回、源码证据、精确到文件与签名的源头返修方案）。
5. **5.4b Swarm 工人调用链对账与源头返修（`swarm add, name="fix-bN-rM"`）+ 5.4c 清场亲审**：
   - 每次必用新轮次名（`fix-b1-r1`、`fix-b1-r2`...）调 `swarm(action="add", model="gemini-4-carbon-medium", ...)` 开启全新无上下文工人（**机动升档开关**：若同一模块连续两轮被 Codex 抓出深层状态/逻辑漏洞未断根，或涉及跨多模块复杂事务重构，将该轮返修工人提档为 `model="gemini-4-carbon-high"`）。
   - **返修提示词硬规矩（先跑工具全调用链对账，再落刀）**：
     1. **动刀前强制工具对账**：先通读授权文件完整上下文，用 `python3 -m tools.symbol_locator` 与 `rg` 拉出受改符号的**全部真实调用点、同模块兄弟入口与测试替身签名**，与主代理裁决单逐项核对；
     2. **发现出入立即在看板 `@coordinator` 提问停手**：一旦对账发现主代理漏算了调用方/兄弟入口、签名与既有契约冲突、或必须联动修改授权外文件，**立即调用 `post_message` 发送 `@coordinator [对账疑点] <证据与建议>` 并结束本轮挂起等待主代理拍板**，严禁带病硬改或越界偷改；若对账严丝合缝无出入，直接落刀修改；
     3. **严守边界与门禁**：测试文件除非明确授权否则不许改，严禁新增任何过度防御；改完跑通 `.venv/bin/ruff check`、`pyright` 与针对性 `pytest` 直到全绿，在看板发 `@coordinator` 汇报对账结论与实际修改明细。
   - **5.4c 主代理清场与逐文件亲审 `git diff`**：收到返修完成汇报后，主代理立即调用 `swarm(action="kill", name="fix-bN-rM")` 清空集群；亲自用 `git status --short` 与 `git diff` 逐行复核：① 源头根治是否完整到位；② 有无新引入过度防御；③ 有无越界修改，跑通初筛门禁并向用户汇报后进入 5.5。
6. **5.5 Codex Ultra 第 N 轮复审（每次必先 `new --lane review`）**：
   - 提示词必含：① 上一轮修复核实清单与历史问题映射；② 已裁决不改的共识边界；③ 反过度防御专项审查指令；④ 开放式全链路猎杀指令。抓到普适新型反模式按 [references/blacklist.md](references/blacklist.md) 入库规则归类入库。
7. **5.6 主代理终局门禁验证（Final Referee Sign-Off）**：
   - 全量静态与质量门禁：`.venv/bin/ruff check <src_dirs> tests`、`.venv/bin/python -m pyright <changed_py_files>`、`.venv/bin/python -m tools.scan_over_defense --fail-on-new`、`.venv/bin/python -m tools.scan_dead_code_islands --mode quick`、前端构建与契约探针（若涉前端）及全量 `.venv/bin/python -m pytest -q`。

---

## 五、已知限制与避坑要点
- **Swarm 首轮看板工具晚一拍出现**：第一次调用 `swarm(action="add")` 的那一轮没有 `post_message`、`read_board`、`swarm_wait`，下一轮才会自动挂载；首轮加完工人、向用户展示看板链接后直接结束本轮即可。
- **Swarm 被 `kill` 的名字永久不可重用**：每次新建工人必须带阶段、批次或轮次后缀（如 `s1-b1`、`s2-b1`、`draft-b1`、`fix-b1-r1`、`fix-b1-r2`），绝不复用已销毁的名字。
- **`workspace:"share"` 删除风险**：默认使用 `workspace:"inherit"` 直接在主工作区修改互不重叠分片；若极少数场景启用了 `workspace:"share"`，必须在调用 `swarm(action="kill")` 之前将工人的提交合回主工作区，否则 `kill` 会连同独立工作树一并删除。
- **Codex 终端插话**：用户在 `codex watch` 终端亲自打字插话会开启新一轮对话，插话后告知主代理，等该轮结束后主代理再发 `turn`。
- **`codex_drive.py answer` 双重保障**：① `thread/resume` 期间通过 `on_event` 缓冲所有推送事件，确保重连瞬间的 `requestUserInput` 0 漏包；② 若通过 `select(0.5s)` 检测到 TUI 倒计时（`auto-resolves`）已提前自动关闭该提问，立即自动降级调用 `turn/steer` 将主代理的裁决文本实时注入当前正在运行的 turn（打印 `ANSWER STEERED`）并继续挂起跟流。
- **只读审查物理核验**：`$stop-that-shit` 现为纯模式指令约束（`$stop-that-shit review`），只读阶段（阶段 1 / 2 Swarm 工人及阶段 3 / 5.3 Codex）每轮结束后均以主代理亲自执行的 `git status --short` 与 `git diff` 为最终边界准绳。
