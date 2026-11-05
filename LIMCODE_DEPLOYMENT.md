# Limcode Test 适配改造与可复现部署方案

本文档记录将 `vibe-coding-anti-defensive-programming` 适配至 **VS Code Limcode Test**（`lurenxing628/limcode-test`）的完整设计映射、可复现部署步骤，以及向插件仓库或本仓库提交 PR 的改动清单。

---

## 一、为什么不能直接照搬 AGY / OMP 技能？

原仓库中的 `agy-dual-model-pipeline` 与 `omp-dual-model-pipeline` 绑定了各自宿主的外部进程和路径假设，直接拷入 Limcode 会产生三类违反本项目原则的问题：

1. **宿主控制面不匹配（死分支 / 死资产）**：
   - AGY 版依赖 `tmux`、`~/.gemini/config/skills/.../scripts/{codex_drive.py,claude_drive.py}` 与 Unix socket；
   - OMP 版依赖 `~/.omp/agent/config.yml`、`task(agent="scout"|"code-simplifier")`、`local://` 与 `write(path="agent://<id>")`。
   - Limcode Test 自身已内置可靠内核（`ReliableKernelApplication`），拥有原生的 `run_agent` 子代理生命周期控制、工具策略收窄（`READONLY_TOOLS`）和团队协作消息（`list_agents` / `send_agent_message`）。
2. **路径硬编码脱离技能基准目录**：
   - 旧技能硬编码 `~/.codex/skills/认知一致性/SKILL.md` 与 `~/.agents/skills/.../blacklist.md`。
   - Limcode 的 `skills` 工具加载任意技能时，都会注入 `Base directory for this skill: <skill.dir>`，并支持在 `run_agent(operation="spawn", skills=[...])` 时由控制面在子代理首轮直接预加载技能正文（`childSkillPreload.ts`）。
3. **双份文件拷贝破坏单一事实源**：
   - 用复制或重新从 GitHub 下载的方式安装到 `globalStorage` 会制造双份副本（且易因 Git `core.autocrlf` 产生 CRLF/LF 分裂）。
   - Limcode 的 `backend/capabilities/skillCatalog.ts` 原生支持跟随目录软链接 / Windows Junction（`realPathKey` 去重 + `linkedDirectories` 文件监听），应直接以软链接/目录联接指向本地仓库。

---

## 二、AGY / OMP → Limcode Test 原语映射表

| 流水线职责 | AGY / OMP 原做法 | Limcode Test 原生映射 | 内核保障机制（`limcode-test` 源码依据） |
| :--- | :--- | :--- | :--- |
| **Stage 1：正交分片纯读深挖** | `research` / `scout` 子代理 | `run_agent(operation="spawn", agent={ type: "explore" }, forkTurns="none")` | `backend/world/modules/agent/blueprints.ts`：`explore` 工具策略强制为 `READONLY_TOOLS`（剔除 `edit`/`write`/`delete`/`transfer`） |
| **Stage 2/3：不重叠文件分片并发编码与返修** | `self` / `code-simplifier` / `claude_drive.py --lane fix` | `run_agent(operation="spawn", agent={ type: "worker" }, forkTurns="none")`；同批次追补用 `operation="send"` | `worker` 具备完整读写与命令执行权限；单轮父会话最多并发准入 8 个 `spawn` |
| **Stage 3：多轮只读反方严审** | `codex_drive.py --lane review` / OMP `reviewer` | 每轮全新 `run_agent(operation="spawn", agent={ type: "reviewer" }, forkTurns="none")` | `blueprints.ts`：`reviewer` 强制 `READONLY_TOOLS`，从机制上杜绝 Codex 偷改业务/测试代码塞防御兜底 |
| **运行中流式补片（Steer）** | `codex_drive.py steer` / `write(path="agent://<id>")` | `list_agents` 获取 `C#` 后调 `send_agent_message(conversationRef="C#", text="...")`，或 `run_agent(operation="send", childRef="...", interrupt=false)` | 运行中的目标会话在下一个安全输入边界自动消费邮箱消息，无需中断重开 |
| **子代理技能注入** | 提示词写死 `~/.codex/skills/...` | `run_agent(..., skills=["认知一致性", "stop-that-shit", "limcode-dual-model-pipeline"])` | `backend/reliableKernel/childExecution.ts`：在子代理首轮事务内直接渲染预加载技能正文 |
| **黑名单引用路径** | `~/.agents/skills/.../references/blacklist.md` | 相对技能基准目录的 `references/blacklist.md` 与 `references/blacklist-cases.md` | `skills` 工具返回 `baseDirectory`，子代理直接拼接绝对路径调用 `read` |

---

## 三、跨环境可复现部署步骤

### 1. 克隆仓库（或拉取包含 `limcode-dual-model-pipeline` 的分支）

```bash
git clone https://github.com/lurenxing628/vibe-coding-anti-defensive-programming.git
```

### 2. 建立单一事实源链接（零文件拷贝）

Limcode 的全局数据根目录默认位于 VS Code `globalStorage/your-publisher.limcode-test`（若在插件中使用过“迁移数据目录”，则以 `.limcode-global-status.json` 中的 `dataFilePath` 所在目录为准），其下的 `skills/` 即为 `source="global"` 技能扫描根。

#### Windows (PowerShell)
```powershell
$repoSkills = "F:\code\vibe-coding-anti-defensive-programming\skills"
$limcodeSkills = "$env:APPDATA\Code\User\globalStorage\your-publisher.limcode-test\skills"

New-Item -ItemType Directory -Force -Path $limcodeSkills | Out-Null

# 清理可能残留的异构死技能或旧拷贝
foreach ($name in @('agy-dual-model-pipeline', 'omp-dual-model-pipeline', '认知一致性', 'stop-that-shit', 'limcode-dual-model-pipeline')) {
    $target = Join-Path $limcodeSkills $name
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

# 建立指向仓库源码的 Directory Junction（无需管理员权限，自动触发 skillCatalog 监听）
foreach ($active in @('认知一致性', 'stop-that-shit', 'limcode-dual-model-pipeline')) {
    New-Item -ItemType Junction -Path (Join-Path $limcodeSkills $active) -Target (Join-Path $repoSkills $active) | Out-Null
}
```

#### Linux / macOS (Bash)
```bash
REPO_SKILLS="/path/to/vibe-coding-anti-defensive-programming/skills"
# Linux 默认路径（macOS 请替换为 "$HOME/Library/Application Support/Code/User/globalStorage/your-publisher.limcode-test/skills"）
LIMCODE_SKILLS="${LIMCODE_SKILLS_DIR:-$HOME/.config/Code/User/globalStorage/your-publisher.limcode-test/skills}"

mkdir -p "$LIMCODE_SKILLS"
rm -rf "$LIMCODE_SKILLS/agy-dual-model-pipeline" "$LIMCODE_SKILLS/omp-dual-model-pipeline"

for s in "认知一致性" "stop-that-shit" "limcode-dual-model-pipeline"; do
  ln -sfn "$REPO_SKILLS/$s" "$LIMCODE_SKILLS/$s"
done
```

### 3. 在 Limcode 中配置双模型 Agent 路由（可选但强烈建议）

Limcode 解析子代理模型的优先级（见 `backend/reliableKernel/vscodeConfigurationAuthority.ts` 的 `resolveModelSelection`）为：
`agent 级 ModelProfile (model-profile:agent:<agentId>)` > `父会话继承模型 (modelFallback)` > `全局默认渠道`。

为使双模型反转流水线严格按预期分工运行，可在 Limcode 设置页将各 Agent 角色绑定为：
- **`explore`**：绑定 `gemini-4-argon`（或保持未绑定以继承 `gemini-4-argon` 主会话）—— 负责大上下文正交分片纯读深挖；
- **`worker`**：绑定 `gemini-4-argon` —— 负责不重叠文件分片并发编码与返修；
- **`reviewer`**：绑定 `gpt-6-astra-minor`（Codex）—— 负责 100% 纯只读反方挑刺与多轮严审。

### 4. 一键验证部署行为

无需编写任何哈希比对脚本，直接在 Limcode 对话中发送：
> 加载 `limcode-dual-model-pipeline` 技能

`skills` 工具在首次查找未命中时会自动调用 `deps.skills.refresh()`（见 `backend/world/modules/tools/definitions/skills/index.ts`），返回 `source="global"` 与技能正文即代表部署成功。

---

## 四、给 `limcode-test` 插件仓库提 PR 的可选内化方向

如果你准备在 `lurenxing628/limcode-test` 插件侧直接做原生整合，可按以下两个粒度之一提交 PR：

### 方案 A：在 `vibe-coding-anti-defensive-programming` 仓库提交 PR（零侵入插件）
直接提交本次新增的：
- `skills/limcode-dual-model-pipeline/SKILL.md`
- `skills/limcode-dual-model-pipeline/references/{blacklist.md,blacklist-cases.md}`
- `README.md`（补充 Limcode 宿主目录结构与 Junction/Symlink 部署说明）
- `LIMCODE_DEPLOYMENT.md`

### 方案 B：在 `limcode-test` 插件仓库内置默认技能或收紧只读边界
1. **内置扩展级默认技能根（`backend/capabilities/skillCatalog.ts`）**：
   - 当前 `roots()` 扫描 `workspaceFolder`、`user`（`~/.agents`、`~/.claude`、`~/.codex`、`~/.copilot`）与 `global`（`<dataRoot>/skills`）。
   - 若希望开箱即用，可将 `认知一致性`、`stop-that-shit`、`limcode-dual-model-pipeline` 放入扩展仓库 `assets/skills/`，并在 `skillCatalog.ts` 的 `roots()` 末尾追加 `vscode.Uri.joinPath(context.extensionUri, 'assets', 'skills')` 作为兜底内置技能源（同名的用户/全局技能因优先级更高仍可无感覆盖）。
2. **`explore` / `reviewer` 的 `shell` 只读约束与提示词对齐（`backend/world/modules/agent/blueprints.ts`）**：
   - 当前 `READONLY_TOOLS` 已剔除 `edit`、`write`、`delete`、`transfer`，但仍保留了 `'shell'` 与 `'bash'` 以供运行 `git diff`、`pytest`、`symbol_locator` 等取证命令。
   - 可在 `blueprints.ts` 的 `reviewer` 默认 `systemPrompt` 中直接写明：“严禁通过 shell/bash 修改任何工作区或测试文件；仅作只读代码审查与命令取证”。