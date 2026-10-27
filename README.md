# Vibe Coding 反防御性编程（Anti-Over-Defense & Multi-Model Pipeline Kit）

> **告别 AI 堆屎山与防御性垃圾代码**：用 **4 条第一性原理 + 15 大母题 + 140 项实战黑名单** 约束大模型，配合 **三模型多车道制衡流水线（Gemini 广撒网/首稿 ↔ Claude 核验/返修 ↔ Codex 反方/严审）** 与 **AST / 调用图静态猎杀工具集**，让 Vibe Coding 写出真正源头统一、零冗余兜底的生产级代码。

---

## 一、目录结构

```text
vibe-coding-anti-defensive-programming/
├── README.md                                  # 本说明文档
├── 反过度防御140项黑名单全集.md                   # 单文件完整版（4条第一性原理 + 15母题总结 + 140项实战案例全集）
├── skills/                                    # 核心工作流与方法论 Skills
│   ├── agy-dual-model-pipeline/               # 反重力三模型流水线（Gemini 广撒网/首稿 + Claude 核验/返修 + Codex 反方/严审 + 主代理裁决）
│   │   ├── SKILL.md                           # 三模型多车道并行流水线主指令
│   │   ├── references/
│   │   │   ├── blacklist.md                   # 4条第一性原理拷问 + 4×15全景速览 + A～O 15个母题主表（派发子代理必传）
│   │   │   └── blacklist-cases.md             # 140项反过度防御与冗余实战案例库（按编号按需检索）
│   │   └── scripts/
│   │       ├── codex_drive.py                 # Codex app-server WebSocket JSON-RPC 多车道驱动器
│   │       ├── codex_view.py                  # Codex 实时终端观看器（codex watch [lane]）
│   │       ├── claude_drive.py                # Claude Code tmux + hooks 多车道驱动器
│   │       └── claude_view.py                 # Claude 实时终端观看器（claude watch [lane]）
│   ├── omp-dual-model-pipeline/               # OMP 宿主双模型反转流水线（Gemini 4 Argon 分片编码 ↔ 原生 Codex 流式严审）
│   │   ├── SKILL.md
│   │   └── references/
│   │       ├── blacklist.md
│   │       └── blacklist-cases.md
│   ├── 认知一致性/                             # 《认知一致性》核心设计原则 Skill（立：源头统一，同一件事只有一条路）
│   │   ├── SKILL.md
│   │   ├── LICENSE
│   │   └── agents/openai.yaml
│   └── stop-that-shit/                        # 《Stop That Shit》执行边界、防测试膨胀与无用哈希约束 Skill
│       └── SKILL.md
└── tools/                                     # 配套的反过度防御扫描、死代码检测与调用链定位工具集
    ├── __init__.py
    ├── scan_over_defense.py                   # 1. 高精度反过度防御棘轮门禁扫描器（6大高精度信号 + 基线棘轮）
    ├── scan_over_defense_deep.py              # 2. 全量 15 母题深度探针扫描器（覆盖 20+ 种 Python AST / JS 反模式信号）
    ├── boundary_baseline.py                   # 3. 棘轮基线对比与受控刷新公共库（只减不增）
    ├── scan_private_imports.py                # 4. 跨模块私有符号导入棘轮扫描器
    ├── scan_dead_path_literals.py             # 5. 失效路径字面量棘轮扫描器（防删文件后残留死路径字符串）
    ├── scan_dead_code_islands.py              # 6. 0 调用死代码孤岛防回潮扫描器（AST 快模 + SCIP 精模）
    ├── dead_code_usage/                       # 死代码孤岛 AST / SCIP 引用分析引擎
    ├── symbol_locator/                        # 7. 函数定义与上下游调用链定位器（whereis / callers / callees）
    └── callgraph/                             # 8. 函数级静态调用图与风险数据流提取引擎
```

---

## 二、快速安装与接入

### 1. 安装 Skills
根据你使用的宿主环境，将 `skills/` 下的目录复制到对应配置路径：

- **Antigravity (AGY) 宿主**：
  ```bash
  mkdir -p ~/.gemini/config/skills ~/.codex/skills
  cp -r skills/agy-dual-model-pipeline ~/.gemini/config/skills/
  cp -r skills/认知一致性 skills/stop-that-shit ~/.codex/skills/
  ```
- **OMP / Codex 宿主**：
  ```bash
  mkdir -p ~/.agents/skills ~/.codex/skills
  cp -r skills/omp-dual-model-pipeline ~/.agents/skills/
  cp -r skills/认知一致性 skills/stop-that-shit ~/.codex/skills/
  ```

### 2. 配置终端实时观看别名（可选，用于 `agy-dual-model-pipeline`）
在 `~/.zshrc` 或 `~/.bashrc` 中添加辅助函数，即可在终端通过 `codex watch [lane]` / `claude watch [lane]` 实时观看各车道子代理的思考与执行过程：
```bash
codex() {
  if [ "$1" = "watch" ]; then
    shift
    python3 ~/.gemini/config/skills/agy-dual-model-pipeline/scripts/codex_view.py "$@"
  else
    command codex "$@"
  fi
}

claude() {
  if [ "$1" = "watch" ]; then
    shift
    python3 ~/.gemini/config/skills/agy-dual-model-pipeline/scripts/claude_view.py "$@"
  else
    command claude "$@"
  fi
}
```

### 3. 接入 `tools/` 扫描与定位工具集
将 `tools/` 目录复制到目标 Python 仓库根目录下（或放入 `PYTHONPATH`），并根据目标仓库的实际顶层源码目录调整 `SCAN_ROOTS` / `PROD_ROOTS`（默认已涵盖 `src`, `core`, `web`, `data`, `app`, `plugins`, `desktop` 等常见目录）。

---

## 三、配套工具使用指南

### 1. 全量 15 母题深度探针扫描器（`scan_over_defense_deep.py`）
对照 140 项黑名单中的可机械识别信号，一次性扫描仓库中的 20+ 类过度防御与冗余坏味道（反射探测、宽泛 `except`、`or` 空字面量、深拷贝/往返转换、裸抛缓存异常、`_ = unused`、外部塞属性、参数进门被覆盖、未使用参数、单行透传包装、对非 Optional 参数判 `None`、默认值兜底后假必填校验、事务内做文件 I/O、0 引用函数、仅测试引用函数、伪可选恒传参、恒常量开关、纯重导出空壳模块、防御性注释与前端 JS/JSX 反模式），输出结构化 JSON 供审查：
```bash
python3 tools/scan_over_defense_deep.py --root . --out /tmp/over_defense_report
```

### 2. 高精度反过度防御棘轮门禁（`scan_over_defense.py`）
针对误报率极低的 6 类核心反模式信号（内部字面量反射、参数进门即覆盖、0 引用函数、仅测试引用函数、往外部对象塞私有属性、前端死守卫）建立**只减不增**的棘轮基线（默认存于 `.checkup/over_defense_baseline.json`）：
```bash
# 查看当前扫描统计
python3 -m tools.scan_over_defense

# 首次建立或清理债务后收紧基线（默认拒绝总数增长）
python3 -m tools.scan_over_defense --refresh

# 在 CI / pre-commit / 门禁中阻断任何新增过度防御
python3 -m tools.scan_over_defense --fail-on-new
```

### 3. 符号定义与调用链影响面定位器（`tools.symbol_locator`）
在修改函数签名、删除疑似死代码或排查跨层调用链前，秒级查询全仓定义位置、上游调用者（Callers）与下游被调函数（Callees）：
```bash
# 查定义位置（支持 --at FILE:LINE 结合 jedi 精确消歧）
python3 -m tools.symbol_locator whereis <func_name>

# 查谁调用了它（评估改动影响面 / 确认是否 0 生产调用）
python3 -m tools.symbol_locator callers <func_name>

# 查它调用了谁
python3 -m tools.symbol_locator callees <func_name>
```
*(注：首次运行或源码变更时会自动调用 `tools/callgraph/callgraph_extract.py` 重建静态调用图快照，需 Python 环境安装 `networkx`；`--at` 消歧可选依赖 `jedi`。)*

### 4. 运行时动态调用图采集与静态×动态交叉对账（`runtime_callgraph_collect.py` & `callgraph_crossval.py`）
由于 Python 动态特性（鸭子类型、高阶回调、动态注册等），纯静态 AST 调用图无法 100% 消解所有多态调用。本套件内置了基于 `sys.setprofile` / `threading.setprofile` 的**运行时动态调用边录像器**与**静态×动态对账器**：
```bash
# 1) 先提取静态调用图（含确信边 confident 与模糊边 ambiguous）
python3 tools/callgraph/callgraph_extract.py

# 2) 在运行 pytest 时挂载 sys.setprofile 录制真实触发的 caller -> callee 边
python3 tools/callgraph/runtime_callgraph_collect.py tests/

# 3) 静态图 × 运行时边交叉对账（将运行时跑到的模糊边升级为实线、揪出静态漏掉的动态派发、过滤假孤岛）
python3 tools/callgraph/callgraph_crossval.py
```

### 5. 死代码孤岛、跨模块私有导入与失效路径棘轮扫描
```bash
# 扫描 0 调用死代码孤岛（AST 快速模式 / SCIP 精确模式）
python3 -m tools.scan_dead_code_islands --mode quick

# 扫描跨模块导入单下划线私有符号（from mod import _private）
python3 -m tools.scan_private_imports --fail-on-new

# 扫描删文件后残留在源码或测试中的失效路径字符串
python3 -m tools.scan_dead_path_literals --fail-on-new
```
