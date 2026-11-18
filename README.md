# Vibe Coding 反防御性编程（Anti-Over-Defense & Referee Pipeline Kit）

> **告别 AI 堆屎山、下游乱兜底与重构半拉子工程**：以 **「立 · 桥 · 破 · 治」四柱体系**（《认知一致性》+ 保证交付定律 & 四步收缩律 + 4 条第一性原理 / 15 大母题 / 208 项实战黑名单 + 零污染异构对抗铁面裁判流水线）约束大模型，配合 **两段式隐式协议死代码求解器、调用图与反模式棘轮工具集**，让 Vibe Coding 写出真正源头统一、零冗余兜底的生产级代码。

---

## 一、核心方法论：立 · 桥 · 破 · 治

| 支柱 | 核心要义 | 解决什么根源病灶 |
| :--- | :--- | :--- |
| **1. 立：认知一致性** | 同一种事情只有一条路；把复杂度花在源头合一，不在下游观察、同步或打补丁补偿 | 平行入口口径漂移、双轨布尔开关、在旧结构外叠包装层 |
| **2. 桥：保证交付与四步收缩** | **保证交付定律**：准入即定型（边界返回不可变定型结构而非 `bool`/裸 `dict`）、持久约束落表结构 `CHECK` 与唯一读取原语、写原语当场交出新生成值（消灭写后回读）、明确只读共享/改写点复制/出口隔离一次的所有权。<br>**重构四步收缩律**：① 立共同机制 → ② 迁全链调用方（含跨语言）→ ③ 归零清创（清旧引用/死测试/私有名/旧原语守卫）→ ④ 单向收紧棘轮 | 下游因拿不到上游结构字据而满屏 `.get()`/判空/深拷贝；重构“新路通了就收工”带着死肉搬家 |
| **3. 破：4 问 × 15 母题 × 208 案例** | 存在性、单一事实源、信任边界、语义保真 4 条第一性原理拷问 + A～O 15 个通用代码形态母题 + 208 项脱敏实战战例 | 死分支/伪可选、搬家式吞错、靠假测试续命、先算后扔、乒乓转型、精度与状态生命周期走样 |
| **4. 治：异构裁判与防门禁异化** | **零污染铁面裁判**：主代理 0 动刀改代码、分片工人阅后即焚（`kill` 清场）、异构模型每次开无上下文新会话只读当反方与严审、上下文压缩强制重读全文门禁。<br>**防门禁异化与终轮逐条结案**：先抽样算真垃圾率（禁拿原始信号总数定 KPI）；堵死函数内延迟 `import` 躲环等逃逸通道；终轮小问题逐条判“修 / 推迟（必写重启条件）/ 驳回（必写源码证据）”，严禁整包放行 | 主代理自己写代码护短、上下文锚定顺杆爬、压缩失忆、模型为过门禁搞症状搬家、终轮小问题攒成积压债 |

---

## 二、目录结构

```text
vibe-coding-anti-defensive-programming/
├── README.md                                  # 本说明文档
├── 反过度防御黑名单全集.md                      # 单文件完整版（四柱总纲 + 4×15母题主表 + 208项实战案例全集）
├── skills/                                    # 核心工作流与方法论 Skills
│   ├── agy-dual-model-pipeline/               # 反重力双模型集群流水线（主代理0动刀裁判 + 原生Swarm分阶段工人 + Codex只读反方/严审）
│   │   ├── SKILL.md                           # 分阶段清场与铁面裁判流水线主指令
│   │   ├── references/
│   │   │   ├── blacklist.md                   # 4条第一性原理拷问 + 保证交付提醒 + A～O 15个母题主表（派发必传）
│   │   │   └── blacklist-cases.md             # 208项反过度防御与冗余实战案例库（一条一事，按编号/母题按需检索）
│   │   └── scripts/
│   │       ├── codex_drive.py                 # Codex app-server WebSocket JSON-RPC 多车道驱动器
│   │       ├── codex_view.py                  # Codex 实时终端观看器（codex watch [lane]）
│   │       ├── compaction_skill_gate.py       # 上下文压缩（CHECKPOINT）后强制全文重读规范钩子门禁
│   │       ├── claude_drive.py                # 可选：Claude Code tmux + hooks 多车道驱动器
│   │       └── claude_view.py                 # 可选：Claude 实时终端观看器（claude watch [lane]）
│   ├── omp-dual-model-pipeline/               # OMP 宿主双模型反转流水线（Gemini 分片编码 ↔ 原生 Codex 流式严审）
│   │   ├── SKILL.md
│   │   └── references/
│   │       ├── blacklist.md
│   │       └── blacklist-cases.md
│   ├── 认知一致性/                             # 《认知一致性》核心设计原则 Skill（立 + 把保证以结构交出来 + 重构收缩）
│   │   ├── SKILL.md
│   │   ├── LICENSE
│   │   └── agents/openai.yaml
│   └── stop-that-shit/                        # 《Stop That Shit》执行边界、防测试膨胀与无用哈希约束 Skill
│       └── SKILL.md
└── tools/                                     # 配套的反过度防御扫描、两段式死代码求解、循环依赖与调用链定位工具集
    ├── __init__.py
    ├── repo_paths.py                          # 仓库根目录解析与路径边界校验基础件
    ├── path_roles.py                          # 生产（production）/ 测试（test）/ 工具（tool）统一路径角色定义
    ├── boundary_baseline.py                   # 棘轮基线对比与受控刷新公共库（只减不增）
    ├── scan_over_defense.py                   # 1. 高精度反过度防御棘轮门禁扫描器（6大高精度信号 + 基线棘轮）
    ├── scan_over_defense_deep.py              # 2. 全量 15 母题深度探针扫描器（覆盖 20+ 种 Python AST / JS 反模式信号）
    ├── scan_dead_code_islands.py              # 3. 死代码孤岛防回潮扫描器（区分 suspect_dead 与 test_only）
    ├── dead_code_usage/                       # 两段式隐式协议绑定（MRO/super/运算符/框架回调/跨过程实参传播）与 SCIP 引用引擎
    ├── scan_constant_arguments.py             # 4. 恒定实参、伪可选参数与恒空模块常量只读审计扫描器
    ├── scan_import_cycles.py                  # 5. 循环依赖棘轮扫描器（同时监管顶层硬环与函数体内延迟 import 环）
    ├── import_cycle_*.py                      # 循环依赖图提取、SCC 强连通分量拆解与基线核验模块
    ├── scan_private_imports.py                # 6. 跨模块单下划线私有符号导入与 __all__ 导出棘轮扫描器
    ├── scan_dead_path_literals.py             # 7. 失效仓库路径字面量棘轮扫描器（防删文件后残留死路径字符串）
    ├── symbol_locator/                        # 8. 函数定义与上下游调用链定位器（whereis / callers / callees）
    └── callgraph/                             # 9. 函数级静态调用图（light AST 档 + deep SCIP/Runtime 融合档）
```

---

## 三、快速安装与接入

### 1. 安装 Skills
根据你使用的宿主环境，将 `skills/` 下的目录复制到对应配置路径：

- **Antigravity (AGY) 宿主**：
  ```bash
  mkdir -p ~/.gemini/config/skills ~/.codex/skills
  cp -r skills/agy-dual-model-pipeline ~/.gemini/config/skills/
  cp -r skills/认知一致性 skills/stop-that-shit ~/.codex/skills/
  ```
- **挂载上下文压缩防失忆门禁（可选，推荐用于 Antigravity `~/.gemini/config/hooks.json`）**：
  在 `PreInvocation`、`PreToolUse`、`Stop` 三个生命周期挂载 `python3 ~/.gemini/config/skills/agy-dual-model-pipeline/scripts/compaction_skill_gate.py <Event>`，发生 `CHECKPOINT` 压缩后强制代理完整重读已激活的 `SKILL.md`、《认知一致性》与 `blacklist.md`。
- **OMP / Codex 宿主**：
  ```bash
  mkdir -p ~/.agents/skills ~/.codex/skills
  cp -r skills/omp-dual-model-pipeline ~/.agents/skills/
  cp -r skills/认知一致性 skills/stop-that-shit ~/.codex/skills/
  ```

### 2. 配置终端实时观看别名（可选，用于 `agy-dual-model-pipeline`）
在 `~/.zshrc` 或 `~/.bashrc` 中添加辅助函数，即可在终端通过 `codex watch [lane]` 实时观看各车道子代理的思考与执行过程：
```bash
codex() {
  if [ "$1" = "watch" ]; then
    shift
    python3 ~/.gemini/config/skills/agy-dual-model-pipeline/scripts/codex_view.py "$@"
  else
    command codex "$@"
  fi
}
```

### 3. 接入 `tools/` 扫描与定位工具集
将 `tools/` 目录复制到目标 Python 仓库根目录下，按项目实际目录微调 `tools/path_roles.py` 中的 `_ROLE_ROOTS`（默认已涵盖 `src`, `core`, `data`, `web`, `app`, `plugins`, `desktop`, `tools`, `scripts`, `tests`）。

---

## 四、配套工具使用指南

### 1. 高精度反过度防御棘轮门禁（`scan_over_defense.py`）与全量深度探针（`scan_over_defense_deep.py`）
```bash
# 6 大低误报核心信号（内部字面量反射、参数进门即覆盖、0引用函数、仅测试引用函数、外部塞私有属性、前端死守卫）
python3 -m tools.scan_over_defense --fail-on-new
python3 -m tools.scan_over_defense --refresh

# 20+ 类 Python AST / JS 反模式信号深度普查报告
python3 tools/scan_over_defense_deep.py --root . --out /tmp/over_defense_report
```

### 2. 两段式隐式协议死代码扫描器（`scan_dead_code_islands.py`）与恒定实参审计（`scan_constant_arguments.py`）
`tools/dead_code_usage/` 对 MRO 派发、`super()`、dataclass 初始化链、运算符与容器协议成员、外部库回调与跨函数实参传播做两段式不动点求解，将候选精确拆分为 `suspect_dead`（零引用）与 `test_only`（仅测试引用）：
```bash
# 快速 AST 模式（门禁阻断新增 suspect_dead / test_only）
python3 -m tools.scan_dead_code_islands --mode quick --fail-on-new

# SCIP 精确模式（仅出报告，要求干净工作区与当前 HEAD 的 index.scip）
python3 -m tools.scan_dead_code_islands --mode precise

# 扫描全仓恒传同一常量、默认值从未生效的伪可选参数与恒空常量（只读审计输入）
python3 -m tools.scan_constant_arguments
```

### 3. 循环依赖（含函数体内延迟 `import` 环）、私有导入与失效路径棘轮
```bash
# 同时检查顶层硬加载环、Python 父包初始化文件环与函数体内延迟导入环（堵死“挪进函数体躲门禁”逃逸通道）
python3 -m tools.scan_import_cycles
python3 -m tools.scan_import_cycles --include-tests

# 阻断跨模块导入单下划线私有符号与 __all__ 导出私有名
python3 -m tools.scan_private_imports --fail-on-new

# 阻断删文件后残留在源码或测试中的失效仓库路径字面量
python3 -m tools.scan_dead_path_literals --fail-on-new
```

### 4. 符号定义、调用链影响面定位器与两档调用图（`symbol_locator` + `callgraph`）
```bash
# 秒级查定义位置、谁调用了它（callers）、它调用了谁（callees）
python3 -m tools.symbol_locator whereis <func_name>
python3 -m tools.symbol_locator callers <func_name>
python3 -m tools.symbol_locator callees <func_name>

# 两档静态调用图提取（light 零依赖 AST 消歧档 / deep 融合 SCIP + sys.setprofile 运行时边档）
python3 tools/callgraph/callgraph_extract.py --mode light
python3 tools/callgraph/runtime_callgraph_collect.py tests/
python3 tools/callgraph/callgraph_extract.py --mode deep
python3 tools/callgraph/callgraph_crossval.py
```
