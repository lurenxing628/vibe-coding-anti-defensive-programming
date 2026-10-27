---
name: agy-dual-model-pipeline
description: 反重力专属的三模型架构设计、开发实现、源头修复、充分重构与代码审查流水线（Gemini 4 Argon 广撒网找问题与首稿编码 ↔ Claude Opus 5.5 核验线索、深挖核心链路与返修 ↔ Codex GPT-6 Astra Ultra 当反方与多轮只读严审 ↔ 主代理亲核源码终局裁决与门禁验证），多车道并行。融合《认知一致性》与反过度防御原则，用于新功能开发、复杂故障修复、架构重构与代码审查。
---

# 反重力三模型流水线（Gemini 广撒网与首稿 + Claude 核验与返修 + Codex 反方与严审 + 主代理裁决）

本技能把三家模型按各自长处分工，并由 **主代理担任铁面裁判**，用于完成**架构设计、功能开发、故障源头修复、充分重构与代码审查**：
- **Gemini 4 Argon**（反重力原生子代理：`research` 只读广撒网，`self` 分片写首稿）；
- **Claude Opus 5.5**（[scripts/claude_drive.py](scripts/claude_drive.py) 驱动 tmux 中的原生 Claude Code 会话：核验线索、深挖核心链路、按裁决单返修）；
- **Codex GPT-6 Astra Ultra**（[scripts/codex_drive.py](scripts/codex_drive.py) 驱动本地 `app-server daemon`，锁定 `--mode review`：当反方、多轮只读严审）。

> [!IMPORTANT]
> **为什么这样分工？**
> - **GPT（Codex）不当找问题的人，当反方**：本仓库大多是 GPT 写的。模型偏爱自家和同家族的输出，越“眼熟”的写法越觉得正常，所以 GPT 查 GPT 写的代码有盲区，而且它偏爱防御性代码，会少报过度防御、多建议加兜底。它写代码也心机重，删一处明面兜底会偷偷在下层塞入“搬家式吞错、伪可选参数、自编假测试锁死死分支”（见 [references/blacklist.md](references/blacklist.md) 母题 L「删旧壳时搬家兜底」与 D「靠测试续命」）。但它做只读 Review 极其细致毒辣，擅长抓漏改调用点、契约破损、边界漏洞与类型不一致。因此让它只回答“这样改会弄坏什么”：它的保守习惯放在这里，正好防止另外两家删过头。
> - **Gemini 广撒网、写首稿**：大局观强（超长上下文最强），大刀阔斧、不搞防御性小心思，敢于彻底删死代码和统一源头机制。但它容易犯“马大哈”毛病：漏改下游调用方、漏删未用导入、改半截子签名、忽略边界细节，找问题时也会误报。所以它的线索必须经 Claude 核验，它的代码必须经 Codex 严审。
> - **Claude 核验、深挖、返修**：长在顺着调用链跑工具取证、抓逻辑 bug，以及按明确清单精确修改并把门禁跑到全绿。短在范围膨胀、遇阻绕路，规格模糊时会为了过测试硬编码。所以返修必须给逐条裁决单、写死文件范围、测试文件不许改，做不到就提问。
> - **每一份产出都由另一家模型检查**：Gemini 的线索由 Claude 核验，Gemini 和 Claude 的代码由 Codex 严审，避免同家族互评的盲区。最终由主代理亲核源码裁决。

用户在反重力终端里执行 `codex watch` / `claude watch`，即可通过原生界面实时观看 Codex / Claude 的思考、工具调用与内部子代理过程。

---

## 一、核心原则：一“立”一“破”

本流水线同时约束**怎么设计好结构（立）**与**禁止写出什么垃圾代码（破）**：

### 1. 立：认知一致性（源头统一、同一件事只有一条路）
完整原则见 [../认知一致性/SKILL.md](../认知一致性/SKILL.md)（或 `~/.codex/skills/认知一致性/SKILL.md`），贯穿全流程的核心精神如下：
- **首要目标是降低长期理解与修改成本，而非追求单次 diff 行数最小**：禁用“最小补丁 / 最小改法”思维。不要为了少改几行代码去打局部特判、绕开现有结构或在下游做观察/同步/补偿。
- **让同一种事情遵循同一套规则**：把共同语义放在一处实现，复杂度花在**源头把路合成一条**。新增能力或修复问题时，先核对它属于哪个概念、能否进入已有共同路径，绝不引入第二个事实源或第二条并行链路。
- **把现有实现和设计当作可变项，用充分重构换后续简单**：若当前问题源于职责分散、状态重复或数据路径别扭，直接重构共同机制。先把共同机制做完整，再迁移全部实际调用者，同步删除被替代的旧实现和旁路——**只在旧结构外再包一层不算完成**。
- **靠推广规则而非追加特判消解问题**：发现漏洞时，先看能否推广已有规则或删除造成分歧的条件，而不是逐条叠加特例。
- **保护机制与真实职责相称**：在真正承担责任的边界完成检查；解决真实问题所需的机制弄清职责后完整保留，删去各层重复防御、无用兜底和阻碍正常工作的机制。

### 2. 破：4 条第一性原理拷问 + 母题主表
坏习惯分两层维护（不塞进本文件，避免跨项目噪音与双份维护）：
- **主表** [references/blacklist.md](references/blacklist.md)：4 条第一性原理拷问的全文，以及按拷问分组的母题（识别信号、正确做法、最短例子、案例编号）。阶段 1 / 2 / 3 / 5 派发时必须附上它的绝对路径，要求先读再动手。
- **案例库** [references/blacklist-cases.md](references/blacklist-cases.md)：已抓获的全部具体案例，编号永久不变。只给路径、按需查，不整份传给子代理。

对每一处设计与每一行代码改动，用 4 条拷问主动过筛（全文见主表）：
1. **存在性**：删掉会怎样？真实调用链与数据契约下行为不变，就是废代码。
2. **单一事实源**：上游是不是已经算过、查过、证过了？拿到了就显式传下来复用。
3. **信任边界**：外部不可信边界只校验一次且校验到位；内部受控结构不写兜底、反射探测、防御拷贝和事后自检。
4. **语义保真**：比较、合并、过滤、去重、归一化、降级之后，合法数据的意思变了吗？前三条抓“多做了”，这一条抓“做错了”；删防御时用它确认删掉的不是真实业务保护。

审查或复核中抓到新套路，当场剥离，并按主表“四、入库规则”先归类再入库：能归进已有母题的只追加到案例库，归不进才新开母题。

---

## 二、角色与模型

| 阶段与角色 | 实现 | 模型 |
| :--- | :--- | :--- |
| 阶段 1 广撒网找问题 / 推导方案 | `invoke_subagent(TypeName="research", Model="inherit")`，3～4 个正交分片并发 | 继承主会话模型（Gemini 4 Argon） |
| 阶段 2 核验线索 + 深挖核心链路 | `claude_drive.py --lane verify`，`--mode review` 只读 | `claude-opus-5-5` + `xhigh` |
| 阶段 3 当反方 | `codex_drive.py --lane counter`，`--mode review` 只读 | `gpt-6-astra` + `ultra`（内部自行派发 subAgent） |
| 阶段 4 终局裁决 / 各轮裁决与门禁 | 主代理亲自完成（亲读源码、审 diff、辨别真问题与过度防御、跑门禁） | 主会话模型 |
| 阶段 5 首稿编码 | `invoke_subagent(TypeName="self", Model="inherit")`，按互不重叠的文件分片并行 | 继承主会话模型（Gemini 4 Argon） |
| 阶段 5 多轮严审 | `codex_drive.py --lane review`，`--mode review` 只读，**每轮先 `new` 开新对话** | `gpt-6-astra` + `ultra` |
| 阶段 5 返修（首稿之后的一切修改） | `claude_drive.py --lane fix`，`--mode change --files '<本块文件>'` | `claude-opus-5-5` + `xhigh` |
| 实时画面 | 用户在反重力终端执行 `codex watch` / `claude watch`（[scripts/codex_view.py](scripts/codex_view.py)、[scripts/claude_view.py](scripts/claude_view.py)） | — |

> [!IMPORTANT]
> - `invoke_subagent` 的 `Model` 参数只接受 `inherit / flash_lite / flash / pro`。要让阶段 1 和阶段 5 的子代理都用上 Gemini 4 Argon，派发时主会话本身必须是 Gemini 4 Argon。
> - `research` 子代理只读，报告放在最终消息里返回，由主代理落盘；`self` 子代理具备读写与执行权限，只在严格限定、互不重叠的文件分片内写首稿和跑针对性测试。
> - **常规中小开发/修复任务的直通路径**：需求与涉及模块已非常明确、无需大范围找问题时，跳过阶段 1～3，直接从**阶段 4（主代理亲核源码制定方案）→ 阶段 5（Gemini 首稿 → Codex 严审 → Claude 返修 → 主代理裁决与门禁）**启动。
> - Claude 不可用时，阶段 2 由主代理亲自核验，阶段 5 返修退回由 Argon `self` 子代理完成。

---

## 三、宿主机理与调用命令

### 1. 车道（两个驱动共用）
- 每个命令都必须带 `--lane <名字>`。一条车道就是一个独立会话加一份独立状态，**不同车道可以同时跑**；同一车道内串行，忙时返回 `BUSY`（75）。
- 固定车道名：Claude 用 `verify`（阶段 2）和 `fix`（阶段 5 返修）；Codex 用 `counter`（阶段 3）和 `review`（阶段 5 严审）。
- 退出码两边一致：0 这一轮完成；1 失败（Claude 侧还包括 `BLOCKED` / `CLAUDE EXITED` / `NOT ACKNOWLEDGED`）；75 `BUSY`；76 `IDLE` / 没有待答问题；77 模型在提问；Claude 另有 2 = 模式不一致。
- 每次执行 `turn` / `answer` / `wait` 都用 `run_command(IsDaemon=false, WaitMsBeforeAsync=500)`：这一轮结束或模型提问时脚本退出，反重力原生通知立即唤醒主代理。多条车道同时跑时，各自起一个命令。
- **实时观看**：`codex watch` / `claude watch` 默认跟随最近开始一轮的车道；`codex watch counter`、`claude watch fix` 这样加上车道名，就固定看那条车道。想同时看几条车道，多开几个终端。主代理直接推进执行，无需询问用户是否已打开观看器。

### 2. Codex 驱动（`codex_drive.py`）
1. **后台服务**：`codex app-server daemon start` 启动后监听 `~/.codex/app-server-control/app-server-control.sock`（WebSocket JSON-RPC）。驱动脚本仅用 Python 标准库直连该 socket，多条车道共用同一个后台服务、各自一个线程。
2. **Codex 中途提问（`request_user_input`，2026-10-22 实测）**：
   - 驱动脚本新建会话时打开 `features.default_mode_request_user_input`，并在每轮首行指令后自动附上提问约定（`ASK_RULE`）：遇到必须由主代理裁决的问题不猜、不扩范围，由根代理调 `request_user_input` 提问。该工具**只允许根代理调用**，subAgent 调用会被拒绝，所以 subAgent 先汇报给根代理。
   - Codex 提问后这一轮在后台服务里**原地等待**（实测等 150 秒仍在等，不会自动超时），脚本把问题存进该车道的 `question.json`、打印问题并以退出码 **77** 退出，唤醒主代理。
   - 主代理核对源码、做出裁决后，把回答写进文件，执行 `answer --prompt-file <回答>`（同样 `IsDaemon=false`）。脚本重新挂到会话上，后台服务会把还没回答的问题重新发过来，脚本按 `itemId` 对上后送出回答，然后继续跟踪，直到下一次提问（77）或这一轮结束（0/1）。
   - 驱动脚本意外中断（比如反重力重启）时，执行 `wait` 重新挂上这一轮；如果还有没回答的问题，会立刻再报一次。
   - 只有用驱动脚本新建的会话才有这个工具。
3. **流式注入**：Codex 正在跑时，用 `steer` 调 `turn/steer` 把新内容直接塞进当前轮；无运行轮时返回 `IDLE`（76，改用 `turn`），有运行轮时 `turn` 返回 `BUSY`（75，改用 `steer`、`answer` 或 `wait`）。
4. **换会话**：`new --cwd <repo>` 之后，该车道的下一次 `turn` 新建会话并发出第一轮，观看器自动切换。观看器用 `--no-alt-screen` 内联模式运行，记录留在终端自己的滚动区，可以上下滚动查看历史。
5. **读写边界（配合纯 Skill 版 `$stop-that-shit`）**：
   - 本地 `stop-that-shit` 已移除全部自动 hook、Guard 与 `lock change files=` 拦截，现为纯 Skill 指导。
   - `--mode review`：首行发送 `$stop-that-shit review`，声明只读复审意图。本流水线中 Codex 全程固定使用 `--mode review`，并由主代理在复审后用 `git status --short` / `git diff` 核验工作区。
   - `--mode change`（仅备选）：首行发送 `$stop-that-shit change`（若传 `--files 'a.py|b.py'` 则在首段显式注明允许修改的文件范围），最终读写边界以主代理执行的 `git status --short` 与 `git diff` 为准。

### 3. Claude 驱动（`claude_drive.py`）
1. **宿主**：每条车道是 tmux 服务器 `agy-claude` 中会话 `claude` 的一个窗口，跑原生 Claude Code 交互界面（`claude-opus-5-5`、`xhigh`、`bypassPermissions`）。hooks 只通过 `--settings` 注入这些会话，不改全局设置；状态文件在 `~/.cache/agy-dual-model-pipeline/claude/<车道>/`。
2. **会话与模式**：`new --cwd` 后，该车道的下一次 `turn` 在同一个窗口里起新的 `claude --session-id <新UUID>`。模式随会话固定：
   - `--mode review` 禁用 Edit / Write / NotebookEdit，子代理同样拿不到；
   - `--mode change` 不禁工具，`--files` 写进系统提示词；
   - `turn` 的模式或文件范围与当前会话不一致时以退出码 2（`MODE MISMATCH`）退出，需先 `new`。
   - 提问约定和模式说明放在 `--append-system-prompt`，不用每轮重复。Bash 仍然能写文件，最终读写边界以主代理的 `git status --short` / `git diff` 为准。
3. **发送与跟踪**：首轮消息作为启动参数传入，之后的 `turn` / `steer` 用 tmux bracketed paste 粘贴提交，每条消息都要等 UserPromptSubmit hook 确认收到，没确认就报错退出。驱动跟踪 hook 写入的事件流，打印 `$ 命令`、`[工具] 路径` 和子代理起止；最终结果取自 Stop hook 的 `last_assistant_message`。
4. **一轮何时算完**：Claude 的子代理默认在后台跑，根会话可能先停、等子代理完成后被通知唤醒（打印 `[root turn paused …]` / `[background task finished …]`）。只有出现 `background_tasks` 为空的 Stop，才输出 `TURN COMPLETED` 并以 0 退出；API 错误（StopFailure）以 1 退出。
5. **Claude 提问（AskUserQuestion）**：PreToolUse hook 接住提问、写入该车道的 `question.json`，驱动以 **77** 退出；主代理裁决后执行 `answer --prompt-file`，hook 把这段回答交回去（一段回答对应本次调用的全部问题），这一轮原地继续。hook 最多等 24 小时；问题还没回答时，`wait` 会再报一次 77。
6. **插话**：运行中用 `steer` 粘贴的消息，会在两次工具调用之间并入当前轮；没有运行轮时返回 76。
7. **异常（均以退出码 1 退出）**：
   - `BLOCKED`：Claude 在界面里等人处理（例如提问超过 24 小时被退回原生选项框）。让用户在 `claude watch` 里切到该车道，按 F2 打开键盘处理、再按 F2 关闭，然后执行 `wait`。
   - `CLAUDE EXITED`：该车道的 Claude 进程没了，下一次 `turn` 会用 `--resume` 接回同一会话。
   - `NOT ACKNOWLEDGED`：消息没送进去（比如卡在启动对话框），在 `claude watch` 里处理后重发。
8. **观看器按键**：默认只看，键盘输入被屏蔽，误触不会发给 Claude；F2 打开/关闭键盘，F3 切换车道，鼠标滚轮翻看历史（`q` 退出翻看），Ctrl-C 退出观看、会话不受影响。状态栏列出所有车道，并显示当前车道的模式和会话 ID。用户按 F2 后亲自发的消息也会被记成一轮，主代理用 `wait` 即可跟上。

```bash
S=~/.gemini/config/skills/agy-dual-model-pipeline/scripts
codex app-server daemon start                                                                           # 阶段开始时确保后台服务运行
python3 $S/codex_drive.py  new    --lane counter --cwd <repo_root>                                      # 换新会话（该车道下一轮 turn 生效）
python3 $S/codex_drive.py  turn   --lane counter --mode review --prompt-file <work>/prompts/x.md       # IsDaemon=false, WaitMsBeforeAsync=500
python3 $S/claude_drive.py new    --lane fix     --cwd <repo_root>
python3 $S/claude_drive.py turn   --lane fix     --mode change --files 'a.py|b.py' --prompt-file <work>/prompts/fix_1.md
# 两个驱动的 steer / answer / wait 用法相同：
python3 $S/<driver>.py     steer  --lane <车道>  --prompt-file <work>/prompts/z.md                     # 注入当前运行轮
python3 $S/<driver>.py     answer --lane <车道>  --prompt-file <work>/prompts/answer_N.md              # 退出码 77 后回答提问；IsDaemon=false
python3 $S/<driver>.py     wait   --lane <车道>                                                        # 驱动中断或 BLOCKED 处理后重新挂上；IsDaemon=false
```

---

## 四、五阶段执行流程

### 并行排程（能并行就并行）
阶段之间是**职责边界，不是硬等待**。只读工作尽量流水重叠，只有写代码要等工作区静止：
- **块内**：
  - 阶段 1 的 3～4 个 Gemini 分片并发。**任一分片完成，就同时送进阶段 2（Claude `verify`）和阶段 3（Codex `counter`）**：第一份用 `new` + `turn`，后续分片在运行中用 `steer`、已空闲则用 `turn`。
  - 阶段 2 和阶段 3 **并行**，互不等待：Codex 只回答“照这条改会弄坏什么”，与线索真假无关；被 Claude 判为“不成立”的条目，Codex 对它的分析在阶段 4 直接作废。
  - 两边都完成后进入阶段 4 裁决。
  - 阶段 5 中，Gemini 首稿分片并发；Claude 返修时可在会话内派子代理，按互不重叠的文件并行改。
- **跨块**：第 K 块进入阶段 5 时，第 K+1 块立刻开始阶段 1～3。车道互不占用（`verify` / `counter` 给 K+1，`fix` / `review` 给 K），两块真正同时推进。
  - **前提**：阶段 4 切块时，让各块的文件和调用链互不相交。相交的块必须串行，等第 K 块收口后再开第 K+1 块的阶段 2～3，否则核验会读到正在改的代码。

```text
块 K   ：[1 Gemini 广撒网]→[2 Claude 核验 ‖ 3 Codex 反方]→[4 裁决]→[5 首稿→严审→返修→严审…→门禁]
块 K+1 ：                                                    [1 广撒网]→[2 ‖ 3]→[4 裁决]→[5 …]
```

### 阶段 1：Gemini 广撒网（只读）
- 按职责正交切分为 3～4 个分片并发派发。
- **强制注入触发**：给每个 Argon 子代理的提示词中，必须附上 `~/.codex/skills/认知一致性/SKILL.md` 与 `~/.gemini/config/skills/agy-dual-model-pipeline/references/blacklist.md` 的路径及 4 条第一性原理拷问，要求子代理先读取这两份文件再开始分析；同时附上项目**已裁决不改的 by-design 清单**，避免重复误报。
- **任务要求（发挥大局观）**：
  - **新功能开发 / 重构 / 故障修复**：沿实际调用链追踪数据、状态和职责，找出共同语义与复杂度根源，给出**源头统一、同一种事只有一条路**的实现或重构方案（含需迁移的全部调用点与需清理的旧实现/旁路），严禁给局部打补丁的“最小改法”。
  - **代码审查 / 去冗余治理**：对照真实调用链、边界契约与 `references/blacklist.md`，重点找架构级问题：同一件事有两条路、重复事实源、职责放错地方、跨模块重复、重复防御与无用兜底、死代码。每条线索给出精确的文件:行号与调用链。
- 子代理若因限流或中断停下，用 `send_message` 原地唤醒续跑，不丢弃重开、不由主代理代写。

### 阶段 2：Claude 核验线索 + 深挖核心链路（`--lane verify --mode review`，与阶段 3 并行）
- 第一个 Gemini 分片落盘后，立即 `new --lane verify` 再 `turn`；后续分片用 `steer` 流式注入。
- **任务 A：逐条核验 Gemini 线索**，给出“成立 / 不成立 / 存疑”和证据：
  - 用 `python3 -m tools.symbol_locator`、grep 和实际运行取证：真的 0 调用吗？上游真的已经算过 / 校验过吗？是不是已裁决的 by-design？
  - 鸭子接口、框架钩子等**隐式调用必须动态实测**，静态零调用不算数。
  - “不成立”的条目**连同证据保留在报告里**，供阶段 4 推翻，不得删除。
- **任务 B：独立深挖 1 条高风险核心链路**（主代理指定，如核心调度、事务、并发），专找逻辑 bug、状态 / 事务 / 契约问题，这是 Gemini 的弱项。
- 涉及 Claude 自己写过的代码（提交带 `Co-Authored-By: Claude`）时，在报告里标出，由主代理重点亲核。
- **强制注入触发**：提示词必须附上《认知一致性》、`references/blacklist.md`、by-design 清单与 4 条第一性原理拷问；要求等内部所有子代理完成后再输出汇总。

### 阶段 3：Codex 当反方（`--lane counter --mode review`，与阶段 2 并行）
- 第一个 Gemini 分片落盘后，立即 `new --lane counter` 再 `turn`；后续分片用 `steer` 流式注入。
- **职责收窄为一个问题：如果照这条改，会弄坏什么？** 重点拦截：
  1. 漏看下游消费者与调用方（改了上游结构却漏改下游消费端）、签名 / 返回值 / 契约破损、跨入口语义分裂（表单入口 vs 文件导入入口、全局 vs 个人、预检 vs 确认）；
  2. 删掉的是否是**真实的业务保护**（快照一致性、完整范围覆盖、旧事实保护等）；
  3. 不安全的缓存键、不可移植调用（如 Windows 下不可用的 `MSG_DONTWAIT`）、过度复杂的状态机；
  4. 方案是否真在源头统一，是否留了第二条链路、兼容空壳或下游补偿。
- 对每条给出 `Confirm` / `Refine` / `Reject` 及源头解法；要求等待内部所有 subAgent 完成后再输出汇总。
- **它不负责判断“这段防御该不该留”**。凡是建议新增兜底、保留旧兜底的意见，主代理默认从严：必须给出真实的失败场景才采纳。
- 结束后主代理用 `git status --short` 确认工作区干净。

### 阶段 4：主代理亲核源码终局裁决
- **【铁律】主代理必须亲自用 `read_file` 核对每一处目标源码**：
  - 严禁照抄任何报告中的伪代码函数名、参数顺序或字段名；
  - 亲自核实真实方法名与签名、真实数据结构与字段名（区分全形态必有键与按判别字段区分的多态字典，如先判别 `source == "internal"` 再索引分支专属键）、真实调用链入口与全部待迁移调用者（必要时用 `python3 -m tools.symbol_locator` 查全仓定义与引用）。
- **合并三方结论**：Gemini 线索 + Claude 核验证据 + Codex 反方意见。推翻 Claude 的“不成立”或驳回 Codex 的意见时，都要写明源码依据。
- **保存修改前快照**：在 `<work>/` 下保存 `git status --short`、`git diff` 与目标文件 `tar` 包备份。
- **编写裁决单 `<work>/adjudicated_plan.md`**：
  - 把方案切成**块**，再把每块切成**互不重叠的文件分片**，写明每个分片的文件列表、精确行号、源头改法、需同步迁移的调用点与需删除的旧旁路；
  - 写明每块的文件与调用链边界，供跨块并行判断；
  - 列出**已裁决不改项**，后续每轮提示词都要带上。
- **向用户汇报逐条裁决表**：条目、结论（接受 / 驳回）、源码证据，以及基于《认知一致性》和项目长期价值的理由。

### 阶段 5：Gemini 首稿 → Codex 严审 → Claude 返修闭环（核心铁律！）

Gemini 写首稿容易“马大哈”，Codex 百分之百会抓出问题。因此阶段 5 **严禁改一遍就交差**，必须执行以下多轮闭环：

1. **5.1 Gemini 首稿（`TypeName="self", Model="inherit"`）**：
   - 按裁决单把本块切成**严格互不重叠的文件分片**，并发派发 2～4 个 `self` 子代理。
   - **给每个 `self` 子代理的强制纪律**：
     - 必须先阅读 `~/.codex/skills/认知一致性/SKILL.md` 与 `~/.gemini/config/skills/agy-dual-model-pipeline/references/blacklist.md`（4 条拷问与全部母题）；
     - 严格只改本分片授权的文件，严禁跨分片触碰其他子代理的文件；
     - **严防“马大哈”自检清单**：每删一个函数 / 参数 / 字段，必须在分片内核查所有调用点、导入语句、类型标注和关联测试；禁止留下未使用的 import、悬空调用、半截子死代码（母题 C）或违反黑名单母题的兜底；
     - 改完后在本分片范围内跑 `.venv/bin/ruff check <本分片文件>` 与针对性 `pytest`。
2. **5.2 主代理初筛门禁（主代理严禁亲自改代码！）**：
   - 所有首稿子代理完成后，主代理用 `git status --short` 和 `git diff` 核对文件边界，跑 `.venv/bin/ruff check`、`.venv/bin/python -m pyright -p pyrightconfig.gate.json <changed_py_files>` 与相关 `pytest`。
   - **【主代理 0 动刀铁律】**：初筛或后续任何一轮发现语法、导入、类型或逻辑问题，**主代理严禁自己调用 `edit_file` / `write_file` 修改仓库业务代码或测试代码**。首稿之后的一切修改都交给 Claude（`fix` 车道，见 5.4）。初筛不过的问题先交 Claude 修，修完再过初筛，然后进入 5.3。
3. **5.3 Codex 严审（`--lane review`）**：
   - **【防上下文污染与防并发踩踏铁律】**：
     - 每轮代码 Review 前，必须先执行 `codex_drive.py new --lane review --cwd <repo>` 开启全新对话，彻底隔绝历史对话的自我辩护或锚定污染；
     - 等本块全部写代码的工作（首稿子代理或 Claude 返修）完成并通过初筛、工作区静止后，再一次性把整块送审。
   - **【每轮严审提示词必须包含两层内容（严禁局限于已知问题清单！）】**：
     1. **已知背景与已裁决边界**：本轮解决的问题清单、修改的文件列表、裁决单路径、《认知一致性》与 `references/blacklist.md` 主表、主代理已裁决不改的边界项，让它逐项核实改动是否真正从源头到位、有无引起回归；
     2. **强制开放式全链路猎杀指令（无论首轮还是第 N 轮都必写！）**：必须在提示词中显式声明——**“严禁将审查视野局限于已列出的问题清单！写代码的子代理容易犯马大哈疏漏，受改文件及其上下游关联调用链、跨入口（如表单入口 vs 文件导入入口、全局 vs 个人、预检 vs 确认）中极可能还存在其他未被指出的问题（包括但不限于：跨入口语义分裂、漏改的调用方/测试、签名或返回值契约不匹配、边界条件遗漏、删错有效业务防线、次生未清理的死代码/死导入/死字段、以及违反 4 条第一性原理和黑名单母题的残留/新引入坏习惯），请主动向周边调用链深挖猎杀！”**
4. **5.4 主代理裁决 + Claude 返修（`--lane fix --mode change`）**：
   - **【铁律】主代理亲自用 `read_file` 核对 Codex 报出的每一条问题，依据《认知一致性》并着眼于项目长期演进价值 vs 折腾成本做终局裁决（绝不盲从 Codex，也不局限于打眼前补丁）**：
     - **真 Bug 与增进《认知一致性》的源头统一项（必改）**：真实逻辑 / 边界 bug、跨入口或上下游对同一业务规则口径分裂、漏改调用点、残留双轨旁路 / 死代码 / 次生孤岛、违反黑名单与《认知一致性》，且从源头统一的长期收益大于修改成本的，100% 采纳；
     - **受保守防御习惯驱使的伪问题（直接驳回）**：要求“把已确认 0 调用的死方法加回来”或“给内部受控结构重新加上 `getattr` / `=None` 兜底”的，凭源码铁证直接驳回，绝不把防御性垃圾写回去；
     - **纯吹毛求疵且收益小于折腾成本（果断拒绝）**：不增进《认知一致性》、仅属表面挑刺的条目（例如去改早已固化的历史一次性迁移脚本 `v1..v44`、违反既有测试强契约文案、无实质收益的微秒级折腾），当场拒绝，并在下一轮提示词中列为“已裁决不改项”。
   - **【强制向用户汇报逐条裁决清单】**：每一轮 Codex 的提问（`CODEX ASKS`）与审查报告（`TURN COMPLETED`），主代理都要向用户完整列出逐条裁决表（条目、结论【接受 / 驳回】、源码证据、裁决理由）。
   - **派 Claude 返修**：
     - **会话切换规则（防上下文污染）**：只有针对“上一轮同一批已确定问题没修全的追补/微调”才沿用同一个 `fix` 会话；凡是进入新一轮审查发现的新问题或切换到不同文件范围时，必须先执行 `claude_drive.py new --lane fix --cwd <repo>` 新开干净的 Claude Code 会话，避免上一轮的大量工具输出污染上下文。
     - `--files` 填本轮允许修改的文件列表。确需超出范围时，Claude 会提问；主代理裁决后授权或对 `fix` 车道 `new` 一个扩大范围的新会话。
     - **返修提示词必须附上**：逐条裁决清单（写清问题根因、目标文件:行与源头统一方向）、《认知一致性》、`references/blacklist.md`、已裁决不改项，以及以下硬规矩：
       - **先看全貌再动刀（严禁无脑照抄主代理伪代码）**：动手改任何函数、类或字段前，必须先通读所在文件的完整上下文，并用 `python3 -m tools.symbol_locator` 与 `grep` 核查全部调用方、子类继承关系、同模块兄弟入口与关联测试；主动核验主代理的改法是否有遗漏边界、漏看调用点或次生副作用；
       - **严禁自作主张偷改裁决单外内容，提问攒到合理数量（如 2～4 个）即可批量提问**：在阅读上下文时，若发现受改文件或周边调用链还有主代理漏掉的同类问题、或发现某条裁决单改法需要调整/联动修改 `--files` 之外的文件，**严禁不经主代理裁决就自行顺手改掉，也不要每碰到一个小问题就立刻单独打断提问**——不必死等全部工作结束才问，只要攒到合理数量（例如 2～4 个问题，或完成当前小批次核查时）即可调用 `AskUserQuestion` 批量向主代理提问，经主代理亲核源码批准后再改；
       - 测试文件除非裁决单写明或经提问授权，否则不许改；不许为过测试改断言、跳过用例或硬编码结果；
       - 不许新增兜底、吞错、兼容层或伪可选参数；
       - 改完自己跑 `.venv/bin/ruff check`、`.venv/bin/python -m pyright -p pyrightconfig.gate.json <改动文件>` 与针对性 `pytest` 直到全绿，并在汇报中逐条列出实际改动及与裁决单的差异说明。
     - **合理并发派发 2～4 个子代理（严禁单线程串行硬啃大批量文件）**：当本轮返修涉及多个文件或较多条目时，必须在提示词中要求 Claude 根代理按**严格互不重叠的文件分片**并发派发 **2～4 个子代理**（`Agent` 工具，最多不超过 4 个）并行阅读上下文与执行修改，严禁在根会话里一个文件接一个文件串行硬改（既慢又容易因上下文膨胀改出问题）。各子代理遇到存疑或发现裁决单外问题时统一汇报给根代理，由根代理汇总去重后通过一次 `AskUserQuestion` 集中向主代理提问，并在全部子代理完成后统一跑门禁收口。
     - **主代理逐文件复核 `git diff`（防乱改铁律）**：收到 `TURN COMPLETED` 后，主代理必须亲自用 `git status --short` 与 `git diff` 逐处复核 Claude 的实际代码改动，核实其是否严格符合裁决、有无未经授权的夹带修改或改坏逻辑，再跑初筛门禁。
5. **5.5 Codex 第 N 轮复审（每次必先 `new --lane review` + 必带开放式猎杀指令！）**：
   - 复审提示词必须同时包含：① 上一轮问题的修复核实清单；② 主代理已裁决不改的共识边界（避免新会话重复提问或误报）；③ 开放式全链路猎杀指令。
   - 若复审仍抓出经主代理裁决确认的**高价值真问题**，继续执行 **“主代理裁决 → Claude 返修 → `new` 开新对话再 Review”** 循环，直到全新会话的 Codex 复审给出 **0 问题通过（Sign-off）** 为止。
   - 复审中抓到具有普适性的新型反模式套路，按 [references/blacklist.md](references/blacklist.md) 的“四、入库规则”先归类再入库。
6. **5.6 主代理终局门禁验证（Final Referee Sign-Off）**：
   - 全量静态与质量门禁：`.venv/bin/ruff check core data web tests`、`.venv/bin/python -m pyright -p pyrightconfig.gate.json <changed_py_files>`、`.venv/bin/python -m tools.quality_gate_scan --strict --json`、前端契约探针（若涉前端）及全量 `.venv/bin/python -m pytest -q`。

---

## 五、已知限制
- 用户在 `codex watch` 终端里亲自打字插话会开启新一轮对话，请用户插话后告知主代理，等该轮结束后主代理再发 `turn`。
- `codex_drive.py answer` 已内置双重保障：① `thread/resume` 期间通过 `on_event` 缓冲所有推送事件，确保重连瞬间的 `requestUserInput` 0 漏包；② 若通过 `select(0.5s)` 检测到 TUI 倒计时（`auto-resolves`）已提前自动关闭该提问，立即自动降级调用 `turn/steer` 将主代理的裁决文本实时注入当前正在运行的 turn（打印 `ANSWER STEERED`）并继续挂起跟流，确保哪怕过了 TUI 倒计时答案也能生效。
- `$stop-that-shit` 已移除自动 hook 与 Guard 工具层拦截，纯靠模式指令（`$stop-that-shit review`）与提示词约束，最终读写边界以主代理亲自执行的 `git status --short` 与 `git diff` 为准。
- Claude `--mode review` 只禁用了写文件工具，Bash 仍然能写文件，所以每轮结束后同样要用 `git status --short` 复核。
- Claude 一次调用里问多个问题时，一段回答会作为每个问题的答案，回答时自己分条写清。
- `claude watch` 开了鼠标支持，要选中文字复制时按住 Shift 或 Option 拖动（取决于终端）。
- 反重力终端切走再切回（视图重新挂载）后会丢掉 SGR 鼠标编码，滚轮因此失灵；tmux 在终端重新获得焦点时会自动重发鼠标模式，所以切回后先点一下终端再滚。仍不行就 Ctrl-C 后重新执行 `claude watch`。
