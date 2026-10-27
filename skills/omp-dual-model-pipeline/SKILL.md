---
name: omp-dual-model-pipeline
description: OMP 专属的双模型反转流水线（Vertex Global Gemini 4 Argon 16分片深挖与并发分片编码 ↔ OMP 原生 Codex GPT-6 Astra 多轮流式只读严审 ↔ 主代理亲核源码终局裁决与门禁验证）。融合《认知一致性》与 140 项反过度防御黑名单，用于第一性原理深水区挖掘、复杂故障根治、架构重构与代码审查。
---

# OMP 双模型反转流水线（Vertex Global Gemini 4a 编码 ↔ 原生 Codex 严审）

本技能在 **OMP 宿主环境**下生效，采用经过重度压测实战检验的**反转流水线**：
- **谁深挖、谁写代码**：**`vertex-global/gemini-4-argon:high`**（`@default`）—— 发挥超大上下文、全局视野与遵从指令优势，负责 **4×4（16 分片）第一性原理纯读深挖**与**严格不重叠文件分片（Disjoint File Slices）并发编码**。
- **谁只读挑刺、谁跨界猎杀**：**`openai-codex/gpt-6-astra:max`**（`@slow` / `agent: "reviewer"`）—— 直接使用 OMP 原生登录的 Codex，发挥其显微镜级边界挑刺与探针验证能力，**100% 纯只读（严禁写业务/测试代码，彻底杜绝其反扑式防御坏毛病）**，负责多轮流式严审，直到输出 **“本轮通过”**。
- **谁拍板裁决、谁跑门禁**：**主代理（Main Agent）**—— 亲自读源码核对行号与签名、按《认知一致性》与 15 大母题 / 140 项黑名单裁决 Codex 审查意见、统管门禁验证。

---

## 一、核心原则（必须先读）

1. **《认知一致性》**：`~/.codex/skills/认知一致性/SKILL.md`
   - 从共同语义出发，在源头统一数据、行为与职责，让同一种事情只有一条路；问题源于职责分散、重复链路或下游补偿时，直接重构共同机制、迁移全部调用者并清理旧实现与旁路。
2. **《4 条第一性原理拷问 + 15 大母题 / 140 项反过度防御黑名单》**：`~/.agents/skills/omp-dual-model-pipeline/references/blacklist.md`（及案例库 `references/blacklist-cases.md`）
   - 派发任何研究、编码或审查子代理时，必须要求其先完整阅读主表 `blacklist.md`（含 4 条第一性原理拷问与 A～O 15 个母题的识别信号，需要核对具体案例时按编号查 `blacklist-cases.md`）。

---

## 二、OMP 模型与子代理路由规则（Model Routing in OMP）

在 OMP（`~/.omp/agent/config.yml`）中，模型别名已绑定如下：
- `@default` = **`vertex-global/gemini-4-argon:high`**（Vertex Global 渠道的 Gemini 4 Argon）
- `@slow` / `@vibe_good` / `@vibe_fast` = **`openai-codex/gpt-6-astra:max`**（OMP 原生登录的 Codex）

调用 `task` 工具时的精确路由方式：
1. **第一性原理纯读深挖（Research Subagents）**：
   - 使用 `agent: "scout"`（默认绑定 `@default` = `vertex-global/gemini-4-argon:high`）或 `agent: "task", model: "vertex-global/gemini-4-argon:high"`（Prompt 中明令 100% 纯只读，只把报告写到 `local://...`）。
   - **并发批次**：单批建议 **4 个并发子代理**；若要做 4 大块 × 4 子分片 = **16 个深挖分片**，可按 4 批（每批 4 个）或流式补位（完成 1 个立即补发下 1 个，始终维持 4 个在跑）推进；若遇 `429 RESOURCE_EXHAUSTED`，绝不重开新代理，直接用 `write(path="agent://<id>", content="继续完成剩余分析并写入 local://...")` 原地唤醒续跑！
2. **分片并发编码（Coding Subagents）**：
   - 使用 `agent: "code-simplifier"`（在 `~/.omp/agent/config.yml` 将 `task` 绑定至 `@vibe_good` = Codex 时，`code-simplifier` 默认继承主会话 `vertex-global/gemini-4-argon:high` 且具备完整读写/执行工具；或显式指定 `model: "vertex-global/gemini-4-argon:high"`，确保由 **Gemini 4 Argon** 执笔写代码！）。
   - 每个编码子代理严格分配**完全不重叠的文件白名单（Disjoint File Slices）**，严禁跨分片改文件，严禁执行 `git commit` / `git checkout` / `git reset`。
3. **多轮流式只读严审（Codex Review Subagents）**：
   - 使用 `agent: "reviewer", model: "openai-codex/gpt-6-astra:max"`（OMP 原生 Codex，纯只读审查）。
   - **每一轮审查开始前**：启动一个**全新的** `reviewer` 子代理（确保上下文干净、无锚定偏见）。
   - **流式推进（完成一片、立即送审一片）**：
     - 当第 1 个编码子代理完成时，立即启动本轮 `reviewer` 子代理审查第 1 片；
     - 当第 2、3、4 个编码子代理陆续完成时，若本轮 `reviewer` 仍在运行，直接调用 `write(path="agent://<reviewer_id>", content="补充流式分片：Slice X 已完成，请在同一轮审查中一并核查以下文件：...")` 动态推入；若已完成则携带剩余分片继续唤醒或汇总。

---

## 三、标准四阶段工作流（4-Stage Flipped Pipeline）

### Stage 1：4×4（16 分片）第一性原理纯读深挖 + Codex 交叉核实
1. 将待深挖的四大深水区（每大块拆为 4 个正交子分片，共 16 个分片）交由 `vertex-global/gemini-4-argon:high` 纯读子代理逐行穿透扫描，输出结构化报告至 `local://trench_<M>_<N>.md`。
2. 每完成一个分片，主代理亲自 `read` 报告中提及的源码位置核实真伪，或派 `reviewer`（`openai-codex/gpt-6-astra:max`）对照真实源码与测试契约做交叉核验，剔除假阳性与低收益折腾，收敛出精确修复清单。

### Stage 2：主代理亲核源码 + 备份快照 + 4 路 Argon 不重叠分片并发编码
1. **保存可恢复快照**：修改前先将 `git status --short` 和 `git diff` 备份到 `/tmp/`。
2. **亲自核对源码签名与调用链**：主代理用 `read` 核实每个待改函数的真实签名、字段名与测试断言，按文件不重叠原则切分为 3～4 个编码分片。
3. **并发派发 `vertex-global/gemini-4-argon:high` 编码子代理**：
   - Prompt 中明确列出：授权修改文件白名单、每处修改的前后语义、80 类黑名单铁律（严防“马大哈”漏删导入、漏改调用点、漏迁测试观察点），并要求子代理改完后自行跑完对应文件的 `ruff check`、`pyright` 与定点 `pytest` / `.cjs` 测试。

### Stage 3：OMP 原生 Codex（`reviewer`）多轮严审 ↔ 主代理着眼项目未来的《认知一致性》终局裁决与子代理返修循环
1. **整块对齐送审 + 跨块只读深挖并发**：
   - 等待当前板块的全部分片编码子代理均完成并通过门禁（工作区静止）后再启动全新 `reviewer` 整块送审，严防边改边审引发脏工作区并发冲突；在 `reviewer` 纯只读严审第 $K$ 块的同时，并发派发第 $K+1$ 块的 4 个 Argon 只读深挖子代理。
2. **Dual Prompt 双重指令（无论是首轮严审还是第 $N$ 轮复审都缺一不可，严禁局限于已知问题清单！）**：
   - **明确靶心与已裁决边界**：清晰列出本轮每个分片解决的具体问题编号、修改文件、预期语义，以及主代理已明确裁决不改的边界共识项；
   - **强制开放式全链路猎杀指令**：明确警告 Codex `reviewer`：“严禁将审查视野局限于主代理或子代理已列出的问题清单！编码子代理（Gemini 4 Argon）虽然大局观好，但常犯‘马大哈’疏漏。请你不仅逐行核实已改代码和 85+ 类黑名单，更要主动顺着受改文件及上下游关联调用链、跨入口（表单 vs 文件导入、全局 vs 个人、预检 vs 确认）开放式深挖未提及的同类漏洞、契约分裂与回归！”
3. **主代理铁腕终局裁决（Referee Adjudication，严禁主代理自己动手改代码！）**：
   - **【主代理 0 动刀铁律】**：无论是首轮编码、初筛修错还是严审后的每一轮返修，**主代理严禁自己调用 `edit` / `write` 直接修改业务代码或测试代码**，必须 100% 派发或唤醒 Argon 编码子代理执行修改！
   - **【着眼整个项目未来的《认知一致性》裁决标准】**：主代理必须先 `read` 亲核 Codex 报出的每一处源码，站在整个项目未来的可演进性与认知统一高度做判断（既不局限于打眼前补丁，也绝不盲从 Codex 的全部挑刺）：
     - **果断采纳（Adopt）**：凡属真 Bug、测试失败、跨入口/上下游对同一业务规则口径分裂、漏改调用点、残留双轨旁路/死代码，且**从源头统一后对整个项目未来的可维护性长期收益大于修改成本**的，立即派发或唤醒对应分片的 Argon 编码子代理从源头彻底统一成一条路；
     - **果断驳回（Reject）**：凡属保守防御习惯（如要求加回 `getattr`/`=None`/0调用死入口）或纯属吹毛求疵、改动收益小于折腾成本的条目（如改固化历史迁移脚本、违反既有测试强契约文案、无实质收益的微秒级折腾），主代理凭源码与实测证据当场果断驳回，并在下一轮提示词中明确列为“已裁决不改项”防止重复纠缠。
   - **【强制向用户汇报逐条裁决清单】**：每一轮 Codex `reviewer` 的反馈，主代理都必须向用户完整展示**逐条裁决表（条目、结论【接受 / 驳回】、源码位置、以及基于《认知一致性》和项目未来价值的裁决理由）**。
4. **循环直至签字通过**：
   - 子代理每修完一轮并通过门禁，开启**全新**的 Codex `reviewer` 会话（同样附带开放式全链路猎杀指令与已裁决不改边界）复审本轮改动，直到全新会话的 Codex `reviewer` 明确给出 **0 问题通过（Sign-off）** 为止。

### Stage 4：全量门禁验证与交付
在 Codex `reviewer` 签字“本轮通过”后，依次执行：
1. `.venv/bin/ruff check <src_dirs> tests/`
2. `.venv/bin/python -m pyright`
3. `.venv/bin/python -m tools.scan_over_defense --fail-on-new` 与 `.venv/bin/python -m tools.scan_dead_code_islands`
4. 若修改过前端目录：执行前端构建脚本并跑全套前端契约测试
5. 全量 `.venv/bin/python -m pytest -q`
