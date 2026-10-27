# 反过度防御黑名单案例库

> 这里收录已抓获的具体案例，是主表 [blacklist.md](blacklist.md) 的附录。派发任务时传主表；案例库不必通读，对某个母题拿不准时，按主表列出的编号来查。
> - 编号永久不变，新案例在末尾顺延，旧报告和 SKILL.md 里的 `#27`、`#44` 这类引用始终有效；`#27.1` 表示第 27 条第 1 点。
> - 每条标题下的“母题”行标注它归属的主表母题，排在第一个的是主归属。
> - 新案例按主表“四、入库规则”追加：先归类，一条只写一种花样。

### 1. 严禁对已通过边界/Contract 校验的受控结构重复判空与二次类型检查
> 母题：F 重复校验

- **典型坏习惯**：
  - 在已通过 `OrderContract.item` / `ExecutionContract.detail` 边界校验的模型层继续写 `!item || !item.detail || !Array.isArray(detail.warnings)`；
  - 对内部固定字典必有键（如 `context["selected_id"]`、`row["source"]`）写 `context.get("selected_id")`、`row.get("source")`；
  - 在外层 JSX 已经守卫 `{current && ...}` 或状态已保证 `command.result` 非空的情况下，内层继续写 `Boolean(current && ...)`、`done && command.result`；
  - 对查询 Hook 已经保证互斥的状态重复写 `!request.loading && !request.error && result`（`result` 非空时前两者必成立）。
- **正确做法**：边界校验只做一次，内部直接索引必有键；若某个测试夹具漏写了契约必有字段（如 `warnings: []`），**去补齐测试夹具**，绝不在生产代码里加 `Array.isArray` 迁就残缺测试夹具。**注意区分“全形态必有键”与“按类型区分的多态/联合体字典”**：将 `.get(k)` 改为 `[k]` 前，必须核对所有生产调用方的字典结构——若该字典按判别字段（如 `kind == "local" | "remote"`）拥有不同键集（如本地任务含 `cpu_shares`/`memory_mb`，远程任务仅含 `timeout_days`），必须先用权威判别字段（如 `if before["kind"] != "local":`）短路排除其他分支后，再直接索引分支专属必有键 `after["memory_mb"]`，绝不能既不判别分支又盲目强读分支专属键。

### 2. 严禁捏造永不触发的死分支与无脑兜底 Fallback
> 母题：A 死分支、死参数、伪可选 · D 靠测试续命

- **典型坏习惯**：
  - 后端恒返回非空字符串（如 `summary.headline`），前端却顺手写 `summary.headline || '当前没有可用的汇总结果...'`，甚至在测试里专门把 `headline` 改成 `''` 去测这个死分支；
  - 在已校验非空的实体字典推导中写 `entities[ref]["code"] if ref is not None else None`；
  - 给全仓所有调用点都必传的参数加 `context_facts=None` 默认值和 `if context_facts is not None else None` 死分支；
  - 未核实真实字段名就写 `elif kind == "worker" and key == "group_code":` 死分支，还写假测试直接传 `"group_code"` 自证通过。
- **正确做法**：彻底删除一切生产路径不可达的 `|| ''`、`or {}`、`=None` 兜底与死分支，严禁为死分支编写自嗨测试。

### 3. 严禁无脑防御性拷贝
> 母题：M 无用拷贝与往返转换

- **典型坏习惯**：
  - `group.items.concat(group.archivedItems)` 已经返回全新数组，传入 `buildTracks(segments)` 后第一行又写 `segments.slice().sort(...)`；
  - 下游 `RequestContract.input` 已经做 `.slice().sort()`，上游调用传参又写 `item_ids: selected.slice()`，还在测试里写 `assert.notStrictEqual` 锁定这次多余拷贝；
  - 合并对象时先 `const base = { ...savedInput }; delete base.item_ids; return { ...base, ...initialContext };` 连续分配两个临时对象。
- **正确做法**：不原地修改入参的受控链路严禁加 `.slice()`、`list()`、`dict()`、`deepcopy()`。

### 4. 严禁自造无独立消费者的包装函数与同义反复校验
> 母题：B 多余的层 · F 重复校验

- **典型坏习惯**：
  - 生产中只有 `lookupStatus(intent, signal)` 一个调用点，却额外拆出 `queryCommandStatus(targetId, requestKey, intent, signal)`，从 `intent` 拆出 `intent.target_id` 和 `intent.request_key` 传进去，再在第一行写 `check(targetId === intent.target_id && requestKey === intent.request_key)` 自己跟自己比对。
- **正确做法**：没有第二个独立生产调用者绝不拆包装层；绝不把同一对象的属性拆出来再跟原对象做同义反复校验。

### 5. 严禁“先用 `set()` 去重掩盖异常，再另起炉灶二次遍历查重”
> 母题：G 重复计算

- **典型坏习惯**：
  - 时间分段列表已按起止时间排好序，却在 `_build_intervals` 末尾写 `tuple(sorted(set(intervals)))` 既重复排序又抹掉重复区间，随后为了查重复区间又在另一个函数里重新遍历构造 `periods = [(start, end) ...]` 检查 `len(set(periods)) != len(periods)`。
- **正确做法**：直接返回有序 `tuple(intervals)`，让已有的相邻性检查（`left[1] != right[0]`）单遍自然拦截重复与不连续区间。

### 6. 严禁构造被所有调用方立即丢弃的返回值，或保留被丢弃返回值的重型调用
> 母题：E 先算后扔

- **典型坏习惯**：
  - `extract_segments(record)` 在非多段时构造单元素元组 `((start, end, resource_id, owner_id),)`，而全部 3 个调用方都写 `if len(segments) <= 1:` 将其丢弃并用当前行覆盖；
  - 在提交处理函数 `_commit_payload` 中对上游已验证过的 `ValidatedTaskPayload` 再次调用 `build_validated_task_payload(...)` 且直接丢弃返回值。
- **正确做法**：无多段覆盖时直接返回 `()`，调用方判断 `if not segments:`；上游已构建验证过的 DTO 绝不在下游二次重建。

### 7. 严禁在异常保护边界外或数据库写锁内塞入文件 I/O 与无消费者字段
> 母题：N 事务、锁与状态生命周期

- **典型坏习惯**：
  - 在 `preview()` 的 `try ... except` 块之前无条件调用 `self.context.read_ref()` 读磁盘状态文件（导致目标记录 404 时白做磁盘 I/O，且文件损坏异常逃逸出 `storage_failure` 边界）；
  - 在 `commit()` 的事务守卫（持有应用级互斥锁与 SQLite `BEGIN IMMEDIATE` 写锁）中读取磁盘状态文件并把 `snapshot_ref` 塞进提交回执，而前端早已在 `preview` 阶段保存了该值、根本不消费回执里的该字段。
- **正确做法**：将 I/O 放在前置校验通过之后且纳入统一的异常转换边界；写事务锁内绝不执行无业务消费者的外部文件读取。

### 8. 严禁同一作用域重复扫描/重复建索引或跨文件复制粘贴同语义逻辑
> 母题：G 重复计算 · J 口径没在源头统一

- **典型坏习惯**：
  - 同一行 JSX 渲染内重复写两次 `items.some(item => item.legacy)` 线性扫描和三次 `isEmptyRow(row, view.mode)`；
  - `React.useMemo` 的工厂函数与依赖数组重复手写同一段长三元表达式；
  - 在异步回调的各个分支重复调用入口已同步执行过的 `setBannerInfo(null)`；
  - 在上游数据采集模块已经构建过全量 `TaskLookupIndex` 的情况下，下游投影分支不复用该索引，反而对全量数据再跑一遍过滤并重建第二个 `TaskLookupIndex`；
  - 在 `DraftViewModel.js` 与 `TimelineViewModel.js` 中逐字复制粘贴同一套实体解析循环。
- **正确做法**：同一渲染/函数作用域提取局部变量计算一次；同请求同时间窗复用已构建的索引实例；跨文件同语义纯函数提取到单一模型模块导出共用。

### 9. 严禁为纯文档/说明/配置文案修改强行编造字面量测试或重复渲染未改动组件
> 母题：D 靠测试续命

- **典型坏习惯**：
  - 仅修改了 `docs/user_manual.md` 等说明文档，却在测试里加 `fs.readFileSync` + `.includes(...)` 死锁 Markdown 原文字面量；
  - 本次根本未改动某前端组件（且测试上一行已渲染过该组件），却把同一组件再 `render` 第二遍去断言静态字面文案。
- **正确做法**：遵守“不强行为配置/说明修改编造测试”与“不测源码/文案字面量”原则，纯说明文档改动只跑文档格式与结构一致性检查，绝不编造源码/文档字面匹配测试。

### 10. 严禁对自有类做反射式防御与动态猴子补丁（`getattr` / `hasattr` / `setattr` 套路）
> 母题：F 重复校验

- **典型坏习惯**：
  - 对本仓库自己定义的类（如 `HistoryReader`、`PreparedRequest`、`SnapshotCapture`）写 `getattr(capture, "execution_service", None)`、`getattr(snapshot, "config_error", None)`；
  - 不在 `__init__` 中显式声明缓存或状态属性，而是在业务函数里写 `if not hasattr(reader, "metrics_cache"): reader.metrics_cache = {}`。
- **正确做法**：自有类的属性必须在 `__init__` 或 dataclass 中显式初始化并直接通过 `obj.attr` 访问，严禁对内部受控对象使用 `getattr(..., default)` 或 `hasattr` 动态补丁。

### 11. 严禁序列化 / 格式化 / 集合类型“乒乓往返”（Round-Trip Conversion 套路）
> 母题：M 无用拷贝与往返转换

- **典型坏习惯**：
  - 时间对象刚由 `parse_dt` 解析为 `datetime`，立刻 `.isoformat()` 转成字符串返回，紧接着下一层算分钟差又调用 `datetime.fromisoformat(...)` 重新解析；
  - 上游已 `json.loads` 解析出 `audit` 字典，传给下游却把原始 JSON 字符串再传一遍，导致下游二次反序列化；
  - 集合与列表来回折腾：`Array.from(new Set(selected.concat(Array.from(new Set(...)))))` 或 `set(list(set(...)))`。
- **正确做法**：解析/反序列化在入口只做一次，内部调用链直接传递强类型对象（`datetime`、已解析 `dict`、`set`），仅在最终序列化/展示边界转换一次。

### 12. 严禁留存兼容重导出空壳文件与单行同义桥接函数（Zombie Re-Export / Adapter 套路）
> 母题：B 多余的层

- **典型坏习惯**：
  - 抽离或移动函数后，生怕漏改调用点，在原位置留下 5～15 行的纯重导出模块（如 `services/metrics.py`、`domain/lineage.py`）或单行同义别名函数（`def has_table(conn, name): return table_exists(conn, name)`）。
- **正确做法**：坚持 Clean Cutover——用全局符号检索工具或 `rg` 找齐全部生产与测试调用点一次性迁移，立即删除重导出空壳文件与透传别名。

### 13. 严禁“刚构造完就原地自检/自查找”的被害妄想式校验（Self-Check After Construction 套路）
> 母题：F 重复校验

- **典型坏习惯**：
  - 上一行刚刚按规则筛选出全部 `eligible=True` 的 `samples` 并将 `len(selected)` 写入 `suggestion["sample_count"]`，下一行立刻写 `if len(samples) != suggestion["sample_count"] or any(not row["eligible"] for row in samples): raise ...` 怀疑自己上一秒算出的结果；
  - 刚刚用 `side["tasks"]` 建了 `lookup = {row["row_ref"]: row for row in side["tasks"]}`，紧接着遍历 `for task in side["tasks"]: original = lookup[task["row_ref"]]` 拿自己查自己；
  - 自己刚拼出的合法字符串立刻放进 `try: int(s) except ValueError:` 自抛自接。
- **正确做法**：信任当前进程同一调用链刚刚确定性构造出的局部不变量，严禁写自证清白的事后自检。

### 14. 严禁循环内嵌不变量计算与“先全量重型构造再按分支丢弃”（Loop-Invariant & Eager-Discard 套路）
> 母题：E 先算后扔 · G 重复计算

- **典型坏习惯**：
  - 在遍历列表的循环内部反复计算与循环变量无关的值：每行调用 `as_of.isoformat(timespec="seconds")`、每单元格推导翻转字典 `{label: code for code, label in ENUM_LABELS[field].items()}`、每行执行 `sorted(uncertain)`、每个指标重复遍历过滤同一组状态集合；
  - 在 `if fmt == "csv":` 之前先把只有 XLSX 才需要的全部多表数据算出来，或在功能开关关闭 / 空搜索词时先花 $O(N)$ 构造完列表再在下一层入口直接 `return []` 丢弃。
- **正确做法**：循环不变量一律提到循环外或模块级常量；条件分支（如导出格式、开关未启用、空搜索词）必须在重型遍历构造**之前**短路。

### 15. 严禁同一事务/快照内“先 Probe 再 Fetch”或拆表散查同一行（Two-Step DB Probe 套路）
> 母题：G 重复计算

- **典型坏习惯**：
  - 先调 `find_by_request_key(request_key)` 查主表拿 `job_id`，下一行再调 `get_status(row["job_id"])` 去 `LEFT JOIN` 查同一条记录的状态；
  - 在 `claim_pending_job` 同一写事务里，刚算完 `_check_quota(conn, row)` 和 `require_quota(repo, row)`，紧接着在校验函数里把这两个查库函数原封不动再跑一遍；
  - 在同一读快照内对同一张表反复执行 `table_exists` / `require_schema`。
- **正确做法**：同一事务内对同一实体的状态探测与数据获取合并为单次 SQL 联表查询，或直接复用首次读取结果。

### 16. 严禁宽泛 `try / except` 吞掉编程错误或层层包装同义异常（Broad Catch & Re-Raise 套路）
> 母题：K 吞错与静默降级

- **典型坏习惯**：
  - 在内部受控计算外层套 `except (AppError, ValueError, TypeError, KeyError, OverflowError):` 把拼写错误或契约破坏导致的 `KeyError`/`TypeError` 混同为业务异常吞掉；
  - 内层捕获异常包装成自定义异常抛出，紧邻的唯一外层调用点又立刻捕获该自定义异常转成默认值。
- **正确做法**：`try / except` 只包裹真正解析外部输入/存储字节的最小单语句，且只捕获精确异常类型；绝不捕获内部固定结构的 `KeyError`/`TypeError` 掩盖代码缺陷。

### 17. 严禁保留恒定布尔开关、零消费死参数与“只写不读”的僵尸字段（Dead Params & Zombie Fields 套路）
> 母题：A 死分支、死参数、伪可选

- **典型坏习惯**：
  - 函数参数列表留着全仓所有调用点都传同一常量（如 `check_readiness(item, True)`、`build_summary(..., details=True)`、`all_rows=False`）的伪通用开关；
  - 签名里挂着函数体根本不读（或只透传给下层但最终无人消费）的 `conn`、`reader`、`now`、`context`、`projections` 参数；
  - 往对象或字典里塞全仓没有任何地方读取的字段（如 `self.audit_log`、`self.input_sources`、`captured[ref]["header"]`、`prepared.extra_metadata`）。
- **正确做法**：一并清理零消费参数、恒定开关与只写不读的僵尸属性，保持函数签名与数据结构最小化。

### 18. 严禁“上一行用默认值兜底，下一行假装做非空必填校验”（默认值架空必填校验套路）
> 母题：K 吞错与静默降级 · A 死分支、死参数、伪可选

- **典型坏习惯**：
  - 在时间窗解析函数中先写 `start = parse_hhmm(row.get("start_time"), allow_none=False)`（其中 `allow_none=False` 在缺失时静默返回默认字符串 `"08:00"`），紧接着下一行写 `if start is None or end is None: raise ValidationError("缺少开始或结束时间。")`——表面上写了严格报错，实际上被上一行的静默默认值完全架空，变成 100% 永不可达的摆设；
  - 先写 `base_time = start_dt or datetime.now()`，隔两行又写 `if base_time is None: return datetime.now()`。
- **正确做法**：必填校验必须直面原始未兜底输入（或显式传 `allow_none=True` 返回 `None` 后抛错），绝不允许先塞默认值再写永远进不去的 `if x is None: raise ...`。

### 19. 严禁“仪式感记账（构造 Collector 收集完直接扔掉）”与“防空洞外再修防空洞”
> 母题：A 死分支、死参数、伪可选 · K 吞错与静默降级

- **典型坏习惯**：
  - 在聚合分支专门 `collector = WarningCollector()` 传给 `parse_float(..., collector=collector)`，调用结束后根本不读 `collector`、直接随作用域销毁；
  - 明明被调函数 `parse_float(..., strict_mode=strict_mode, fallback=0.0)` 内部已经完整处理了严格模式抛错与非严格模式回退，调用方外层还是再包一层 `try ... except Exception: if strict_mode: raise; total_value = 0.0`。
- **正确做法**：不消费的收集器/状态容器一律不建（若底层接口必填，核实是否漏记或精简接口）；底层已具备明确异常/回退契约时，外层绝不再套第二层 `try / except` 防空洞。

### 20. 严禁“验钞式递归深拷贝（建完大树看一眼就扔）”与“热循环二传手闭包套娃”
> 母题：M 无用拷贝与往返转换 · B 多余的层

- **典型坏习惯**：
  - 在计算快照签名函数中先调 `build_tree_snapshot(value)` 把整棵配置树递归构造成 `frozenset`/`tuple` 树，仅用于判断 `is not UNSUPPORTED` 后当场丢弃，下一秒外层又调 `encode_tree(value)` 从头递归遍历第二遍；
  - 对字段全是不可变标量的 `TaskResult` 反复调用 `dataclasses.asdict(result)` 做全字段递归深拷贝（甚至先 `asdict` 深拷贝构造完子类再调校验函数自检）；
  - 在候选评分热循环内，已经有延迟闭包 `score_fn` 的情况下，每轮每个候选又额外定义一个零参闭包 `def cached(...): return active_cache.resolve(..., score_fn)` 赋给 `fallback`，然后在下一行立刻 `fallback()` 调完就扔。
- **正确做法**：校验与编码合并为单次遍历；标量 dataclass 浅层提取字段，不用 `asdict()` 递归深拷贝；热循环内直接调用目标方法，绝不分配一次性二传手闭包。

### 21. 严禁“缓存命中后原地回写”与“死壳引死壳的俄罗斯套娃垫片”
> 母题：H 缓存失效 · A 死分支、死参数、伪可选 · B 多余的层

- **典型坏习惯**：
  - 在计算均值函数中，刚从缓存字典 `totals_by_id` 命中读出 `cached`，走完分支后又重新调一遍 `_normalize_id(getattr(item, "id", 0))` 把刚读出来的 `float(sample)` 原样写回 `totals_by_id`；
  - 在核心调度函数中留着十几个 `=None` 的散装历史参数，还在函数末尾写一行 `_ = processed_count` 专门欺骗 Linter 掩盖死参数告警；
  - 目录搬迁后层层留重导出垫片（`engine/` 根重导出 `runtime/`、`summary/` 重导出 `contracts/`、`solvers/` 重导出 `solver_contracts/`），甚至出现“0 调用的垫片 A 导入 0 调用的死函数 B”的死壳互锁。
- **正确做法**：缓存命中分支直接返回或追加样本，绝不向缓存原地回写；严禁用 `_ = unused_param` 掩盖死参数；对跨目录重导出垫片顺着依赖链连根拔除。

### 22. 严禁模块导入期对硬编码常量表做“静态自检妄想症”，或反过来每次调用都对静态常量表做 $O(N)$ 重复遍历
> 母题：F 重复校验 · G 重复计算

- **典型坏习惯**：
  - 在表结构描述模块同一个文件里刚手写完静态描述符常量字典 `_DESCRIPTORS`，紧接着在模块顶层 `import` 时跑 80 多行 `_check_descriptors()` 校验自己上一行写死的常量键名和类型对不对；或在令牌注册表模块导入时对硬编码 `_SPECS` 跑 `dict()` 转换和正则/前缀自检；
  - 在静态规格模块里，每次调用 `get_field_spec(key)` / `iter_field_specs()` 都去 $O(N)$ 线性遍历静态元组并临时构造字典，而不是在模块加载期一次性预建 `_SPEC_BY_KEY` 索引字典。
- **正确做法**：删除模块导入期对自身硬编码常量的运行时自检代码（静态契约由单元测试或门禁保证，不在每次进程导入时自检）；对静态常量表的按键查找一律在模块加载期预建 $O(1)$ 映射字典。

### 23. 严禁 SQL-Python 跨层双重设防、仓储 `fetchall` 返回后再套 `[dict(row) ...]` 双层拷贝、或同一连接同一作用域内连发重复 PRAGMA / 元数据查询
> 母题：F 重复校验 · M 无用拷贝与往返转换 · G 重复计算

- **典型坏习惯**：
  - SQL 查询已经是 `SELECT COUNT(*) ...` 或 `SELECT COALESCE(MAX(version), 0) ...`（SQLite 聚合查询必返回 1 行非空整数），Python 层却还要写 `int((row[0] if row and row[0] is not None else 0) or 0)` 三重防御；
  - `BaseRepository.fetchall()` / `fetchone()` 内部已经将 `sqlite3.Row` 转换成了全新的 `dict` 列表/字典，仓储子类或服务层拿到后又写 `[dict(row) for row in self.fetchall(...)]` 或 `dict(self.fetchone(...))` 做双层浅拷贝；
  - 在同一个 `ensure_schema(conn)` 或迁移校验函数内，对同一张表连调 3～5 次 `PRAGMA table_info(table)`、`PRAGMA index_list(table)` 或 `SELECT sql FROM sqlite_master`，先查表在不在、再查列全不全、再查 SQL 文本对不对，而不是查一次传入复用。
- **正确做法**：信任 SQL 聚合/非空契约与 `BaseRepository` 已返回的 `dict` 新实例，严禁二次 `dict()` 套壳拷贝；同一连接同一校验周期内的 `PRAGMA` / `sqlite_master` 元数据只查一次并向下游传参复用。

### 24. 严禁“恢复/校验路径误入首次初始化分支导致静默洗白”与“半截子契约/分页断言”
> 母题：O 语义走样

- **典型坏习惯**：
  - 在外部备份恢复或完整性校验中，把 `PRAGMA integrity_check == 'ok'` 当成“备份文件有效”的充分条件（忽略了 SQLite 对 0 字节空文件或无业务表空库也返回 `'ok'`），紧接着调用 `ensure_schema(..., full_data=True)` 时又未区分“普通首次启动”与“外部恢复校验”，导致空库误入 `executescript(schema.sql)` 首次建表分支，把“空备份擦除原库数据”静默洗白成“恢复校验成功”；
  - 在接口默认返回分页切片（如 `view="summary"` 下 `response["items"] == []`、全量总数在 `response["pagination"]["totals"]["items"]`）时，直接拿当前分页列表长度 `len(response["items"])` 去断言全量业务记录数。
- **正确做法**：完整性校验必须在源头同时验证物理完整性（`PRAGMA integrity_check`）与业务表非空契约；恢复/校验模式（`full_data=True`）严禁回退到首次建库分支；分页接口的完整性断言必须对齐权威全量统计字段而非默认分页切片。

### 25. 严禁内层组件/方法已内聚守卫或事务，外层调用方再包一层同职责守卫导致“副作用翻倍”
> 母题：F 重复校验 · N 事务、锁与状态生命周期

- **典型坏习惯**：
  - 前端 `<Modal>` 组件传入 `guardOwner` 后，其内部关闭处理器已经统一调用 `confirmLeave()` 弹出未保存离开确认框，外层传入的 `onClose` 回调却又手写一遍 `if (!confirmLeave()) return;`，导致用户点一次关闭连弹两次确认框；
  - 批量更新配置或批量落库时，外层方法已经开启了事务并完成了整批快照校验，内层循环却逐字段调用单条更新入口，导致每个字段重复建仓储、重复开嵌套事务、重复跑单字段校验。
- **正确做法**：守卫（离开确认、权限/快照校验、事务边界）遵循单一职责归属，内层组件已接管守卫时外层直接传纯执行回调；批量操作在入口统一校验并在单一事务内直接调用底层写入原语。

### 26. 严禁遗留跨阶段/跨模块清理后的“次生孤岛死代码”与“仅靠孤岛测试续命的旧链路”
> 母题：C 清理不彻底

- **典型坏习惯**：
  - 删除上层死服务/死路由（如 `team_query_service.py`、`legacy_delete_guard.py`、`batch_sync_service.py`）后，未顺藤摸瓜清理底层仅服务于它们的 Model 类（如 `ResourceGroup`）、Repository 文件/方法（如 `ResourceGroupRepository`、`MemberRepository.list_with_names_by_*`）与共享 helper，导致底层留下一批 0 生产调用的“次生孤岛”；
  - 某套旧业务服务（如旧版 `LegacyTaskService` + `task_edit_service.py`）在生产代码中早已 0 调用，仅因某个历史测试文件还在 import 并自测它，就误以为它仍是活代码而保留。
- **正确做法**：每次删除上层模块或方法后，必须反向追踪其引用的全部下层 Model / Repository / Helper 引用计数，把减为 0 生产调用的次生孤岛一并连根拔除；对生产 0 调用、仅被孤岛测试自测的旧代码链路，确认其校验职责已被现行主链路覆盖后，连同孤岛测试或旧入口一起清理。

### 27. 严禁编码模型在重构清理时搞“反扑式防御”（搬家式吞错、抢跑重复校验、双轨布尔开关、基类残缺兜底、自编假测试锁死防御）
> 母题：L 删旧壳时搬家兜底（第 1、3、4 点） · F 重复校验（第 2 点） · J 口径没在源头统一（第 3 点） · D 靠测试续命（第 5 点）

- **典型坏习惯（编码模型执行修改时的 5 大惯犯套路）**：
  1. **搬家式吞错（Exception Sink Migration）**：指令要求删掉上层调用者（如 `order_processor.py`、`projection.py`、`baseline_validator.py`）的宽泛 `except (KeyError, TypeError, ValueError, OverflowError):` 时，模型不敢让内部契约错误暴露，偷偷在下层 5～7 个被调函数（如 `ArchivedPlan.__init__`、`SnapshotFacts.__init__`、`load_draft`、`load_record`）里各自新塞一套 `try ... except (KeyError, TypeError, ValueError):`，甚至在上下游叠双层捕获，把 1 处吞错裂变成 7 处吞错；
  2. **抢跑式重复预校验（Pre-Validation Before Real Validation）**：明明紧接着下一行调用的 `check_snapshot_current` 和 `prepare_run_input` 已经在职责边界完整校验了 `capture["execution"]` 和 `capture["input"]`，模型却在 `load_candidate` 里新写一个 `_decode_capture(capture)` 提前把同一份快照跑一遍全量归一化和投影校验，造成全量双重解析；
  3. **双轨布尔开关妥协（Dual-Mode Boolean Flag）**：要求源头统一数值校验（如数值字段禁止布尔值与负数）或统一业务流程（如文件导入复用模板复制逻辑）时，不敢直接统一源头语义或拆分单一职责函数，反而加一个 `strict_numbers: bool = False`、`_prevalidated: bool = False`、`facts=None` 开关，甚至当 `_prevalidated=True` 时把同次调用传入的 7 个参数 100% 丢弃，人为制造“同一种事两套口径”；
  4. **基类/属性残缺兜底（Lazy Partial Fallback）**：当子类（如 `_InventoryChecks`）或测试夹具未初始化某个必填属性（如 `self.availability`、`prepared.item_scope`）时，不去子类 `__init__` 或测试夹具补齐 1 行初始化，反而在基类/生产函数加 `Optional = None`、`if x is not None else ...`、`scope=None` 回退重算，再配 `cast(...)` 糊弄类型检查；
  5. **自编破坏性假测试锁死防御 & 改写测试为底层死代码站台续命（Self-Justifying Corruption Tests & Zombie Test Resurrection）**：
     - 一方面：偷偷塞完搬家式吞错、防御深拷贝和抢跑校验后，在 `tests/` 里专门新写手工把内部字典/JSON 改成荒谬类型（如 `columns: []`、`source_key: []`、`start_time: 7`）或原地篡改返回字典的假测试，企图把新塞的防御代码“合法化”；
     - 另一方面：当上层查询/业务服务方法作为 0 调用死代码被删掉时，不顺手删掉底下已经失去唯一生产调用方的私有方法/仓储方法（如 `_normalize_rows`、`list_with_names_by_*`、`QueryService._selected`），反而专门改写单元测试去直接调用这些 0 生产调用的底层死方法为其“续命”！
- **正确做法**：
  - 删上层宽泛 `except` 时，严禁把 `except (KeyError, TypeError, ValueError)` 转移到下层内部类/函数中；只允许在真正的反序列化/外部输入解析单语句保留精确异常转换；
  - 下游已有权威校验入口时，严禁在上游读取函数里抢跑重复校验；
  - 统一规则时直接合并为单一严格口径，严禁引入 `strict_* = False` / `_prevalidated: bool = False` / `facts=None` 双轨开关；
  - 子类、测试夹具与基类/生产契约在源头对齐，严禁在生产代码写 `if self.attr is not None else ...` 或 `scope=None` 动态残缺兜底；
  - 审阅 `git diff` 时必须**同时审阅 `tests/` 的 diff**：凡是为掩护新增过度防御而编造的破坏性假测试，或者为 0 生产调用的底层死方法站台续命的测试，连同生产死代码/防御代码一起彻底删除！

### 28. 严禁保留伺候旧测试的“猴子补丁同步跳板（Monkeypatch Sync Trampoline）”与“默认函数身份比对死分支（`fn is not _DEFAULT_FN`）”
> 母题：D 靠测试续命 · A 死分支、死参数、伪可选

- **典型坏习惯**：
  1. **猴子补丁同步跳板**：大模块拆分为子模块后，在 facade 模块里写 `_sync_runtime_hooks()` / `_sync_process_stop_hook()`，每次业务函数调用入口把 facade 模块上的十几个属性逐个赋值给子模块的全局变量（如 `_stop.fn = fn`、`_paths.read_... = read_...`）；即使旧 monkeypatch 单元测试早已删除，这套运行时属性拷贝跳板仍残留在生产热路径上；
  2. **默认函数身份比对死分支**：在模块加载时保存 `_DEFAULT_FOO = foo`，随后在业务分支里写 `if foo is not _DEFAULT_FOO and foo_v2 is _DEFAULT_FOO_V2:`，试图在运行时探测“是否有外部测试 monkeypatch 替换了旧函数”。
- **正确做法**：生产代码绝不允许为测试 monkeypatch 维护运行时跨模块属性同步器或 `_DEFAULT_*` 身份探测分支；测试应直接 patch 真实定义点或通过显式依赖注入传参。

### 29. 严禁函数对象属性夹带（Callable Piggybacking）与实例瞬态副作用传参（Transient Side-Effect State）
> 母题：I 偷偷传状态

- **典型坏习惯**：
  1. **函数对象属性夹带 + `getattr` 反射分流**：为了不改函数签名/返回值元组，把额外字典或新函数挂到函数对象本身的属性上（如 `reader.enabled_source_map = ...`、`evaluate_request_input.prepared = evaluate_request_input_prepared`），然后在消费端用 `getattr(fn, "prepared", None)` 或 `getattr(config_reader, "enabled_source_map", {})` 反射探测并分流；
  2. **实例瞬态副作用传参**：不通过函数返回值显式返回计算产物（如 `active_codes`），而是在服务实例上挂可变属性 `self.active_codes = None`，在 `with service.read_snapshot():` 内部偷偷赋值、又在 `finally:` 里立刻置回 `None`，逼迫调用方在退出 `with` 块前“抢救”属性。
- **正确做法**：遵循《认知一致性》显式数据流原则——需要传递结构化契约或多返回值时，必须使用显式返回值或数据类，严禁把函数对象或服务实例当成偷渡瞬态状态的临时挂钩。

### 30. 严禁保留“只写不读的幽灵全局状态（Write-Only Global State）”与“已断头的跨层透传参数（Disconnected Parameter Plumbing）”
> 母题：C 清理不彻底 · A 死分支、死参数、伪可选

- **典型坏习惯**：
  1. **只写不读的幽灵全局状态**：移除全局状态读取出口（如删掉 `get_plugin_registry()`）时，只把读取函数改成私有 `_get_plugin_status()`，却保留模块级 `_STATE = {...}` 字典，在类方法里把刚算出来的局部变量写进 `_STATE`，紧接着下一行立刻调 `_get_plugin_status()` 从 `_STATE` 读出来返回——外部无人再读，内部原地绕圈；
  2. **已断头的跨层透传参数**：底层执行函数（如 `execute_job_comparison`）早已删除某个回调/参数（如 `on_progress`），但上层 `run_worker_in_process` -> `JobWorker` -> `compute_job_run` -> `_prepare_and_compute_in_snapshot` -> `_compute_in_read_snapshot` 整整 5 层函数签名与进程间管道仍在层层透传这个在终点 100% 不被读取的死参数。
- **正确做法**：移除某个状态出口或底层参数时，必须顺着调用链从头追到尾，连根删除模块级 `_STATE` 容器及整条跨层透传链上的死参数。

### 31. 严禁“调用方切片绕过自身契约上限（Caller Chunking to Bypass Contract Limit）”与“叠床架屋双重 `@cached_property`”
> 母题：J 口径没在源头统一 · B 多余的层

- **典型坏习惯**：
  1. **调用方切片绕过契约上限 + 重复校验**：被调函数（如 `prepare_batch_projections`）硬编码了 `len(payload) > MAX_ITEMS`，结果同文件下一行的 `restore_batch_projections` 和上游调用模块为了传超长列表，在调用方手写 `range(0, len(payload), MAX_ITEMS)` 切片循环调用，并把内层刚做过的 `isinstance` 与 `ref in refs` 去重在外层重写一遍（甚至漏写外层去重造成跨分片重复漏检漏洞）；
  2. **叠床架屋双重缓存传话筒**：上游方法/属性已是 `@cached_property` 或带字典缓存（如 `DataSource.analysis_groups("reference")`、`DataSource.official`），同对象或下游对象又包一层单行 `@cached_property` 传话筒（如 `reference_item_groups`、`ResourceContext._official`）。
- **正确做法**：在源头函数通过显式参数统一控制边界（如 `max_items=None`），严禁调用方切片绕过并重复校验；已有缓存的属性直接统一调用，严禁叠床架屋二次包装。

### 32. 严禁“为保活 0 调用死代码反向内迁依赖与死台账（Ledger-Driven Zombie Preservation）”与“自抛自接吞错包装”
> 母题：C 清理不彻底（第 1 点） · K 吞错与静默降级（第 2 点）

- **典型坏习惯**：
  1. **为保活死代码反向内迁依赖**：当删除某旧模块（如 `legacy_request_helper.py`）导致另一模块中的函数（如 `safe_url_for`、`_resolve_doc_src`）彻底变为 0 调用死代码时，因为看到质量门禁台账里登记了这些函数，不敢删除 0 调用死函数，反而把被删模块里的辅助函数（如 `_log_warning`）反向搬迁到死函数所在模块里，专门供这几个 0 调用死函数调用；
  2. **对已吞错返回 `bool` 的日志器搞“自抛自接”二次异常包装**：底层 `AuditLogger` 已内部吞错并返回 `False`，外层 `write_audit_log` 故意写 `if result is False: raise RuntimeError(...)` 紧接着在下一行 `except Exception:` 捕获自己刚抛出的异常，且返回的 `bool` 在全仓十几个调用点 100% 被丢弃。
- **正确做法**：门禁台账是存量风险退出追踪表，不是死代码的免死金牌——唯一消费方退役时必须连同 0 调用死函数与台账项一起删除；不得写“同函数内 `if False: raise` 紧接着 `except Exception` 接住自己”的荒唐包装。

### 33. 严禁“双重传参自证有用（Dual-Parameter Self-Justifying Pass-Through）”
> 母题：A 死分支、死参数、伪可选

- **典型坏习惯**：
  - 上游调用方已经从原始大容器（如 `worker_timeline`、`worker_downtimes`、`worker_busy_hours`、`candidate`）中算好或取出了子结构（如 `w_timeline`、`w_downtimes`、`w_busy`、`overlap_reuse`、`rows`），并通过新增的 `=None` 关键字参数传给下游函数；但模型**不删下游函数的原始大容器参数**，而是在下游函数里写一段永不可达的 `if w_timeline is None: w_timeline = worker_timeline.get(...)`。这样原始大容器参数在 AST 里依然“被引用”，静态检查不会报 unused parameter，实则原始参数与 `if ... is None` 回退分支双双沦为死代码。
- **正确做法**：当上游已统一预计算并传入子结构时，直接将该子结构改为必填参数，同步删除下游函数的原始大容器参数与 `if ... is None:` 死回退分支。

### 34. 严禁“为 0 生产调用的死代码抽公共辅助函数（Extracting Shared Helpers for Dead Code）”
> 母题：C 清理不彻底

- **典型坏习惯**：
  - 扫描重复代码时，看到若干方法里有几行 SQL 或判空逻辑重复，**不去核查这些方法在当前架构下还有没有生产调用方**，反而专门新建公共模块（如 `repositories/reference_checks.py`）或私有 helper（如 `ContextRepository._missing_table_rows`），把 0 生产调用的死方法重构得“整整齐齐”，用新增抽象给死代码续命。
- **正确做法**：重构或提取公共函数前，第一步必须用符号检索工具或 `git grep` 核查真实生产调用链；对 0 生产调用的方法直接连根删除，绝不为死代码提取公共辅助函数。

### 35. 严禁在热循环里“现造单键临时字典去凑旧校验签名（Throwaway Single-Key Dict Wrapping）”
> 母题：B 多余的层

- **典型坏习惯**：
  - 数据结构已经从“按行字典”重构为“按列数组/缓存字典”后，为了复用一个接收 `Mapping[str, Any]` 的旧校验小函数 `_required_non_negative_metric(metrics, field_name)`，在双层热循环里对每个元素每次现造一个单键临时字典 `{rank_field: cache[rank_field][item_id]}` 传进去，再在里面把 key 取出来校验。
- **正确做法**：直接让校验函数接收标量值 `(value, field_name)`，严禁在循环内为凑旧签名分配临时单键字典。

### 36. 严禁抽公共基类/宿主类时预留“无子类覆写的 1 行钩子空壳（Pass-Through Hook Stub）”
> 母题：B 多余的层

- **典型坏习惯**：
  - 在合并两套平行类到公共基类（如 `BaseCommandHost`）时，明明所有子类 100% 都执行完全相同的调用（如 `read_current_context_ref(self.conn)`），没有任何子类覆写，却在基类上专门写一个 `def _read_context_ref(self): return read_current_context_ref(self.conn)` 的 1 行转发方法，假装预留“子类扩展钩子”。
- **正确做法**：所有子类行为一致时直接在基类调用目标函数，严禁为不存在的子类差异预留 1 行钩子方法。

### 37. 严禁拆函数/抽模块时“顺手把旧代码的宽泛 `except` 原样复制过去（Copy-Pasted Legacy Broad Catch）”
> 母题：L 删旧壳时搬家兜底 · K 吞错与静默降级

- **典型坏习惯**：
  - 被禁止“新加”宽泛异常捕获后，在拆分或搬迁旧函数（如 `_evaluate_saved_rows`、`resolve_anchor`）时，把旧代码里原本包裹大段逻辑的 `except (AppError, KeyError, TypeError, ValueError, OverflowError):` 原封不动复制进新函数，哪怕新函数内部面对的已经是强类型受控结构、只可能抛出 `AppError` 或时间解析 `ValueError`。
- **正确做法**：每次拆分或搬迁函数时，必须按第一性原理重新核查新函数体内真正可能抛出的边界异常，把历史上混入的 `KeyError / TypeError / OverflowError` 一并清掉，绝不允许“旧代码有所以我照抄”。

### 38. 严禁“抽了公共函数却在调用点写条件互绕 + `lambda: helper(None)[0]` 取元组包装（Caller-Site Branch Bypass & Tuple-Index Lambda）”
> 母题：J 口径没在源头统一

- **典型坏习惯**：
  - 在公共模块 `read_snapshot_time(token)` 里写了 `if token is None: return datetime.now().replace(microsecond=0), None`；结果到了调用端，先写 `read_snapshot_time(token) if token is not None else (None, None)` 故意绕开 `token is None` 分支，紧接着下一行又写 `clock=(lambda: read_snapshot_time(None)[0]) if token is None else None` 去回调刚才被绕开的 `None` 分支并取元组第 0 项。
- **正确做法**：如果“解析已有快照 token 的时间”和“获取当前快照时钟（`current_snapshot_time()`）”发生在事务内外两个不同时机，就应当在源头把这两个职责分清楚（或直接传无参时钟函数 `current_snapshot_time`），严禁把两个不同时机混进一个返回元组的 `Optional` 函数后再在调用点用三元判断 + `lambda: fn(None)[0]` 互相绕弯子。

### 39. 严禁“在轻量无快照路径硬塞 `self._pinned()` 假缓存分支 + 专门编造跨模式测试为其续命（Phantom Cache-Hit Branch + Synthetic Cross-Mode Test）”
> 母题：A 死分支、死参数、伪可选 · D 靠测试续命

- **典型坏习惯**：
  - 生产环境中某轻量查询入口（如 `item_choices` 在 `item_ref is None` 时调用 `reader.choices() -> load_choices()`）是在新建实例上直接裸调的，根本不进 `read_snapshot` / `detached_read_snapshot`，因此 `self._pinned()` 在生产上 **100% 恒为 `None`**；
  - 但模型偏要在 `load_choices()` 开头塞一段 `facts = self._pinned(); if facts is not None: return facts`，为了防止这段生产永不触发的死分支被识破，又在测试文件里**凭空编造一个生产根本不存在的调用组合**（先开全量 `with reader.detached_read_snapshot():`，再在里面调无参 `reader.choices()`）专门去跑通这个死分支！同时还把只服务于轻量路径的 `.choices()` 挂在公共基类上让全量子类 `FullItemProjection` 白白继承一个 0 调用的方法。
- **正确做法**：轻量路径只做轻量路径的事，严禁塞入生产 0 触发的 `_pinned()` 分支，更严禁在测试里构造生产不存在的跨模式组合为死分支站台；不为单一路径的方法强行造继承体系。

### 40. 严禁“通过原地篡改数据字典 `facts["reader"] = reader` 往纯投影层偷渡查库服务（Dict-Mutation Dependency Smuggling）”
> 母题：I 偷偷传状态

- **典型坏习惯**：
  - 本应在数据读取阶段（如 `ReportFacts.read`）完成的入参范围校验（如 `scope.target_ref`），被拖延到了纯内存投影层（如 `validate_selected_refs`）；为了在纯投影函数里还能反查数据库，在中间层写 `facts["reader"] = reader`，把整个带 DB 连接的服务实例偷偷塞进纯数据字典 `facts` 里传给下游，破坏“读取层查库、投影层纯计算”的职责边界。
- **正确做法**：所有依赖数据库或实体索引的入参校验必须在读取阶段（`read`）一次性完成，严禁通过原地篡改 `facts` 数据字典向纯投影层偷渡服务或仓储实例。

### 41. 严禁“生产已切换为 `prepare + apply_prepared` 两步式协议，却在生产类里保留 0 生产调用的 `.apply` 一步式死方法专供测试调用（Test-Only Monolithic `.apply` Leftovers）”
> 母题：D 靠测试续命

- **典型坏习惯**：
  - 生产路由早已统一走 `prepare`/`prepare_checked` + `apply_prepared` 两阶段协议，但生产服务类（如 `CatalogMutationService`、`ResourceMutationService`、`VendorMutationService`、`WorkflowMutationService`）里却继续保留着生产 **0 调用**的 `.apply()` 一步式方法（甚至在多个服务类里把 12 行 `.apply` 逐字复制粘贴三遍，内含 `getattr(identity, "ref", None)`），仅仅因为旧测试夹具还在调 `.apply()`。
- **正确做法**：生产类只保留真实生产协议入口（`prepare` + `apply_prepared`），彻底删除生产 0 调用的 `.apply` 包装方法，并将测试夹具迁移到真实的生产两阶段调用链。

### 42. 严禁“函数签名照收参数，进函数第一行立刻用另一字典同名覆盖（Parameter Immediately Overwritten on Entry）”
> 母题：A 死分支、死参数、伪可选

- **典型坏习惯**：
  - 重构引入聚合上下文 `prepared` 后，在函数签名里继续保留原参数（如 `_enrich_features(..., metrics_by_id, ..., items)`），结果进函数第一行就写 `item_by_id, metrics_by_id, duration_by_id = prepared["items"], prepared["metrics"], prepared["durations"]`——因为同名局部变量赋值让 Python/Linter 以为该变量被使用了，从而掩盖了入参 `metrics_by_id` 100% 被丢弃覆盖、`items` 0 次读取的事实！
- **正确做法**：已从聚合结构解构的数据，必须同步从函数签名及调用方传参列表中彻底删除，严禁用同名覆盖掩盖死参数。

### 43. 严禁“迁移测试时为单个测试文件新建孤立 `*_support.py` 并绕过公开方法直调私有 `_legacy_*` 方法”
> 母题：D 靠测试续命

- **典型坏习惯**：
  - 清理旧写侧测试时，为了少写几行内联 seed SQL，专门新建一个全仓只有 1 个测试文件引用的 `event_test_support.py`，并在测试断言里绕过公开入口 `provider.facts_by_scope([scope])` 直接调用私有方法 `provider._legacy_facts([scope])`。
- **正确做法**：仅服务于单个测试文件的几十行夹具直接内联在该测试文件中；测试断言应通过公开契约入口验证，不绑定内部私有方法名。

### 44. 严禁“删中间包装模型/反射辅助函数时，把 `.get()` / `or default` / `is not None` 偷偷搬家进下游（Fallback Migration on Removal）”
> 母题：L 删旧壳时搬家兜底

- **典型坏习惯**：
  - 要求删除只用于 `INSERT` 传参的死包装模型（如 `ResourceMaintenance`）时，编码者虽然删了模型文件，却在仓储 `create(payload)` 里把原来模型上的默认兜底写成了 `payload.get("reason_code"), payload.get("status") or "active"`——而唯一生产调用方早已构造了包含完整必填键且 `status="active"` 的受控字典；
  - 或者要求删掉 `_extract_task_id(task)` 反射辅助函数时，顺手在旁边的推导式里新塞入恒真的 `and task.id is not None`。
- **正确做法**：删除中间包装模型或反射辅助函数时，必须直接按上游受控字典/对象的真实契约用 `payload["key"]` / `task.attr` 强访问，**严禁在删旧壳的同时把 `.get()`、`or default`、`is not None` 偷偷塞进下游**。

### 45. 严禁“为迁就旧下标保留 `(val, 0)` 哑元元组槽，或删了唯一生产入口却保留整条死采集链（Dummy Tuple Slot & Orphaned Telemetry Chain）”
> 母题：B 多余的层 · C 清理不彻底

- **典型坏习惯**：
  - 上游已经不再需要二元组里的第 2 项（如默认天数 `default_days`），编码者删掉计算逻辑后，为了偷懒不改消费端的 `selected[0]`，居然写 `append((vendor_id, 0))` 往列表里硬塞死常数 `0` 凑二元组；
  - 或者删掉了报告导出方法 `.finalize()` 的唯一生产调用点后，却把 `.finalize()` 本体、报告拼装函数以及热路径上每次迭代都在 `append` 的 5 个事件收集列表原样留在生产类里空转，只因为某个旧单元测试还在调 `.finalize()`。
- **正确做法**：直接将元组精简为标量并同步更新消费端；一旦某个导出/汇总出口在生产上归零，必须顺着数据流反向清理所有仅供该出口消费的字段、事件收集列表与辅助函数。

### 46. 严禁“多信号源防线用 `a if a is not None else b` 粗暴择一，导致陈旧死信号遮蔽存活新信号（Stale Signal Masking Active Signal）”
> 母题：O 语义走样

- **典型坏习惯**：
  - 系统同时存在两个状态信号源（如上次运行遗留的 `manifest` 状态快照与当前新进程持有的 `lock` 锁文件）。检查是否有活跃实例时偷懒写 `state = manifest if manifest is not None else lock`——结果当陈旧 `manifest` 存在（进程已死）而新 `lock` 进程正存活运行时，`manifest is not None` 成立直接短路，把活着的 `lock` 信号彻底遮蔽，误判为“无活跃实例”。
- **正确做法**：对多个独立生命周期的状态/锁信号源，必须遍历或独立评估每一个信号源的存活/监听状态（任一活跃即阻断），严禁用 `a or b` / `a if a is not None else b` 让陈旧信号遮蔽活跃信号。

### 47. 严禁“发现上下文/前置数据损坏时静默降级为 `None`，反而绕过下游业务约束（Corrupt Context Downgraded to `None` Bypassing Guards）”
> 母题：K 吞错与静默降级

- **典型坏习惯**：
  - 校验编辑操作时，先调 `problem = check_context_problem(context)` 检查关联上下文是否完整，接着写 `group = None if problem else build_group(context)`——结果当关联的批处理组上下文损坏（如历史迁移数据缺少整组周期）时，`problem` 非空反而让 `group` 变成了 `None`，直接跳过了紧接着的 `if group and group["mode"] == "merged": raise ...` 业务限制，造成“数据越坏、限制越松”的严重漏洞！
- **正确做法**：前置上下文校验发现 `problem is not None` 时，必须当场拒绝（`raise`）或显式阻断，严禁将损坏的上下文静默降级为“无上下文（`None`）”从而绕过针对该上下文的业务约束。

### 48. 严禁“把上游已有的内存字典/列表伪装成 Repository 传给下层，下层再调 `.list()` 重新遍历建索引（Fake In-Memory Repo Adapter & Index Rebuilding）”
> 母题：B 多余的层 · G 重复计算

- **典型坏习惯**：
  - 上游手里已经有现成的 `items_by_id` 索引字典和过滤好的 `capability_rows` 列表，却为了迁就下层解析器旧的“接收 `repo` 实例并调 `repo.list()`”签名，专门在内存里写 `_ItemSnapshot` / `_CapabilitySnapshot` 伪仓储包装类（甚至带上完全不读的死参数 `def list_capabilities(self, status=None): return list(self.rows)`），传进下层后下层又调 `.list()` 重新遍历建一遍一模一样的 `items_by_id` 字典！
- **正确做法**：按《认知一致性》直接让下层接收上游已准备好的字典/列表事实，彻底拔掉内存伪仓储适配层与重复建索引逻辑。

### 49. 严禁“外层批处理已持有事务并完成契约检查，内层循环却调单条命令入口逐行开嵌套 `SAVEPOINT`（Per-Row Nested Savepoint in Batch Loop）”
> 母题：N 事务、锁与状态生命周期 · F 重复校验

- **典型坏习惯**：
  - 批量保存入口 `apply_rows` 外层已经开启了 `with self.tx.transaction():` 并执行了表结构契约检查，但在循环处理每一行时直接调用单条命令入口 `service.apply_create(row)`——而 `apply_create` 内部又开一层 `with self.tx.transaction():` 并再做一遍相同的表结构检查，在无任何单行独立回滚需求的批处理中白白制造 $N$ 次嵌套 `SAVEPOINT` 和 $N$ 次重复检查。
- **正确做法**：抽出无事务包装的纯写入原语供单条入口（外包事务）与批处理循环（共享外层事务）共同调用，让一个原子批处理只由唯一的外层事务持有。

### 50. 严禁“单元素/首段兜底吞掉多段真实结构语义（Single-Segment Collapse / `[0]` Fallback Masking Multi-Segment Semantics）”
> 母题：O 语义走样

- **典型坏习惯**：
  - 上游已经解析出跨天、跨休息时段的多段有效执行区间 `active_segments: List[Tuple[datetime, datetime]]`，下游在校验或回填资源池占用时为了省事写 `segments[0]` 或退化回 `[window_start, window_end]` 单一首尾大区间兜底——结果把休息与挂起时段也算成资源 100% 满负荷占用，造成严重的容量误判。
- **正确做法**：多段结构在整个生命周期内必须保持多段列表传递与逐段累加，严禁用 `segments[0]` 或首尾包络区间做简化兜底。

### 51. 严禁“把零长度点事件混入非零区间重叠公式，造成边界擦边误报冲突（Zero-Duration Point-Event Overlap False Positive）”
> 母题：O 语义走样

- **典型坏习惯**：
  - 系统同时存在有持续时间的常规任务（`start < end`）和零耗时里程碑/点事件（`start == end`）。校验资源冲突时不先区分语义，而是为了“兼容 `start == end`”去改区间相交公式或把点事件塞进资源占用时间线——结果 `08:00~08:00` 的零耗时点事件撞上 `08:00~10:00` 的正常任务边界，被误判为资源双重占用冲突。
- **正确做法**：零耗时点事件（`point_event` / `duration == 0`）不消耗持续资源容量，必须在进入资源占用重叠检测前显式排除，保持半开区间 `[start, end)` 相交判定 `start < other_end and end > other_start` 的数学纯粹性。

### 52. 严禁“为给内部只读返回值的 `list()` 浅拷贝站台，专门写测试去原地 `.append()` 篡改返回值（Mutating Returned List in Test to Justify Defensive Copy）”
> 母题：D 靠测试续命 · M 无用拷贝与往返转换

- **典型坏习惯**：
  - 内部数据容器（如 `PreparedImportSource.parsed()`）的所有生产调用方都只是只读遍历返回的 `rows` 和 `notices`，从不原地修改；但编码者在 getter 里写了 `return list(self._rows), list(self._notices)` 每次调用拷贝两份列表，然后为了证明这俩 `list()` 拷贝“删不得”，专门在单元测试里写 `rows, warnings = source.parsed(); rows.append({"row_number": 99}); assert source.parsed() == ...`！
- **正确做法**：只读内部访问器直接返回底层元组/列表引用，同步删掉测试里人为原地篡改返回值的假断言。

### 53. 严禁“前一行刚调 `_validate_x()` 抛异常，后一行又对同一条件写 `if not x:` 重复守卫（Post-Validation Redundant Guard）”
> 母题：F 重复校验

- **典型坏习惯**：
  - 函数第 55 行刚调用了 `self._validate_preconditions(conn, payload)`（当 `payload["results"]` 为空时内部 100% 已经抛出 `empty_submission_payload` 异常），结果第 63 行进事务后又写一遍 `if not payload["results"]: raise ...`；或者前一步刚通过 `validate_state_guard_before_persist` 查出并返回了最新 `version_num`，下一行又调 `get_latest_version_num(conn)` 重查一遍数据库。
- **正确做法**：信任同函数/同事务内前一步已经完成的权威校验与查询产物，直接复用返回值，删除事后重复守卫与二次查库。

### 54. 严禁“在 `@property.setter` 与显式更新方法里双向重复同步同一状态（Dual-Hook State Sync in Setter + Method）”
> 母题：I 偷偷传状态 · J 口径没在源头统一

- **典型坏习惯**：
  - 担心别人直接赋属性 `self.best = candidate` 忘了同步关联状态，就在 `@best.setter` 里塞入副作用 `self.report_state.sync_best(value)`；然后紧接着在唯一的正规更新方法 `accept_candidate` 里写 `self.best = candidate`（此时 setter 已经触发了一次同步），下一行又手写调用第二遍 `self.report_state.sync_best(candidate)`！
- **正确做法**：状态变更有且只有一个正规入口方法，不在 `@property.setter` 里藏隐式同步副作用，更不在 setter 与方法里双重调用。

### 55. 严禁“同一领域规则在表单详情修改与批量文件导入里各写半套（Split Domain Rule Across Form & File Import）”
> 母题：J 口径没在源头统一

- **典型坏习惯**：
  - 同一个业务领域不变量（例如资源日历“非工作日 `is_workday=0` 时必须清空工作时段、设可用配额为 0、清空扩充上限”），在单条详情表单归一化函数里写一套，在电子表格批量文件导入里又拆成两段手写一套，甚至两边错误提示归属字段都不一致。
- **正确做法**：遵循《认知一致性》，将领域联动规则抽成唯一的领域模型纯函数（如 `apply_rest_day_rules(fields)`），单条表单修改与批量文件导入共同调用同一套规则函数。

### 56. 严禁“用 `json.loads(json.dumps(x))` 给内部受控字典做防御性深拷贝，或比对完文档后再调 `.as_dict()` 丢弃返回值自检（JSON Round-Trip Defensive Cloning & Discarded Parse Check）”
> 母题：M 无用拷贝与往返转换 · F 重复校验

- **典型坏习惯**：
  - 害怕下游改字典或不信任刚生成的 JSON 字符串，专门写个 `raw_facts(x)`（内部是 `json.loads(document_text(x))`）在只读列表的万行循环里对每行做 2 次 `json.dumps + json.loads` 往返，结果唯一调用方 `[item for item, source in load_rows(...)]` 直接把包含这些字段的 `source` 丢弃；
  - 或者在预览校验里比对完 `preview.document != current.document` 相等后，还加一行丢弃返回值的 `current.as_dict()`（再跑一遍 `json.loads`）去“自证自己刚生成的 `current.document` 能反序列化”。
- **正确做法**：内部受控字典绝不用 `json.dumps + json.loads` 充当深拷贝；只读列表路径绝不预序列化无人读取的快照字段；严禁写丢弃返回值的 `.as_dict()` 自检。

### 57. 严禁“机关枪式双保险/三保险连环全量重校验（Back-to-Back Full Re-Validation Paranoia）”
> 母题：F 重复校验

- **典型坏习惯**：
  - 同一个函数里，前一行刚调完 `payload = validate_candidate(prepared, rows, [])`（内部已跑完全套 `validate_item_candidate` 或 `validate_commit_constraints` + `validate_state_guard_before_persist`），仅仅隔了 4 行不放心，又对一模一样的 `(conn, prepared, payload)` 调一遍 `validate_commit_payload(conn, prepared, payload)` 把全套约束校验从头重跑第 2 遍；甚至在方案提交流程里连环套 3～4 遍全量校验。
- **正确做法**：一次业务动作中的同一种全量约束校验有且只能执行 1 次；若有模式微调（如 `"publish"` vs `"commit"`），通过参数在唯一一次校验中直接完成。

### 58. 严禁“先全量急切算完所有分支/所有行的重型结果，出口再用 `[]` / `_` 抹掉（Eager Full-Universe Compute Then Output Masking）”
> 母题：E 先算后扔

- **典型坏习惯**：
  - 概览模式（只看 `totals` 计数）或单分区展开（只看 1 个 `section`），却先无条件构造全部 6 个分区的完整行 DTO 列表并重算所有区间重叠，最后在返回出口写 `{key: [] for key in ...}` 把刚造好的 6 个大列表全抹成空数组 `[]`；
  - 对未开启流程或未确认阶段的记录，仍先对全表 25,000 条任务记录无条件跑 65,000 次 `content_text`（`json.dumps`），随后 99% 的字符串根本不被比对就直接丢弃；
  - 核心评估每次候选解码只需一个整数 `switch_count`，却构造 5,000 个对象调 UI 富诊断函数生成数千个 ISO 时间字符串和 17 字段字典，最后只取 `["known_count"]` 把整棵诊断树扔掉。
- **正确做法**：按请求模式与实际消费需求做按需计算（overview 算轻量计数、section 只构造目标分区 DTO、已确认记录才算 `content_text`、热路径用纯计数函数），绝不“先全量造出来再在出口抹掉”。

### 59. 严禁“发现上游函数漏字段/漏语义，不去修源头，在下游调用处手写推导式打补丁（Downstream Patching Over Upstream Defect）”
> 母题：J 口径没在源头统一

- **典型坏习惯**：
  - 上游 `build_resource_tasks` 返回的字典漏填了契约要求的 `"row_ref"`；编写下游 `metrics_collector.py` 时明明发现了，却为了“不改上游文件”，在调用处手写 `[dict(task, row_ref=row_refs[task["task_ref"]]) for task in build_resource_tasks(...)]` 打补丁，导致上游其他调用方继续裸奔；
  - 或者发现合并任务组的阶段前置就绪时间在源头只挂在首节点 `min(seq)` 上，不去源头统一，只在执行引擎输入下游写循环打补丁，造成预检、提交校验与引擎输入口径分裂。
- **正确做法**：发现上游契约缺字段或同组语义不全，必须回到上游源头一次性补齐，并删除下游一切打补丁的推导式与补偿循环。

### 60. 严禁“在 `__init__` 急切预跑重计算 + 用包含可变字段的全量状态当缓存 key，导致改一行全盘重算（Eager `__init__` Pre-Compute + Coarse State Invalidation）”
> 母题：E 先算后扔 · H 缓存失效

- **典型坏习惯**：
  - 在校验器 `__init__` 里进门就急切调 `self._protect()` 算一遍全量基线锁定窗与依赖完备性策略，却把用户即将修改的 `row["current"]` 也编进缓存 key `self._protected_state`；结果 `change()` 刚实例化完校验器、改完 1 行 `row["current"]`、紧接着调 `evaluate_saved()` 时，缓存立刻失效，把与 `row["current"]` 毫无关系的基线锁定窗与完备性策略当场重算第 2 遍。
- **正确做法**：不在 `__init__` 中急切跑重计算；缓存 key 只能绑定真正影响该计算结果的不变输入，绝不把无关的可变字段混入缓存 key 自造失效。

### 61. 严禁“把缓存未命中检查（`if missing:`）写在重型依赖查询之后（Pre-Cache-Check Eager Dependency Fetch）”
> 母题：E 先算后扔 · H 缓存失效

- **典型坏习惯**：
  - 在带缓存的方法（如 `projected_tasks`）里，把只有缓存未命中（`if missing:`）才需要用到的数据库查询 `task_refs = self.task_refs(...)` 和 `steps = self.step_refs(...)` 写在 `if missing:` 之前——导致即便 100% 命中缓存，每次调用也照跑一遍全量引用查询与校验。
- **正确做法**：先查缓存算出 `missing`，只有 `if missing:` 非空时才按 `missing` 子集拉取依赖数据。

### 62. 严禁“刚断言 `if not conn.in_transaction: raise`，下一行立刻再套一层无意义的 `with self.tx.transaction():` Savepoint（Assertion-Then-Redundant-Savepoint）”
> 母题：N 事务、锁与状态生命周期 · F 重复校验

- **典型坏习惯**：
  - 外层命令框架已经开启事务，内层确认函数第一行刚写完 `if not self.conn.in_transaction: raise RuntimeError("... must run inside an active transaction")`，第二行就无脑套一个 `with self.tx.transaction():`（且内部无任何局部异常捕获回滚需求），平白无故制造冗余 SQLite `SAVEPOINT`。
- **正确做法**：已明确要求在外层活跃事务内执行且无局部回滚语义的方法，直接执行 SQL 操作，严禁再套冗余内层 `with self.tx.transaction():`。

### 63. 严禁“为了拿下拉框筛选项或 `len()`，把所有 $N$ 个候选或整张表全量反序列化并重验一遍（Full Hydration Just For Dropdowns or `len()`）”
> 母题：E 先算后扔

- **典型坏习惯**：
  - 详情抽屉只需要组装资源/分类下拉筛选项（读模型 `source.resource_cohort` 里早已预计算好），却写 `for candidate in source.rows: source.tasks(candidate["candidate_ref"])`，把同次执行所有 $N$ 个候选方案的任务列表全查出来、触发整库 `SnapshotFacts` 反序列化并把所有任务重验一遍；或者只为求导出预览行数 `len(...)` 就把整张表物化成 Python 字典列表。
- **正确做法**：下拉筛选项与计数直接从已预计算的轻量读模型或按需聚合中获取，严禁为此触发全候选/全表重型水合。

### 64. 严禁“热路径每次 `copy(obj)` 换壳，连环击穿下游以 `id(obj)` 为 key 的多重缓存（Per-Iteration `copy(obj)` Busting `id(obj)` Identity Caches）”
> 母题：H 缓存失效 · M 无用拷贝与往返转换

- **典型坏习惯**：
  - 为了给每次迭代/解码覆盖 1~2 个动态字段（如 `node_id`, `worker_id`），在每次请求入口对 5,000 个对象执行 `task = copy(base_task)` 生成新实例；而下游多处核心缓存（输入指纹、资源 Claim、剪枝快照、预排序）全部以 `id(task)` 为缓存键，结果每次 `copy(task)` 都连环击穿所有 `id(task)` 缓存，引发数十万次重复反射与重建。
- **正确做法**：对变体对象按 `(base_id, override_tuple)` 建立实例池并在创建时一次性向 `id(obj)` 缓存注册，或者将缓存键绑定到稳定的业务标识而非易失的临时对象 `id()`。

### 65. 严禁“把每行唯一 ID（如 `task_id`）塞进本该跨行复用的窗口/分组缓存键，造出 0% 命中率的假缓存（Unique Row ID in Shared Cache Key -> 0% Hit Rate）”
> 母题：H 缓存失效

- **典型坏习惯**：
  - 为耗时函数（如时间窗可用配额计算）建了字典缓存 `quota_by_window`，却把每条任务唯一的 `task_id` 塞进缓存键 `window_key = (task_id, start_dt, due_dt)`，导致单次遍历 5,000 条任务时缓存命中率恒为 **0%**，形同虚设。
- **正确做法**：缓存键只能包含真正决定计算结果的参数维度（如 `(source, node_id, worker_id, category_id, priority, start_dt, due_dt)`），严禁混入调用方个体唯一 ID 把共享缓存退化成只写不命中的垃圾字典。

### 66. 严禁“在 `BEGIN IMMEDIATE` 写事务锁内搞逐行 N+1 发号/反查，或把纯内存建报表/大 JSON 哈希包在数据库事务内（Write-Lock N+1 Allocation & In-Transaction Heavy CPU/Disk IO）”
> 母题：N 事务、锁与状态生命周期

- **典型坏习惯**：
  - 5,000 行批量导入在独占写锁内写 `for row in rows: repo.allocate_number()`（每次跑 3 条 SQL，共 15,000 次 SQL）或逐行 `identities.find_active()`；或者在 `with TransactionManager(conn).transaction():` 读完表后不关事务，把后续纯内存的百万字符 `canonical_json` 哈希、电子表格 `.xlsx` 构建或磁盘日志文件读取全包在 SQLite 事务里，长时间占锁阻塞并发。
- **正确做法**：写锁内发号/反查必须按批次 `allocate_numbers(count)` / 批量查询一次性完成；只读事务在完成全部 SQL 读取后立即退出，纯内存投影、哈希与文件导出放在事务块外执行。

### 67. 严禁“底层公共函数直接抛异常抢跑，把上层专门写的精细诊断分支变成 100% 死代码（Exception Preemption Killing Upstream Diagnostic Branch）”
> 母题：K 吞错与静默降级 · A 死分支、死参数、伪可选

- **典型坏习惯**：
  - 预检函数后半段专门写了 `_actual_periods` 来收集同组各步骤起止时间不一致并返回精确到步骤序号的 `group_span_mismatch` 诊断；但前半段调用底层公共函数时传了 `linked_groups=groups`，底层一遇到同组时间不一致当场抛 `ValidationError` 被外层笼统捕获成 `unproven`，导致后半段的精细诊断 100% 永远无法到达。
- **正确做法**：预检/诊断链路与强拦截执行链路共享纯数据计算，不在预检前置步骤触发笼统异常吞掉下游精细诊断。

### 68. 严禁“在只读方法（如 `preview.as_dict()`）里每次调用都无缓存重跑 `json.loads` 大文档，或直接用 `+=` 拼接两个已聚合列表导致重复 key（Uncached Repeated `json.loads` Getter & Raw `+=` on Grouped Summary Lists）”
> 母题：G 重复计算 · O 语义走样

- **典型坏习惯**：
  - 不可变预览对象的 `as_dict()` 不缓存解析结果，同一个请求在路由、校验、确认里调 4 次 `.as_dict()` 就把 5,000 行 JSON 字符串反序列化 4 遍；或者两个子集各自按 `code` 聚合出有序的 `exclusion_reasons` 列表后，外层直接用 `list_a += list_b` 拼接，导致相同 `code` 重复出现且破坏排序不变式。
- **正确做法**：不可变预览对象的 `.as_dict()` 缓存首次解析结果；合并两个聚合统计列表必须在计数器字典层按 `code` 累加后再排序输出。

### 69. 严禁“局部切片送入全局关系闭包校验，制造‘看不见同组其他成员 = 没冲突’的静默漏判（Partial-Scope Query Breaking Closure Invariants）”
> 母题：O 语义走样

- **典型坏习惯**：
  - 做局部查询时只写 `WHERE project_code IN (selected_projects)` 拉取当前操作项目的任务节点，然后直接送进需要检查“同一个共享任务组 `group_id` 不能跨项目混用（`group_project_mismatch`）”的校验函数——因为其他项目引用同一个 `group_id` 的节点根本没被 SQL 查出来，校验函数永远看不见跨项目成员，导致非法共享组被静默放行落库；
  - 或者校验两个主干阶段节点（`sub_unit_id IS NULL`）在执行流水线中是否连续相邻时，SQL 写了 `WHERE sub_unit_id IS ? OR sub_unit_id IS NULL`，把夹在两个主干节点中间的子单元节点（`sub_unit_id = 'U1'`）全过滤掉了，导致跨子单元阶段的断档节点被误判为“连续相邻”。
- **正确做法**：凡校验同组一致性、跨实体唯一性或路线连续性，SQL 查询必须按**完整关系闭包**（如 `project_code IN (...) OR group_id IN (related_groups)`，或整条流水线节点全集）拉齐全部关联成员。

### 70. 严禁“建好了预计算读模型，下游却顺手访问底层冷路径 `@property` 把整库大 JSON 反序列化拉回热路径（Precomputed Read-Model Bypassed by Cold-Property Access）”
> 母题：G 重复计算

- **典型坏习惯**：
  - 执行结果落库时专门构建了轻量的 `DecisionSummaryReadModel`（`source.summary_tasks` 与 `source.completion.delivery` 已包含 `order_ref`、`order_id`、`task_ref`、`start`、`end`），结果下游写 `order_impacts` 或下拉框 `filter_options` 时顺手写了 `source.raw_facts.order_ids_by_ref`、`source.tasks(...)`、`source.baseline_tasks`，当场触发 `@property` 惰性加载数 MB 的 `raw_facts_json` 全表 JSON 反序列化 + 5,000 条任务重验，让预计算读模型形同虚设；
  - 或者 `save()` 保存方案时已经把完整的 `capacity`、`comparison` 算好存进 `snapshot_json`，查看已保存方案时却无条件重新调 `load_draft` 把数 MB 草稿读出来全盘重算。
- **正确做法**：既然建立了预计算读模型或已保存快照，展示与筛选链路必须 100% 从读模型取数，严禁顺手触碰底层重型 `@property` 击穿读模型。

### 71. 严禁“先对未 diff 的全量输入字段跑‘禁止修改’校验，校验完才过滤 `row[k] != v`（Validating Unchanged Input Fields Before Diffing）”
> 母题：O 语义走样

- **典型坏习惯**：
  - 更新接口先把请求 payload 里的所有字段放进 `changes` 字典，先调 `_validate(..., changes)`（内部写 `if "duration_days" in changes: reject("合并执行组不能逐项改周期")`），等 `_validate` 跑完下一行才写 `changes = {k: v for k, v in changes.items() if row[k] != v}`——导致用户根本没改 `duration_days`，只因前端表单原样带回了旧值就被当场误拒！
- **正确做法**：必须先与当前记录比对算出真实发生变化的字段集 `actual_changes = {k: v for k, v in changes.items() if row[k] != v}`，再对 `actual_changes` 执行变更限制校验。

### 72. 严禁“先生成依赖引用的派生快照/校验，后原地覆盖主键引用或篡改校验基准线（Mutating Identity/Bounds After Snapshot or Before Boundary Check）”
> 母题：O 语义走样

- **典型坏习惯**：
  - `save()` 在 `prepare` 阶段先用草稿的旧 `row_ref` / `task_ref` 生成了 `snapshot`（其中 `snapshot["capacity"]` 绑定的是旧 `row_ref`），到了 `mutate` 阶段才生成新 UUID 覆盖 `snapshot["tasks"]` 的 `row_ref`，导致落库 JSON 内部 `capacity` 与 `tasks` 的引用错位；
  - 或者在调用 `validate_candidate` 检查 `end_dt > prepared.end_date_norm`（`candidate_outside_window`）的前一行，写 `prepared = replace(prepared, end_date_norm=_span_end(rows))` 用待测数据自身的最大结束时间覆盖窗口上限，把紧接着的越界检查变成 100% 恒假的死代码。
- **正确做法**：新身份/新引用必须在构建任何派生快照之前先行分配；严禁在调用边界校验前用待测数据自身篡改校验基准。

### 73. 严禁“在碰撞重试循环或多点探测热路径里，把‘命中终态才需要的重计算’或‘从 0 全量重演’塞进每次迭代（Eager Final-Only Cost in Retry Loop & Non-Incremental Replay）”
> 母题：E 先算后扔

- **典型坏习惯**：
  - 时间窗选槽 `while True:` 碰撞避让循环里，每次因重叠作废的中间尝试都在 `_estimate_attempt` 末尾调 `calendar.active_hours_between(...)` 逆向漫游日历算 `total_hours`，而实际上只有最终无碰撞（`shift_to is None`）那一次才需要用 `total_hours` 算 `idle_gap`；
  - 给 $O(1)$ 的 `owned_segment_certificate` 人为设 `len(segments) >= 128` 门槛，导致 `< 128` 时每次选槽都退化为 `tuple(segments)` 拷贝与逐元素对比；
  - 候选插入位置包含探索位 `0` 和锚点附近 `4500..4505` 时，只共享 `without[:min_pos]`（即 `0`），导致每个 `4500+` 候选位都从 `0` 重演 4,500 步，而不按升序位置增量推进 `without[prev_pos:position]`；
  - 写锁内单次发号 `allocate_numbers(1)` 按 `remaining=1` 逐号发 SQL 试探占用（遇到 20 个已导入连续单号就发 21 次 SQL），而不按固定块窗口（如 `max(remaining, 64)`）一次查询可用号。
- **正确做法**：重试循环内只算判定碰撞所需的最小量，命中终态后再算附加指标；有序多点探测必须按升序增量推进状态或按固定块窗口批量探测。

### 74. 严禁“前端会话切换‘半截子重置’、隐藏常驻组件被动监听刷新自撞、以及父子共用 `WeakMap(adapter)` 命令 Store 触发双倍请求（Frontend Partial Reset, Hidden-Mount Baseline Collision & Shared Command Store Double-Fire）”
> 母题：N 事务、锁与状态生命周期

- **典型坏习惯**：
  - 切换选中行或结束命令时只清 `preview` / `saved`，漏清 `quantity`、`error`、`busy`、`draft` 或漏调 `command.reset()`，导致上一行的报错、草稿或旧请求的 `finally { setBusy(false) }` 污染下一行，甚至提交被拒后红框永久死锁；
  - 子面板点“取消”后用 `<div hidden={!entry}>` 保持挂载且不清 `dirty`，用户在其他页签保存触发父组件 `current` 更新时，隐藏组件后台监听到 `result !== context` 把用户自己的保存误判为外部并发冲突，再次点开时直接弹冲突横幅锁死；
  - 父工作区和子弹窗传入同一个 `adapter` 调用 `useCommand(adapter)`（底层按 `WeakMap(adapter)` 共享同一 Store），子弹窗提交成功时 `onCommitted(refresh)` 与父工作区监听 `command.phase === 'done'` 同时触发，把全套接口重复请求两遍。
- **正确做法**：前端子会话的完整状态（草稿、预检、错误、忙态、命令状态机）必须与会话身份同生共死；取消编辑必须卸载或彻底销毁草稿；父子组件严禁同时订阅同一 `WeakMap` 命令 Store 并各自触发全量刷新。

### 75. 严禁“预览与提交走两套算法，或把连带副作用绑在‘当前行是否 `unchanged`’上导致隐式落库无预警（Preview-Commit Split Algorithm & Unchanged-Row Side-Effect Blindness）”
> 母题：J 口径没在源头统一 · O 语义走样

- **典型坏习惯**：
  - **预览与提交算法分裂**：文件批量导入在预览阶段（`_resolve_final_owners`）算出一套目标集合，而确认提交阶段（`prepare_owner_assignment_changes`）却不消费预览结果，自己按循环行序逐条执行 `selected.clear()`——导致历史多主责人原样保留时，预览显示全部保留为 `"yes"`，提交落库却按文件行序把前几个静默清空、只留最后一个；或者提交时在执行模式改为纯内部处理后自动把 `default_batch_policy` 清为 `None`，预览 `proposed_fields` 却漏写该清除规则，导致预览显示保留旧值、点确认却被抹掉；
  - **把整组连带副作用绑在单行 `result != "unchanged"` 上**：当历史存在 `{R1, R2}` 双主责节点，文件保留 `R1="yes"`（与旧值相同，`result == "unchanged"`）并只修改了 `R3` 的权重等级时，因为该成员整体有变更，提交时 `R1` 认领主责会把 `R2` 连带降级为 `"no"`；但预览提示写了 `if row["after"]["is_primary"] == "yes" and row["result"] != "unchanged":`，只因认领行 `R1` 自身字面值未变，就把即将发生的 `R2` 降级提示和 `requires_confirmation = True` 彻底吞掉。
- **正确做法**：预览（Preview）与确认提交（Confirm/Apply）必须 100% 共享同一个领域状态转移产物；连带副作用提示必须依据“旧状态集合 vs 最终目标集合”的真实差异触发，严禁因触发源行自身字面值 `unchanged` 而漏报连带写入。

### 76. 严禁“模式切换（如多时段切回连续单时段、空时段恢复值班）时，用旧模式的派生汇总列做‘同值过滤’，把模式切换本身的语义变化吞掉（Cross-Mode Same-Value Filtering Killing Mode-Transition Semantics）”
> 母题：O 语义走样

- **典型坏习惯**：
  - 资源日历原为多时段 `08:00-12:00, 13:00-17:00`（数据库汇总列存着 `shift_start="08:00", shift_end="17:00", shift_hours=8.0`）。当用户显式传 `periods=None, hours=8.0`（或 `shiftStart="08:00"`）切回连续单时段模式时，`_changed_shift_keys` 拿新传的 `hours=8.0` 跟多时段旧汇总列 `row["shift_hours"] == 8.0` 比对，误判为“时长没变（`same=True`）”，从而不触发单时段结束时间推导（`08:00 + 8h = 16:00`），保留旧汇总列的 `17:00` 导致算成 9 小时报错或越界；
  - 成员日历休息日存为显式空时段 `periods_json="[]"`（旧行残留 `shift_start="08:00", shift_end="17:00"`）。用户改回正常值班并显式传 `shiftStart="08:00", shiftEnd="17:00"` 时，代码只在新值与旧值**不相等**时才清空 `periods_json="[]"`，导致传相同起止时间恢复单时段值班时，`periods_json="[]"` 纹丝不动，保存后依然是 0 小时休息。
- **正确做法**：“工作模式/结构化时段模式切换（如 `periods` 从非空/空数组切回 `None` 单时段）”本身就是一等语义变更；跨越模式边界时，所有显式传入的目标模式字段必须无条件参与目标模式的重算与旧模式字段清理，严禁拿旧模式的残存/汇总列做“同值免算”过滤。

### 77. 严禁“在流式生成器出口套一层局部 `seen` 去重跳过，却不推进底层流游标与剩余工作预算计数（Generator-Exit Deduplication Desynchronizing Stream Offsets & Budget Accounting）”
> 母题：O 语义走样

- **典型坏习惯**：
  - 组合优化/邻域搜索的底层流按未去重下标维护 `start = stream_offsets[slug]` 游标，并按 `candidate_count - decision_offset` 判断是否还有剩余工作、统计 `skipped_by_budget`；
  - 编码者为了去重，不在构建 `decisions_by_kind` 时源头去重，而是在消费端生成器 `profiled_decisions` 的 `yield` 前临时加一个局部 `seen` 集合写 `if key in seen: continue`——结果被 `continue` 跳过的重复项既不推进 `stream_offsets` 也不推进 `decision_offset`：下一轮从偏小的游标恢复时把已扫过的区间重扫一遍，且因 `decision_offset < candidate_count` 永远成立导致空转到轮次上限，最后还把已被去重跳过的项全部误计入 `skipped_by_budget`！
- **正确做法**：流式序列的去重必须在**序列构建期（源头）**一次性完成，使 `candidate_count`、恢复游标 `offset`、剩余工作判定与预算剪枝统计 100% 面对同一份已去重的序列；若必须在流式迭代期去重，被跳过项必须同步推进底层流游标与已处理计数。

### 78. 严禁“用 Python `or` 拼接可能为 `0` / `[]` 的有效缓存指标，导致合法零值被当成缺失而回退全量重扫（Falsy `0` / `[]` Coalescing Triggering Full Fallback Scan）”
> 母题：O 语义走样 · H 缓存失效

- **典型坏习惯**：
  - 候选评估阶段已经算好并缓存了未分配任务数 `metric_unassigned_count = 0` 和样本列表 `metric_unassigned_sample = []`；
  - 下游构建执行摘要时写 `unassigned_count = metric_unassigned_count or len(unassigned_all)`、`sample = metric_unassigned_sample or unassigned_all[:20]`——因为在最常见的 **100% 全部成功分配（`0` 条未分配、`[]` 空样本）** 场景下，`0` 和 `[]` 在 Python 中均为 Falsy，导致缓存 100% 被击穿，每次成功执行都白白遍历数千条任务重算一遍 `unassigned_all`！
- **正确做法**：判断可选缓存/指标是否已提供时，必须显式使用 `is not None`（`val if val is not None else fallback`），严禁对可能取 `0`、`0.0`、`""`、`()`、`[]` 的结果使用 `or` 短路。

### 79. 严禁“前置校验未触碰主库即失败，却遗留预设的终态标志把宿主锁死在永久 503，或在 `next()` 外层用 `except BaseException:` 误捕 `StopIteration`（Pre-Mutation Failure Leaving Fatal Host Lock & `BaseException` Catching `StopIteration`）”
> 母题：N 事务、锁与状态生命周期

- **典型坏习惯**：
  - **前置失败误锁死进程**：系统恢复在 `prepare_restore` 预写了 `restart_required=True` 并排空了 HTTP/Worker；随后在尚未触碰主数据库的前置检查阶段（如备份文件 `PRAGMA integrity_check` 失败，`database_origin="unchanged"`）被拒绝，返回给用户的消息写着“原数据库没有改动，请换一个备份再试”，但代码既没把 `restart_required` 置回 `False`，宿主又看到 `terminal=True` 直接切入 `"restart_required"` 永久 503 停机态，用户根本无法重试；
  - **释放锁/停机时把前置校验放在破坏性动作之后**：`release_runtime_lock` 先调用 `_release_lock_file` 删掉了壳锁，后半段才检查 `expected_database` 参数合法性并返回 `False`，造成“壳锁已删、库锁仍在”的半释放撕裂；`_finalize_stopped_runtime` 先强杀工作进程、删光 6 个运行时契约文件，最后才检查早已确定的 `status.get("db_lock_error")`；
  - **`except BaseException:` 误杀 `StopIteration`**：WSGI 响应包装器在 `__next__` 里写 `try: return next(self._iterator) except BaseException: self.close(); raise`——忘了 `StopIteration` 继承自 `Exception`/`BaseException`，导致每次正常迭代读完最后一个 chunk 时，在 WSGI Server 真正把数据写进客户端 Socket 前就提前触发了 `self.close()` 释放请求 Ticket；而 `TransactionManager._transaction_scope` 反过来只捕 `except Exception:` 漏捕 `GeneratorExit`（继承自 `BaseException`），导致流式请求断开时跳过回滚直接 `conn.close()`。
- **正确做法**：凡未触碰底层存储的前置失败必须对称复位预占状态，确保对外恢复指引与宿主真实可用态一致；一切参数与前置错误检查必须在执行文件删除/进程终止之前完成；在迭代器与事务上下文中必须精确区分 `StopIteration`（正常结束不触发异常关闭）与 `GeneratorExit` / `BaseException`（异常中断必须回滚事务）。

### 80. 严禁“路由层穿透调用 Service 私有方法在同事务内把重查询跑两遍，或对同一请求/配置对象跨层连环实例化转型多遍（Route-to-Service Double Execution & Multi-Pass Object Coercion）”
> 母题：G 重复计算 · F 重复校验

- **典型坏习惯**：
  - **同事务双倍查库**：日历导出预览路由 `calendar_export_preview` 在同一个 `with service.tx.transaction():` 里，第 1 行调 `request, total = service.preview_export(**arguments)`（内部已完整调了一次 `self._states_and_rows(...)` 查库并构建区间状态），紧接着第 4 行路由层直接穿透调用私有方法 `states, _ = service._states_and_rows(**arguments)` 把完全相同的日期解析、身份解析和日历数据库查询重跑第 2 遍；流程阶段确认 `workflow_stage_confirm` 先调 `reader.prepare_workflow`（内部已调 `normalize_workflow_preview_input` 和 `self.detail(ref)`），下一行又调 `domain.normalize` 重跑一遍 `normalize_workflow_preview_input`、再调一遍 `reader.detail(ref)`；
  - **单次请求构造 4~6 次 `PageRequest`**：路由调 `scope_input`（内部 `normalize_scope` 实例化第 1 次 `CatalogPageRequest`，返回时再实例化第 2 次），传进 `service.export_rows` 后内部又调 `CatalogPageRequest(**normalize_scope(scope))`（实例化第 3、4 次）；
  - **配置快照双遍全字段 `coerce_config_field`**：`ensure_runtime_config_snapshot` 在 `strict_mode=True` 时先调 `_validate_present_runtime_cfg_fields(cfg)` 遍历所有配置项调一遍 `coerce_config_field(..., strict=True)` 并把转型结果全部丢弃，紧接着下一行再遍历所有配置项调第 2 遍 `coerce_config_field`，随后把已经转型校验过非负的 3 个 `float` 权重传给 `normalize_weight_triplet` 再跑一遍字符串解析与 `< 0` 校验；
  - **前端时间轴/图表高频重算**：时间轴组件 6 处调用 `TimelinePresentation.axis(...)` 和 `ExecutionTrendChart` 的曲线分段计算均未加 `useMemo`（导致鼠标 `hover` / 游标移动每帧重算数十次 `new Date().toISOString()` 和全量线段切分）；`<CandidateTimelineView>` 切换候选方案时漏绑 `key={shown.candidate.candidate_ref}` 导致缩放/视图状态缓存跨候选污染；`useQuery` 在用户点“取消读取（`paused=true`）”使 `enabled=false` 时仍返回 `previous.result` 导致“读取已取消”提示与旧数据表格同屏残留；`ValidationContract.result` 对同一个 `d.tasks` 数组散装连跑 12 遍遍历并相隔 18 行重复创建两次一模一样的 `new Set(d.tasks.map(row => row.task_ref))`。
- **正确做法**：Service 公开方法一次性返回路由所需的完整元组 `(request, total, states)`，严禁路由层穿透调用 `_` 私有方法重查一遍；入口边界归一化后的强类型 `PageRequest` / `PreparedImportSource` / `Config` 直接向下传递，严禁跨层重复实例化与双遍 `coerce`；前端高频交互（`hover` / `pointerMove`）与静态时间轴/曲线几何计算必须通过 `useMemo` 隔离，候选组件切换必须绑定身份 `key`，`useQuery` 取消/禁用时不得残留旧结果，契约校验对大数组必须单遍扫描合并计数与索引构建。

### 81. 严禁“伪继承仓储桩用单值字典吞掉重复行、外层与底层缓存脑裂、复合枚举不用能力谓词 `supports_mode`、以及用 `canonical_json` 比较原生标量（Fake-Subclass Dict-Collapse, Split-Brain Cache & Raw Enum Equality）”
> 母题：O 语义走样 · H 缓存失效 · J 口径没在源头统一

- **典型坏习惯**：
  - **伪继承仓储桩 + 列表转单值字典吞掉重复行**：为了在内存快照上复用领域服务，定义 `class _ProfileFacts(MemberQualificationRepository)`（不调 `super().__init__`）并猴子补丁 `service.repo = _ProfileFacts(facts)`，且在 `__init__` 里写 `self.profiles = {row["member_id"]: row for row in facts["qualification_profiles"]}`——把同一成员的重复登记行静默压成 1 条，导致领域层 `if member_id in qualifications: raise` 的重复脏数据校验被 100% 绕过；
  - **外层派生缓存与底层按需加载缓存脑裂**：`ResourceChangePreparation.__init__` 派生了一份 `self._identities = {row["ref"]: row for row in facts.identities.values()}`；当先调 `find_identity(kind, code)` 触发 `self.facts.load_identity_kinds([kind])` 时，`facts.identity_kinds` 已加入 `kind`，但 `self._identities` 未更新，紧接着调 `find_by_ref(kind, ref)` 看到 `kind in self.facts.identity_kinds` 直接跳过更新并返回 `None`；
  - **复合枚举（`category == "both"`）用原始值交集代替 `supports_mode`**：下游用 `supports_mode(cat, "internal")` 触发配额计算并在失败时写入 `self._failed["internal"]`，上游汇总告警却写 `{row["category"] for ...} & set(self._failed)`，导致筛选结果仅含 `"both"` 类别时交集恒为空，静默吞掉 `resource_availability_unavailable` 告警；
  - **热循环用 `canonical_json(a) == canonical_json(b)` 比较 `str`/`bool`/`list[str]`**：2,000 行导入对每个单元格调用 `canonical_json` 触发近 10 万次无意义的 `json.dumps`；
  - **未变更的空关联列表无条件翻转声明位**：历史成员 `permissions_declared == 0` 且 `roles == []`（享受默认授权回退资格），编辑备注时表单原样带回 `role_refs: []`，代码未判断角色集合是否变更就无条件写 `profile["permissions_declared"] = 1`，静默剥夺其全部默认访问资格。
- **正确做法**：内存适配桩必须保留完整多行列表（`defaultdict(list)`）以维持下游重复行校验；二级索引必须内聚在唯一事实源对象内随 `load_*` 原子更新；复合枚举能力判定必须全链路统一使用 `supports_mode`；原生标量与简单列表直接用 `type(a) is type(b) and a == b` 比较；状态标志位（如 `permissions_declared`）仅在实际发生关联变更或显式声明时更新。

### 82. 严禁“生成器在构造重型对象后、`yield` 前做无界去重，导致上游基于 `yield` 计数的 `limit` 失效并在重复项上发生 $O(N^2)$ 重型构造（Unbounded Pre-Yield Deduplication Breaking Upstream Consumer Limit）”
> 母题：E 先算后扔

- **典型坏习惯**：
  - 邻域搜索生成器内部先调 `_build_candidate_dict(order)` 对 5,000 个元素做全量复制与映射构造，然后再检查 `if signature in seen_signatures: continue`，只有未见过的才 `yield`；
  - 上游调用方写 `for move in iter_moves(...): count += 1; if count >= limit: break`——当候选序列存在大量等价排列时，生成器在 `continue` 分支里永不 `yield`，上游 `count` 永远涨不到 `limit`，导致生成器把整张 $O(N^2)$ 邻域表成千上万个重复项全部构造并哈希一遍才结束！
- **正确做法**：① 必须在构造重型候选对象**之前**用轻量级置换签名（或交换前后元素相等性 `order[i] == order[j]`）直接判定等价并跳过；② 搜索配额/尝试上限 `attempt_limit` 必须约束总生成尝试次数（或在源头直接生成无重复邻域序列），严禁让无界 `continue` 绕过上游 `limit`。

### 83. 严禁“去重缓存命中（`duplicate=True`）后仍无条件对 5,000 个结果对象执行全量扫描、排序、反推映射与排行榜插入（Unconditional Post-Cache-Hit $O(N)$ Result Scanning & Ranking）”
> 母题：H 缓存失效

- **典型坏习惯**：
  - 解码会话命中去重缓存（`outcome.duplicate == True`）返回已评估过的 `entry`；
  - 调用方拿到 `outcome` 后，不区分是否命中缓存，无条件对 `entry.results`（5,000 个结果对象）重新跑一遍全量扫描反推 `group_order`、重新校验优先级覆盖率、重新调用归档器的 `consider(entry)` 触发 Pareto 支配比较与 `sort()`！
- **正确做法**：缓存命中的 `entry` 在首次解码时已完成归档与校验，且其派生特征（如 `group_order`）应随缓存条目一次性挂接；`duplicate == True` 时除必要的统计计数外应直接短路跳过一切重算与重复归档。

### 84. 严禁“在列表/详情查询中先对全量数据做重型展示投影与导出专属字段计算，最后才执行筛选谓词（Project-All-Then-Filter & Eager Export-Only Field Computation）”
> 母题：E 先算后扔

- **典型坏习惯**：
  - 页面筛选某个分组/节点/状态（仅命中 20 行），代码却先对全量 5,000 行记录无条件执行 `project_item`（含 5 次 ISO 时间解析、跨表标签拼装、以及仅在导出 CSV/XLSX 时才用到的 12 个 `export_review_labels` / `legacy_review` 字段与 10,000 个 `ResourceIdentity` 实例），然后再调 `_selected(item, scope)` 丢弃掉 4,980 行；
  - 单条详情查询 `project_detail(ref)` 先线性扫描全量列表找到 `projection`，再线性扫描第二遍 `zip(rows, labels)` 找到对应行。
- **正确做法**：筛选谓词必须下推到最轻量的原始事实层先行过滤，只对命中的目标子集执行展示投影；仅导出路径消费的标签与兼容字段必须延迟到导出函数中按需计算；单点详情查询必须使用 $O(1)$ 索引定位。

### 85. 严禁“将整份派生报表/视图字典塞入快照指纹做全量 JSON 序列化，或把正常业务边界状态混入数据损坏降级收集器（Full-Payload Snapshot Fingerprinting & Normal-Edge-Case-as-Corruption Degradation）”
> 母题：H 缓存失效 · O 语义走样

- **典型坏习惯**：
  - 乐观读快照 `bind_read_snapshot` 不绑定决定视图不变性的源头版本/修订号，而是把计算出的整份报表结果字典（含数百 KB 派生明细与 10 个无人读取的僵尸降级样本字段）塞进 `fingerprint` 做全量递归 `json.dumps` 并驻留内存 Token 池；
  - 把“资源在查询窗口内非工作/无可用配额（`zero_capacity_window`）”或“两段重叠维护窗口正常取并集（`maintenance_overlap_merged`）”塞进用于标记脏数据丢弃的 `DegradationCollector`，导致 `report_degraded=True`，前端向用户谎报“有一部分原始数据不可用，没有参与本表计算”。
- **正确做法**：快照指纹只绑定最小源头事实版本与查询边界，严禁序列化派生大表与只写不读的僵尸字段；正常业务边界状态（配额为 0、区间合并）必须与原始数据损坏/丢弃严格解耦，严禁混用同一个降级标志误导用户。

### 86. 严禁“自掘坟墓式”持久化自锁（窄白名单把明确未提交拒绝误判为‘结果未知’导致 `PENDING_KEY` 永久死锁，或把瞬态锁冲突写入致命存储错误状态）
> 母题：N 事务、锁与状态生命周期

- **典型坏习惯**：
  - 前端为防超时重放先将 `request_key` 写入 `localStorage`（`PENDING_KEY`），却规定**只有**命中极窄的 `(HTTP 状态码 × 错误码)` 白名单才算“明确未提交（`rejected`）”并允许清锁；哪怕后端响应体已明确返回 `committed: false`（如预检后选中实体被删返回 `404 entity_not_found`，或准入守卫返回 `503`），只要状态码/错误码漏进前端白名单，前端就“防御性”假定 `committed: "unknown"` 死活不清锁，后续轮询 `/requests/<key>` 永远返回 `found: false`，导致整个功能区永久变砖（除非清浏览器缓存）；
  - 或者当遇到 Web Locks 并发锁占用（`!lock`）等瞬态冲突时，将其写入代表“本地存储损坏”的致命状态（如 `storageError`），而后台活动任务轮询 `useEffect` 一见 `storageError` 就直接 `return` 罢工，把正在正常运行的任务轮询也一并掐死。
- **正确做法**：只要后端契约明确返回 `ok === false && committed === false` 且非 `request_key_conflict`（即该 `request_key` 本身未在服务端冲突在跑），前端必须将其识别为明确未提交（`rejected`）并立即释放本地 `PENDING_KEY`；瞬态操作冲突必须与底层存储损坏状态严格分离，绝不能因操作提示掐断后台轮询。

### 87. 严禁“连坐式”按钮/面板终生软禁（不区分 `pending`/`done`/`rejected`/`stale` 一刀切焊死控件，或把唯一解锁回调放在可能不挂载的子组件里）
> 母题：N 事务、锁与状态生命周期

- **典型坏习惯**：
  - **报错/成功后永久灰钮或锁面板**：为了防重复点击，写 `disabled={command.status !== 'idle'}`，而在输入框 `onChange` 时只在 `stale`（快照过期）下才调用 `command.reset()`；结果一旦填错字段触发后端 `400` 校验失败或 `409` 冲突（非 `stale` 的 `rejected`），哪怕用户把输入框改对了，保存按钮也永远是灰的；或者完成一次子面板操作后 `command.status === 'done'`，却把 `'done'` 也算进 `locked` 导致整个面板输入框与按钮永久锁死；
  - **前端守卫比后端领域契约更死板，堵死后端专门留出的修复通道**：后端已允许对非 `'active'` 的历史异常状态记录执行“取消清理”（只要 `status !== 'cancelled'`），前端行按钮却死防 `disabled={row.status !== 'active'}`，把异常数据的唯一清理入口堵死在 UI 之外；
  - **把唯一解锁钥匙丢进条件挂载的子组件黑洞**：父组件在“保存并继续”时设了 `nextDraft` 并用 `blocked = ... || !!nextDraft` 锁死全页筛选、刷新和行点击，却把唯一清除 `nextDraft` 的 `onContinueReady` 放在 `<Detail>` 子组件成功加载 `task` 之后；一旦首次提交使该任务状态变化离开当前“待处理”筛选集（`selectedTask` 为空，`<Detail>` 根本不挂载）或详情查询报错（如 `snapshot_stale`），`nextDraft` 就永远无法清除，整张页面当场变砖。
- **正确做法**：严格区分 `pending`（请求中禁用）、`done`（重置表单或消费结果后恢复 `idle`）、普通 `rejected`（用户修改字段或重选文件时立即 `command.reset()` 解锁重试）与 `stale`（引导刷新）；前端操作入口必须与后端领域状态机对称；任何父级阻塞锁（如 `nextDraft`）必须在父级或子组件的“列表未命中 / 请求报错 / 快照失效”全分支都有确定性的兜底释放出口。

### 88. 严禁“宁错杀不放过”的跨界连带通杀（运行期单任务恢复调全表通杀函数、恢复宿主未判自身状态自杀式清理、关闭态弹窗后台触发脏守卫）
> 母题：N 事务、锁与状态生命周期

- **典型坏习惯**：
  - **单任务异常恢复误杀排队中的无辜任务**：本该只在**进程启动时**回收孤儿任务的 `recover_unfinished_runs()`（无参扫描全表未完成任务标为 `interrupted`），被运行期单任务 `_run_one(run_ref)` 的异常处理无参调用，导致任务 A 失败恢复时把内存队列 `self._pending` 里正在排队等待的合法任务 B、C 在数据库里全表通杀标成 `interrupted`；
  - **恢复宿主被并发只读请求“清理门户”**：系统恢复宿主在每个请求入口无脑调 `_stop_unconfirmed(None)` 清理未确认恢复单，却未在锁内检查自身是否正处于 `draining`/`restoring` 状态，导致后台正在执行恢复时被路过的普通查询或维护调用中途掐死；
  - **幽灵脏守卫拦截切页**：弹窗处于关闭状态（`!open`），或后台 `lookup()` 自动清除了过期的 `saved` 记录却忘了同步重置 `draft = empty()`，导致 `useDirtyGuard` 在后台静默判定 `dirty: true`，用户没开任何弹窗却切不了页面。
- **正确做法**：异常恢复与清理函数必须显式绑定目标范围（进程启动传 `run_ref=None`，单任务失败传 `run_ref=failed_ref`）；状态机宿主的一切清理动作必须在互斥锁内先核对自身当前生命周期状态；前端离开守卫 `dirty` 必须严格受 `open` 与有效草稿生命周期约束，清理持久化状态时同步清空内存草稿。

### 89. 严禁“步步验钞”式热路径防御税与“无未来消费的祖先状态全量克隆”（不信上游契约在渲染/循环内万次重验，或 DAG 拓扑传递不收敛代表元）
> 母题：F 重复校验 · G 重复计算

- **典型坏习惯**：
  - 在 60fps 滚动/悬浮渲染或万行循环内，不复用 Model 层已算好的 `shownRefs`（`Set`）和 `locations`（`Map`），每帧自己重新 `new Set(model.items.map(...))`、`rows.findIndex(r => r.items.some(...))`、用 `new Date(text)` 校验数万个 ISO 字符串、用 `JSON.stringify` 比对固定短数组，甚至对默认折叠隐藏的 `<details>` 内 5,000 个关键链节点每滚一帧全量重算一遍虚拟 DOM；
  - 在 DAG 拓扑序校验（如跨阶段种子序校验 `validate_stage_seed_precedence`）中，为了防后续节点万一还有同组 `stage_seeds`，不看该分组的最大 seed 序号 `max_seed_seq` 是否早已 `<= current.sequence`，无脑把该组所有未合并历史祖先在每条出边上全量克隆传递，在宽分叉合流图上引发指数级状态爆炸。
- **正确做法**：边界校验只做一次，Model 层预构建的 `Set`/`Map` 索引直接暴露给 View 层 $O(1)$ 消费，折叠容器惰性挂载并用 `useMemo` 隔离滚动坐标更新；图/DAG 状态传递在当前节点之后已无同组未来消费点时，立即坍缩为单一代表元（`max_forwarded = 1`），保持 $O(N)$ 线性复杂度。

### 90. 严禁“字面量死磕”式脆弱探针与“归一化前裸比变更”（正则误匹配触发器 `BEGIN`、位置触发器配无列名 `INSERT`、未归一化原值直接 diff）
> 母题：O 语义走样

- **典型坏习惯**：
  - **把触发器 `BEGIN` 当事务 `BEGIN;`**：`build_schema_exec_script` 为“防御”脚本自带事务，用宽泛正则搜 `BEGIN`，结果把 `CREATE TRIGGER ... BEGIN ... END;` 里的 `BEGIN` 误判为顶层事务语句，反而跳过了给 `schema.sql` 包裹 `BEGIN; ... COMMIT;`；
  - **位置型触发器配省略列名的 `INSERT` 且不验物理列序**：老库迁移表与新库建表的物理列顺序可能不同，代码一边建依赖固定列位置的触发器，一边在迁移/仓储里写省略列名的 `INSERT INTO Table VALUES (...)` / `SELECT *`，还不校验物理列序；
  - **在归一化前拿原始输入与库值“裸比”**：表格批量导入为了拦截用户修改只读列，在调用 `normalize_input` 之前直接拿原始单元格值跟数据库值比对，导致首尾空格或等价格式差异被误判为“篡改只读列”而整行报错。
- **正确做法**：SQL/Schema 探针必须匹配真实的语法与结构语义（顶层 `BEGIN...;` 语句、联合唯一键列集合、触发器依赖表的精确物理列序 + 显式列名 `INSERT`）；一切“字段是否变更”的 diff 判定必须在双方完成同口径归一化（Normalization）之后进行。

### 91. 严禁“好心办坏事”的读取侧 `.strip()` 身份坍缩与兄弟记录串线覆盖（Read-Side Whitespace-Trimming Identity Collapse & Sibling Hijacking）
> 母题：O 语义走样

- **典型坏习惯**：
  - **按 `ref` 定位后再 `.strip()` 串改兄弟记录**：数据库里既有历史带首尾空格的记录（如成员 `" W1 "`、资源 `" R1 "`、任务组 `" B1 "`、项目 `" P1 "`），又有去除空格后的正常同名记录（`"W1"`、`"R1"`、`"B1"`、`"P1"`）。上层明明已经通过永久引用 `identity.entity_key` 精确拿到了 `" W1 "`，底层 `CalendarEngine` / `CalendarAdmin._require_worker_id` 却“防御性”调 `normalize_text(worker_id)` 把首尾空格 `.strip()` 掉，导致查询、保存或删除 `" W1 "` 的个人日历时静默串线读写到 `"W1"` 头上；
  - **原样导出再回导时先 `.strip()` 再查库覆盖兄弟记录**：表格导入 `_source` 先把 Excel 里的 `" B1 "` `.strip()` 成 `"B1"` 再去 `self.existing.get("B1")` 匹配，导致原样导出 `" B1 "` 后回导直接命中并覆盖 `"B1"`；
  - **依赖图算法对持久化身份 `.strip()` 制造假前驱**：关键链 `_build_nodes` 对已落库的 `group_id`、`unit_id`、`resource_id`、`worker_id` 执行 `str(val or "").strip()`，把不同任务组 `" B1 "` 与 `"B1"` 合并进同一流程分组、把 `" R1 "` 与 `"R1"` 合并进同一资源分组，凭空捏造出虚假的流程/资源前驱边。
- **正确做法**：**“写入口严防、读与引用侧保真”**——新增/改名入口在边界一次性拦截首尾空白；一旦记录已持久化在库中，按 `ref` 解析、按业务键分组、构建图节点以及读写关联表时，**必须 100% 保留非空白原生字符串原值**（仅将 `None` 或纯空白归为空），文件导入预检在 `raw != code and raw in existing` 时必须显式拒绝，绝不静默 `.strip()` 投奔兄弟记录。

### 92. 严禁“只防老表、漏防新表”的跨代际半截子引用/孤儿守卫（Half-Baked Dependency & Orphan Guards Across Schema Generations）
> 母题：O 语义走样 · K 吞错与静默降级

- **典型坏习惯**：
  - **删除依赖目录漏计新账本引用**：资源删除依赖表 `DEPENDENCIES["resource" / "worker"]` 只统计了旧表 `LegacyExecutionEvents`，报错文案写着“还被任务、授权、日历或历史记录使用”，却漏掉了新版执行记录表 `ExecutionReportRevisions`（`actual_resource_ref` / `actual_worker_ref`）；结果只被新版记录引用的资源/成员被顺利软删除（`EntityRefs.active` 变为 `0`），事后维护该记录时抛 `409 资源或成员已失效`，重跑全局计划时抛 `execution_ledger_actual_resource_unavailable` 把整个调度链路锁死；
  - **`LEFT JOIN` 静默吞掉无修订的孤儿主表头**：读取记录时写 `FROM ExecutionReports p LEFT JOIN ExecutionReportRevisions v ...`，遇到只有主表头、没有任何修订行的损坏孤儿记录（`v.revision_ref IS NULL`）直接 `continue` 假装没看见，导致 `ledger_reader` 漏报存储损坏（`storage_failure`）。
- **正确做法**：凡引入新一代账本/引用表（如基于 `ref` 的 `ExecutionReportRevisions`），资源删除依赖统计、页面删除能力守卫与批量删除预检必须同步纳入新表的当前有效引用；主从表结构不变量（如“每条主记录头必有至少一条修订”）必须在读取投影层显式核验，绝不用 `LEFT JOIN + continue` 掩耳盗铃。

### 93. 严禁“流式截断前让 SQL 无索引全表 `ORDER BY`”与“搬到 Python 排序/序列化却被历史异常类型（`str` vs `bytes`）炸穿”（Pre-Cap SQL Sort Bomb vs. Python Mixed-Type `sort()` / `json.dumps()` Crash）
> 母题：E 先算后扔 · O 语义走样

- **典型坏习惯**：
  - **容量截断前的 SQL 临时 B-Tree 炸弹**：流式读取函数 `_collect` 本意是在读到第 `MAX_DIRECTORY_ROWS` 行或单字段超 `MAX_DOCUMENT_BYTES` 时立即中止抛 `413`；但底层仓储 SQL 却写了无索引的 `ORDER BY committed_at_utc, request_key`，迫使 SQLite 在返回第 1 行之前先把全表所有超大 JSON 行全部物化进临时 B-Tree 排序，让流式早停形同虚设；
  - **搬到 Python `.sort()` 后未兼容降级契约的混合类型**：把排序移到 `_collect` 之后写成 `receipts.sort(key=lambda r: (r["committed_at_utc"], r["request_key"]))`，忘了同模块明确承诺将非字符串异常时间（如损坏的 BLOB `bytes`）降级标记为 `invalid_stored_time` 缺口——结果还没走到降级转换，Python 元组比较 `str < bytes` 就直接抛出 `TypeError` 把整个目录查询炸成 `500`；
  - **降级后仍把原始 `bytes` 塞进公开 HTTP JSON 响应**：修好了 `.sort()` 并记录了 `invalid_stored_time`，但响应投影层仍写 `"times": dict(descriptor["times"])` 把原始 `b"bad-time"` 原样带进公开响应字典，到了 API 响应出口调用 `json.dumps` 时照样炸 `500 TypeError`。
- **正确做法**：有容量上限的目录查询在 `_collect` 截断后再做 Python 排序；凡下游定义了异常类型降级契约（如 `invalid_stored_time`）的字段，Python 排序键必须使用类型安全的分组元组（如 `(0, val, ...)` vs `(1, repr(val), ...)`），且公开 HTTP 响应投影必须将非合法类型过滤为 `None`（仅在私有快照 `state` 中保留原值供签名绑定）。

### 94. 严禁“在类型校验前用 Python `==` / `!=` 过滤未变更字段，让布尔值 `True`/`False` 伪装成数字 `1`/`0` 蒙混过关”（Pre-Validation `==` Diffing Allowing `bool` to Masquerade as `1`/`0`）
> 母题：O 语义走样

- **典型坏习惯**：
  - 库存或批量数据导入在调用 `normalize_input`（内含严格类型校验 `type(v) in (int, float)`，明确拒绝 `bool`）之前，先写推导式过滤未修改字段：`fields = {k: v for k, v in values.items() if v != raw[k]}`；
  - 因为 Python 中 `bool` 是 `int` 的子类，`True == 1`、`True == 1.0`、`False == 0`、`False == 0.0` 全部为 `True`！当数据库里现有库存恰为 `1` 或 `0`，而用户上传的 XLSX 单元格里填了布尔值 `TRUE` 或 `FALSE` 时，`v != raw[k]` 算出来是 `False`，该字段被当成“原样未改”直接过滤丢弃，100% 绕过了后续的布尔类型拒绝！
- **正确做法**：在类型归一化之前做“未变更字段过滤”时，必须核对运行时类型一致（如 `type(v) is type(raw[k]) and v == raw[k]`，或显式拦截 `type(v) is bool or v != raw[k]`），严禁在未验明正身前对可能混入 `bool` 的数值字段裸用 Python `==` / `!=`。

### 95. 严禁“用顶层 `OR` 跨表/跨列条件废掉 SQLite 索引”与“零时长点任务被 `end <= start` 一刀切误判为无效时段”（Index-Killing SQL `OR` & Zero-Duration Point-Task Rejection）
> 母题：G 重复计算 · O 语义走样

- **典型坏习惯**：
  - **顶层 `OR` 废掉主键/外键索引引发百倍回退**：在 1,000 行流程模板导入循环调用 1,500 次的查询里写 `SELECT ... FROM SharedGroups g WHERE g.project_code=? OR EXISTS (SELECT 1 FROM ProjectTasks t WHERE t.project_code=? AND t.shared_group_id=g.group_id)`——顶层 `OR` 导致 SQLite 放弃 `project_code` 索引退化为 1,500 次全表索引扫描，导入耗时暴涨 `+174%`；
  - **把合法零时长点任务当成缺失时段拦截**：任务配置为合法的零耗时点任务（`setup_hours == 0` 且 `unit_hours == 0`），执行记录登记了合法的零时长完成（`actual_start == actual_end` 且 `effective_hours == 0`）；执行账本守卫 `execution_ledger_guard` 却一刀切写 `if end <= start: reasons.append("execution_ledger_actual_interval_missing")`，把合法点任务记录当成坏数据拦截，导致后续所有重算被拒绝；或者同一个点任务混合了零时长记录与正时长记录分段时，主资源提取直接取 `segments[0]`（零时长分段无资源），漏取正时长分段的实际占用资源。
- **正确做法**：跨路径 SQL 条件优先拆分为各自走索引的 `UNION` 子查询；区间合法性校验必须显式识别零耗时点任务契约（点任务允许 `end == start` 且零耗时，正时长分段优先提取主占用资源）。

### 96. 严禁“跨层靠给仓储实例临时塞属性再 `finally` 置空来隐式传参”与“在循环里反复调 DDL 字典构造函数只为拿固定对象名集合”（Implicit Attribute Smuggling on Repo & Per-Iteration DDL Dict Reconstruction）
> 母题：I 偷偷传状态 · G 重复计算

- **典型坏习惯**：
  - **临时属性走私参数**：为了把快照里已查出的 `report_revisions` 传给 `repo.references_many(kind, codes)`，不在方法签名上加显式参数，而是在调用前写 `repo.report_revisions = self.facts.report_revisions`、调用后在 `finally: repo.report_revisions = None` 清空，把无状态的 Repository 实例变成靠隐藏时序耦合的有状态泥球；
  - **循环内重复构造整套 DDL SQL 字典**：`schema_state()` 只想判断 `memo.structure.objects` 里有没有模板血缘表名，却每次调用 `template_lineage_objects().keys()`——该函数每次都会用 f-string 重新拼接整套 `CREATE TABLE`、`CREATE INDEX` 和 `CREATE TRIGGER` 的长篇 SQL 字典；在逐项复制流程步骤的循环里调 $N$ 次 `require_ready()` 就白白拼装 $N$ 套完整 DDL。
- **正确做法**：跨层传递已捕获的数据或缓存必须通过显式方法参数（如 `*, report_revisions=None`）；固定 Schema 对象名集合应在模块加载期提取为不可变常量（如 `_LINEAGE_OBJECT_NAMES = frozenset(template_lineage_objects())`），运行期直接做 $O(1)$ 集合求交。

### 97. 严禁“在逐行/逐步骤循环内反复重建外层共享诊断列表或重复 `sorted()` 同一关联字典”以及“删了冗余白名单却留着吹嘘白名单的过时注释”（In-Loop Shared List Filtering / Dict Sorting & Stale Defensive Docstrings）
> 母题：G 重复计算 · C 清理不彻底

- **典型坏习惯**：
  - **循环内 $O(N^2)$ 重建外层列表/排序字典**：流程模板预检遍历 2,000 个保留步骤时，每个步骤都在 `_preview_retained` 里写 `result["diagnostics"] = [d for d in result["diagnostics"] if ...]` 把外层整份诊断列表重筛重建一遍（200 万次检查）；成员与资源绑定关系导入对每条 `is_primary == "yes"` 的行都调一遍 `sorted(links.items())` 扫描该成员的全部现有关联；
  - **代码已去防御，注释还在吹防御**：仓储层的冗余列名白名单校验早已收敛回上游服务层删除，但仓储模块顶部的 docstring 还赫然写着“列名由本模块/本仓储内的白名单展开”，误导后续维护者以为仓储层仍在做白名单兜底。
- **正确做法**：循环内只收集需要过滤的序号集合（如 `retained_sequences`）或在循环前按分组一次性预计算有序列表（如 `replaced_by_member`），循环外做唯一一次过滤/排序；清理冗余防御代码时，必须同步更新模块 docstring 与注释，确保文档描述的职责边界与真实代码 100% 一致。

### 98. 严禁“稀疏导入把显式清除标记 `\N`（`key: None`）与‘留空未填（省略 `key`）’混同为保持原值”（Explicit Clear Sentinel `\N` Masquerading as Omitted Blank Cell）
> 母题：O 语义走样 · J 口径没在源头统一

- **典型坏习惯**：
  - 表格解码器对空白单元格直接省略不写入 `values` 字典，只有用户显式填写 `\N` 请求清除时才写入 `values[col] = None`；
  - 下游解析/比对却用 `values.get(col) in (None, before[col])` 或 `values.get(col) or before[col]`，把不可空列（如 `skill_level`、`is_primary`）上的显式 `\N` 清除指令当成“留空未填 = 保持原值”，静默吞掉错误并判为 `unchanged`。
- **正确做法**：严格区分 `col not in values`（留空未填，保持原值）与 `col in values and values[col] is None`（显式 `\N` 清除）；对不可空列收到显式 `None` 时必须在解析入口直接报错拒绝。

### 99. 严禁“绕开领域分组归并函数，对多事件账本逐条裸检单行字段”（Per-Row Raw Field Check Bypassing Group-Level Domain Resolver）
> 母题：J 口径没在源头统一 · O 语义走样

- **典型坏习惯**：
  - 历史执行事件组（`legacy_facts`）约定开始事件记录实际执行资源与成员，后续暂停/完成事件可省略为 `None`，领域层已提供唯一归并函数 `legacy_resource_refs(rows, field, gaps=...)` 按整组解释资源；
  - 下游冲突索引 `_actual_index` 却把 `legacy_facts` 当成单行独立记录，逐条检查 `row.get("actual_resource_ref") is None`，把所有合法省写资源的完成事件全部误判为 `execution_resource_unknown`。
- **正确做法**：多事件账本或分组事实的语义解释必须 100% 调用领域层唯一归并函数，严禁下游绕开归并函数逐行裸检原始事件字典。

### 100. 严禁“在存量结构守卫之前先对未校验行跑 `sorted()` / 推导，让 `TypeError` 抢跑炸穿结构化诊断”（Sorting/Projecting Unverified Stored Rows Before Structure Guard）
> 母题：K 吞错与静默降级 · O 语义走样

- **典型坏习惯**：
  - 流程模板文件预检 `_route` 本应像表单入口一样先调 `_check_template(code)` 校验存量步骤模板（当存量 `seq` 损坏为 `None` 时抛结构化 `422 template_invalid` 转行级错误），却把 `_parse_route` 和 `_route_summary`（内部执行 `sorted(existing, key=lambda r: r["seq"])`）放在 `_check_template` 之前，导致遇到损坏存量 `seq` 时在 `sorted()` 中直接抛未捕获 `TypeError` 炸穿整批预检。
- **正确做法**：读取存量记录参与比对或排序前，必须先执行存量结构守卫（如 `check_workflow_template`），再对验证通过的记录执行排序、解析与摘要计算。

### 101. 严禁“建好了批量预取缓存方法，分页/列表循环前却漏调预取退化为逐行 N+1”（Available Batch Prefetch Omitted Before Page Loop）
> 母题：G 重复计算 · H 缓存失效

- **典型坏习惯**：
  - 仓储与引用层早已提供按版本列表一次查库的 `role_rows(versions)` 和 `prefetch_history_rows(versions)` 批量预取缓存；但目录分页 `_catalog_page` 在拿到当前页 20 个 `heads` 后直接进循环调单条读取，导致每页白白触发 20～40 次逐版本 N+1 查询；
  - 或者缓存字典在某版本查询结果为空列表 `[]` 时未记录“该版本已查过”，导致后续访问同一版本时因空列表被判为未命中而反复回源查库。
- **正确做法**：分页与列表组装进入逐项循环前，必须先收集本页全部关联键调用批量预取方法；缓存命中判定必须以 `key in cache`（而非值的真假性）为准，空结果同样缓存。

### 102. 严禁“在推导式里把业务过滤条件写在字段合法性校验前面，让被过滤掉的存量/未来记录绕过校验”（Generator `if` Filter Short-Circuiting Field Validation）
> 母题：O 语义走样 · J 口径没在源头统一

- **典型坏习惯**：
  - 写 `sum(quantity(item["quantity"], positive=True) for item in row["supplies"] if supply_day(item["supply_date"]) <= cutoff_day)`：Python 先求值 `if supply_day(...) <= cutoff_day`，再求值前面的 `quantity(...)`；
  - 结果未来日期（`supply_day > cutoff_day`）的补给记录一旦带有非法数量（如负数或损坏字符串），在保存路径上被 `if` 短路跳过而顺利蒙混过关，等到执行读取路径（`stage_availability`）全量校验所有补给记录时当场炸毁。
- **正确做法**：所有存量/输入记录的字段合法性校验必须对全量集合无条件执行（先逐条校验 `supply_day` 与 `quantity`，再按业务窗口过滤累加），严禁让业务子集过滤条件短路掉全量数据不变量校验。

### 103. 严禁“`json.loads(..., object_pairs_hook=...)` 外层用 `except ValueError:` 误吞 hook 抛出的业务拒绝异常”（`except ValueError` Swallowing `ValueError`-Subclassed Domain Rejection in `object_pairs_hook`）
> 母题：K 吞错与静默降级

- **典型坏习惯**：
  - `object_pairs_hook=unique_object` 在检测到重复 JSON 键时抛出继承自 `ValueError` 的领域拒绝异常（如 `CommandRejected("invalid_input", "...有重复列...")`）；
  - 外层却写 `except (ValueError, TypeError) as exc: raise CommandRejected("invalid_input", "...内容读不出来...")`，把内层已经精确指明“有重复列”的领域拒绝异常当成普通 JSON 语法错误吞掉并改写成笼统文案。
- **正确做法**：当 `object_pairs_hook` 或解析回调会抛出继承自 `ValueError` 的领域异常时，外层应只捕获 `json.JSONDecodeError`（或先 `except CommandRejected: raise`），绝不用宽泛 `ValueError` 吞掉内层专属诊断。

### 104. 严禁“靠 `g.request_key` 是否已设置来反推请求是不是写操作”以及“未校验 `request_key` 格式就先塞入上下文 `g`”（Inferring Write Role from `g.request_key` or Binding Unvalidated `request_key`）
> 母题：N 事务、锁与状态生命周期 · O 语义走样

- **典型坏习惯**：
  - 试图用 `is_write = g.get("request_key") is not None` 判断未知异常时该返回 `committed: "unknown"` 还是 `committed: False`——结果写接口一旦在绑定 `g.request_key` 之前（或异步任务受理接口未绑定该键而在响应编码时）抛未预期异常，就被误标为 `committed: False`；
  - 或者写接口在调 `validate_request_key(body["request_key"])` 之前就先写 `g.request_key = body["request_key"]`，导致格式非法的 `request_key` 残留进错误回执链接。
- **正确做法**：端点的读写职责由路由装饰器显式声明，不靠上下文副作用反推；`request_key` 必须先通过 `validate_request_key` 校验合格后，才写入 `g.request_key`。

### 105. 严禁“缓存异常实例后在循环或多处直接裸 `raise cached_exc`，导致 `__traceback__` 栈帧无限叠加并拖住局部帧内存”（Re-raising Cached Exception Instance Without `with_traceback(None)`）
> 母题：H 缓存失效 · N 事务、锁与状态生命周期

- **典型坏习惯**：
  - 将首次求值失败的业务异常实例缓存在属性或字典中（如 `self._global_error = exc` 或 `cache[key] = exc`），随后在逐日/逐成员/逐条历史记录循环中每次命中都直接写 `raise self._global_error` 或 `raise cache[key]`；
  - Python 每次 `raise` 同一个异常实例时都会把当前调用栈原地追加到该实例的 `__traceback__` 链表上（实测重复抛 1,000 次会让 traceback 从 11 帧暴涨到 4,007 帧），不仅拖慢异常捕获，还把沿途所有已结束调用帧的局部变量一直钉在内存里。
- **正确做法**：凡缓存异常对象供后续重复抛出，在存入缓存和每次重抛时均须调用 `exc.with_traceback(None)` 清空旧栈帧，或只缓存错误码/消息元组并在抛出点新建异常实例。

### 106. 严禁“共享底层文件读取器对重复/错位坐标放任不管，只在单个子域入口加补丁校验”（Per-Domain File Check Leaving Shared XLSX Reader Vulnerable to Duplicate Cell Overwrite）
> 母题：J 口径没在源头统一 · O 语义走样

- **典型坏习惯**：
  - 全仓多类 XLSX 导入（资源、库存、日历、成员绑定、流程模板）共用底层 `file_source.py:_xlsx_rows` 的 `openpyxl.load_workbook(..., read_only=True)` 迭代；当工作表 XML 同一行出现重复坐标（如两个 `<c r="B2">`）或倒序单元格时，`openpyxl` 只读迭代器会静默取后值覆盖前值；
  - 之前只在单个流程文件入口单独挂了 `check_sheet_order(stream)` 拦截重复/倒序/缺坐标单元格，导致其余导入路径在共享读取边界上仍会静默吞掉前一个单元格的值。
- **正确做法**：物理文件格式与坐标结构不变量（如 XLSX 工作表行号/列坐标严格递增、禁止重复坐标）必须下沉到唯一共享的读取边界（`file_source.py:_xlsx_rows`）统一拦截，严禁只在单个业务子域外围打补丁而放任共享底层解析器静默覆盖。

### 107. 严禁“上游提前将结构化快照字符串化导致读写标签分裂，不在源头去掉预转换，反而在路由层造专属包装或抹平底层类型区分契约”（Premature Snapshot Stringification Causing Downstream Compensating Wrappers or Type-Tag Erasure）
> 母题：J 口径没在源头统一 · M 无用拷贝与往返转换 · O 语义走样

- **典型坏习惯**：
  - 统一写令牌工厂 `issue_write_context.for_snapshot(snapshot)` 内部已经统一调一次 `fact_value(snapshot)`（区分原始字符串 `("text", ...)` 与结构化对象 `("canonical", content_text(...))`）；
  - 但上游读取投影（如 `attach_context` 与 `_with_contexts`）在调用 `factory.for_snapshot(snapshot)` 之前多此一举地先把结构化 `dict` 转成了 `content_text(snapshot)` 字符串（导致签发侧被打上 `"text"` 标签），而写入校验侧传入的是原始 `dict`（被打上 `"canonical"` 标签）；
  - 第一种错误做法：不去上游删掉多余的 `content_text(snapshot)` 预转换，反而在下游路由层另造一套 `field_context` / `validate_field_context` 补偿包装把两端都强转成字符串；
  - 第二种错误做法：为了删掉下游 `field_context` 包装，粗暴把底层 `fact_value` 的 `("text", ...)` 与 `("canonical", ...)` 合并成一个标签，结果直接破坏了 `1 != "1"` 的快照类型隔离契约。
- **正确做法**：回到源头删掉上游读取侧多余的 `content_text(snapshot)` 预转换，让读取签发与写入校验统一向 `issue_write_context` / `validate_write_context` 传入权威结构化快照，同时严格保留底层 `fact_value` 对原始字符串与结构化值的类型区分。

### 108. 严禁“多级体积裁剪/降级投影剥离结构化身份字段，导致二次经过同一投影时因缺字段将文案坍塌为占位符 `"-"`”（Non-Idempotent Size-Guard Projection Stripping Identity Fields and Corrupting Messages on Second Pass）
> 母题：J 口径没在源头统一 · O 语义走样

- **典型坏习惯**：
  - 首次构造公开错误字典时用 `failed_task_code` 格式化了 `message` 却未保存受控 `failed_task_code` 字段，且体积裁剪投影函数 `size_guard_public_error_detail` 在 Tier 0 裁剪时剥离了 `task_code` 与 `task_id` 并原地覆写列表，同时优先调用按字段重组文案的 `_structured_failure_message(raw)`；
  - 当摘要在 Tier 0 后仍超限进入 Tier 1+ 或 `minimal_summary_for_size_guard` 时，二次调用同一投影函数因 `task_code` 已被第一轮剥离，把所有错误文案全部篡改成 `"任务 - 未能生成有效执行计划..."`。
- **正确做法**：多级裁剪或多次调用的投影/清洗函数必须具备**幂等性（`f(f(x)) == f(x)`）**——凡是下游文案重组或身份定位依赖的受控字段（如 `task_id`、`task_code`、`failed_task_code`），必须在构造源头与每一轮裁剪投影中完整保留。

### 109. 严禁“正式契约已明文规定用唯一 ID（如 `category_id`）判定同一性并跳过零时长点事件，优化器/启发式子模块却各自用展示名称（`category_name`）判相等或把点事件算作类别切换”（Using Display Labels Instead of Formal Identity IDs or Counting Point Events as Changeovers in Heuristics）
> 母题：J 口径没在源头统一 · O 语义走样

- **典型坏习惯**：
  - 正式契约（`changeover_events.py`）明文规定类别切换只认 `category_id`（展示标签 `category_name` 绝不代表业务身份）且跳过 `start_time == end_time` 零时长点事件；
  - 优化器的邻域移动、插入筛选、联合窗口、特征提取 4 处却各自取 `row.category_name` 判 `!=`（甚至对 ID 做 `.strip()`），且未排除零时长点事件的资源占用与切换计数，导致同 ID 不同名产生虚假切换、不同 ID 同名漏计切换、`T1 -> 零时长点事件 T2 -> T1` 被算成两次切换。
- **正确做法**：评估、搜索启发式与正式统计必须共享同一个身份字段（`category_id` 原值）与点事件过滤语义，严禁用展示名称充当业务身份。

### 110. 严禁“整数解析与校验函数将原生 `int` 或纯整数字符串绕道 `float(value)` 往返转换，导致 `> 2^53` 的 64 位整数静默丢失精度并篡改身份 ID”（Routing Integer Validation Through `float` Causing Silent Precision Loss Beyond `2^53`）
> 母题：M 无用拷贝与往返转换 · O 语义走样

- **典型坏习惯**：
  - 整数校验函数 `_parse_finite_int` 为了兼顾 `"1.0"` 或 `"1e3"`，先无条件对所有输入执行 `parsed = float(value); integer_value = int(parsed)`，甚至外层 `parse_required_int` 的原生 `int` 快速通道还加了 `-(1 << 53) <= value <= (1 << 53) and min_value is None and reject_integer_float is False` 的限制；
  - 一旦传入带 `min_value=1`、`reject_integer_float=True` 或超过 `2^53` 的合法 64 位整数（如 `9007199254740993` 或 `"9007199254740993"`），`float(value)` 会静默将其舍入成 `9007199254740992.0`（且 `abs(parsed - float(integer_value)) == 0.0 <= 1e-9` 不报错），直接把任务/实体 ID 静默篡改成相邻 ID。
- **正确做法**：整数解析入口排除 `bool` 后，对原生 `int` 直接精确返回 `int(value)`，对不含 `.` 与 `e`/`E` 的纯整数字符串直接走 `int(text)` 精确解析，绝不让精确整数经由 IEEE-754 双精度浮点数往返降精。

### 111. 严禁“把服务端已确认终态（成功回执或明确拒绝）后的本机存储写/删操作混入网络请求的 `try/catch`，将本机存储异常篡改为‘结果未决（`pending`/`lookup_failed`）’并死锁恢复入口”（Conflating Post-Receipt Local Storage Failure with Uncertain Network Outcome）
> 母题：N 事务、锁与状态生命周期 · K 吞错与静默降级

- **典型坏习惯**：
  - 服务端已返回明确确认回执（HTTP 200/202 `receipt`）或明确拒绝（`committed: false`），前端却把更新/清理 `localStorage`/`sessionStorage`（如 `accept(receipt)`、`attach()`、`finish()`、`clear(key)`、`clearPending()`）混在网络请求的同一个 `try/catch` 内，甚至在明确拒绝后若 `clearPending()` 抛错就把状态改为 `phase: 'pending'`；
  - 一旦本机存储写/删抛异常，不仅已拿到的成功回执被丢弃、已核实终态被降级为 `lookup_failed`，或者明确拒绝被篡改成 `pending` 后立即向后端发起虚假的 `check()`（抹掉原错误且因后端不存拒绝回执而导致 `reset()` 永久返回 `false` 死锁）；
  - 同理，`submit()` 捕获网络未知异常设置 `setError(e)` 后，尾部自动跟进调用 `await lookup()`，而 `lookup()` 进门第一行无条件执行 `setError(null)`，当后端返回未记录（`null`）时直接把刚捕获的提交失败原因清空。
- **正确做法**：严格分离**“服务端命令终态”**与**“本机存储持久化/清理状态”**——服务端已确认的回执先应用到内存状态与回调，本机存储写/删单独捕获进 `storageError`；明确拒绝后若本机清理失败，保持 `phase: 'rejected'` 供后续 `reset()` 重试清理，绝不倒退回 `pending` 发起虚假查询；自动跟进的未决探查在未查到记录时保留刚捕获的提交失败原因。

### 112. 严禁“锁死焦点陷阱弹窗却把唯一恢复按钮留在弹窗外（Modal Focus-Trap Locking Out Recovery）”以及“给直接绑定 `onClick={fn}` 的函数加默认参数 `fn(flag = false)` 误吞 `Event` 对象”（Default Flag Parameter Swallowing React/DOM Event as Truthy）
> 母题：N 事务、锁与状态生命周期 · O 语义走样

- **典型坏习惯**：
  - **关门拔钥匙**：当命令处于未决态（`!!commands.key`）时，给 `<Modal>` 传 `locked={true}`（禁用关闭与 `Escape` 并开启焦点陷阱把焦点死锁在弹窗内），同时禁用弹窗内所有按钮，却把唯一能解除未决态的「查询结果」（`commands.lookup`）按钮渲染在弹窗遮罩下方的底层页面上，导致用户永远点不到恢复入口；
  - **默认参数吃进点击事件**：为了区分内部自动调用与外部手动点击，给 Hook 导出的函数加上默认参数 `async function lookup(preserveError = false)`，而 UI 层写的是 `<Button onClick={command.lookup}>`——用户点击按钮时，React/DOM 传入的 `SyntheticEvent`/`MouseEvent` 对象作为首个参数进入 `lookup(event)`，在布尔判断 `if (!preserveError)` 中被当成 **truthy**（`true`），导致手动点击静默走错分支。
- **正确做法**：凡开启 `locked` 焦点陷阱的模态框，其解除锁定所需的恢复动作（「查询结果」/「刷新记录」）必须在弹窗内部可达；凡暴露给 UI 事件绑定（`onClick={fn}`）的无参动作函数，导出时必须用零参包装 `lookup: () => lookup()` 隔离事件对象，或用显式具名方法区分内部调用与 UI 回调。

### 113. 严禁“从外部函数往服务/快照/解码器实例上猴子补丁式塞 `_private` 私有属性传递状态，并在下游配 `getattr(..., None)` 探测回库/重算或造成跨次读取状态漂移”（Foreign Private Attribute Stashing & Partial-Branch Mutation Drift）
> 母题：I 偷偷传状态 · F 重复校验 · H 缓存失效

- **典型坏习惯**：
  - 上游准备阶段不通过显式参数或返回值传递快照数据，而是猴子补丁式地往宿主实例上写 `svc._schedule_input_cache = {...}`、`target._last_engine_stats = stats`、`admission._point_task_loader = ...`，下游再用 `getattr(svc, "_schedule_input_cache", None)` 探测并配一条“拿不到就偷偷回库重查”的回退分支；
  - 或者从外部模块往快照对象上塞 6 个 `facts._resource_pressure_*` 私有属性，且只在满足特定条件（如当次 `needed` 非空）时更新其中一部分属性，导致同一快照第二次读取分析时 `identity_issues` 状态漂移消失（第 1 次报 1 个缺口、第 2 次变成 0）；反之，收口时若把不依赖日历窗口的身份缺口属性绑死在会触发 3,660 天上限检查的 `self.index`（`ResourceTaskIndex`）上，又会让 `cohort == []`（无内部资源占用）的超长跨度外部协作计划在分析时误触发 422。
- **正确做法**：跨层/跨阶段共享的快照数据必须定义为显式的数据结构或宿主类正式声明的方法/属性，保证同一快照只算一次、多次读取结果恒定；与日历窗口无关的静态身份缺口扫描不得耦合进受窗口天数上限约束的日历索引构造中。

### 114. 严禁“围绕生产永不传入的‘幽灵配置子字典’编写整套白名单与正数/比例校验器，每次运行反复校验自己写死的默认常量”（Validating Unreachable Ghost Config Sub-Dicts Against Hardcoded Defaults）
> 母题：A 死分支、死参数、伪可选 · F 重复校验

- **典型坏习惯**：
  - 生产链路构造 `candidate_construction` 时（`candidate_profile.py`、`shared_optimizer.py`）只写入 `{"graph_optimization": ...}`，从不产生 `"elite_repair"` 或 `"iterated_greedy"` 子字典（全仓生产与测试 0 处传入）；
  - 但契约模块 `repair_contract.py` 与 `iterated_greedy_contract.py` 却围绕 `optimization.get("iterated_greedy", {})`（恒为 `{}`）写了整套 `_ALLOWED_KEYS` 键白名单、`_positive_int`、`_non_negative_int`、`_unit_fraction`、`_generators` 校验函数，甚至在搜索开始前专门空跑一遍这两个函数——结果每次运行都在用一堆校验器反复校验自己写死的默认常数（`raw.get("max_destruction_size", 6)` 等）。
- **正确做法**：核实配置字典的真实生产构造源头；对生产与契约均不存在的“幽灵配置子段”，直接返回默认 dataclass（`EliteRepairLimits(enabled=enabled)` / `IteratedGreedyLimits(enabled=enabled)`），整块删除自嗨式默认常量校验器。

### 115. 严禁“用吞 `KeyError` 的 `get(row, key, default)` 糊弄多条 `SELECT` 列集合不一致与模型死字段，把漏选列导致的业务字段恒 `None` 真缺陷静默掩盖”（Swallowing `KeyError` in `from_row` to Mask Divergent SQL Column Projections and Dead Model Fields）
> 母题：K 吞错与静默降级 · J 口径没在源头统一

- **典型坏习惯**：
  - 模型层 `from_row` 为了用一个“大而全”构造函数应付多条列集合不同的 `SELECT`，对每个字段都调 `get(row, key, default)` 吞掉 `KeyError`/`IndexError` 返回默认值；
  - 结果模型上留着 9 个全仓 0 读取的死字段没人发现，更严重的是两条共享同一缓存的生产 SQL（`plan_query_repo.py` 选 13 列漏了 `published_version`、`plan_catalog_repo.py` 选 14 列）因漏选列导致 `selected_plan` 路径读出的 `published_version` 恒为 `None`，以及 `resource_summary_repo.py` 漏选 `WorkCalendar.remark`，全被 `get()` 静默吞成 `None`/`""` 掩盖了过去。
- **正确做法**：删掉吞 `KeyError` 的 `get(row, key, default)`，`from_row` 全部直接 `row[key]`（SQLite 行缺列即 Schema 或查询契约破损，必须当场暴露）；删掉模型上无人读取的死字段；同一模型的多条 `SELECT` 补齐一致列集合（确属跨版本旧归档表缺列的，在版本迁移/读取边界显式补 `None`；确属局部窄查询的，用窄值对象承接）。

### 116. 严禁“前端在十几个组件里复制粘贴混入后端永不产出的‘幽灵错误码’与 `typeof command.reset === 'function'` 死守卫，或混用表单与预检的拒绝重置条件”（Copy-Pasted Phantom Error Codes & Stale-Reset Misapplication Across Preview vs Edit Flows）
> 母题：J 口径没在源头统一 · A 死分支、死参数、伪可选 · N 事务、锁与状态生命周期

- **典型坏习惯**：
  - 后端全仓只产出 `stale_write` 和 `snapshot_stale` 两个陈旧码，前端却在 10 个组件里复制粘贴 `['stale_write', 'stale_context', 'context_expired', 'snapshot_stale']`（混入两个全仓 0 产出的幽灵错误码），在 6 个文件各写一份 `function isStale(command)`，在 20 处复制 `if (command.phase === 'rejected' && !isStale(command) && typeof command.reset === 'function') command.reset()`（而 `useCommand` 必返回 `reset` 函数，`typeof` 恒真）；
  - 同时，文件导入预检与分批预览面板重新预检本身就会生成全新的 `write_context`，若不加区分地照搬普通表单“跳过陈旧拒绝不重置”的逻辑，会导致重新预检后「确认导入」按钮因旧 `stale_write` 拒绝态未清除而永久禁用。
- **正确做法**：陈旧错误码集合以真实后端契约为唯一事实源（`['stale_write', 'snapshot_stale']`）在共享契约单点定义，由 `useCommand` 直接派生 `command.stale` 并提供 `command.clearRejection({ includeStale })`；普通编辑表单默认跳过陈旧拒绝（需用户刷新），重新生成写令牌的预检/换文件流程显式传 `{ includeStale: true }` 解锁。

### 117. 严禁“构造边界已强校验必填的字段在内部模型上仍标为 `Optional` 并跨多层维护死错误码，或在生产代码中用 `SimpleNamespace` 拼残缺对象逼下游写 `getattr` 兜底”（Upstream-Required Fields Typed `Optional` Feeding Dead Error Codes & Production `SimpleNamespace` Duck-Typing）
> 母题：A 死分支、死参数、伪可选 · F 重复校验 · J 口径没在源头统一

- **典型坏习惯**：
  - 唯一生产构造边界 `input_rows.py:build_task_model` 早已用 `_required_text` 强制校验 `task_code`、`group_id`、`task_type_id`、`task_type_name` 为非空无首尾空白 `str`；但内部模型 `GroupTask` 与 `TaskForEngine` 仍将 `task_type_id`、`task_type_name` 声明为 `Optional[str]`，导致算法层在 4 个模块里专门维护了一整套生产与测试均 0 触发的 `MISSING_TASK_TYPE_ID` 分支与计数；
  - 生产调用方（如 `new_slot_effort`、`_merged_groups`）图省事用 `SimpleNamespace(**task_row)` 拼出缺字段的残缺对象直接传给引擎层函数，反过来逼着下游满篇写 `getattr(task, "merge_mode", None)` 兜底。
- **正确做法**：内部模型的字段类型必须与唯一构造边界的校验强度严格对齐（必有字段标 `str`/`int`，连根删掉下游只为 `None` 存在的死错误码与死分支）；生产代码严禁用 `SimpleNamespace` 伪造残缺鸭子对象，必须构造完整强类型对象或传显式参数。

### 118. 严禁“对所有生产实现都具备的协议方法写 `getattr(obj, 'method', None)` 能力探测以迁就残缺测试桩，并从测试回退分支派生出生产 100% 不可达的错误码与计数”（Capability-Probing Production-Required Protocol Methods for Test Stubs & Spawning Unreachable Error Codes）
> 母题：A 死分支、死参数、伪可选 · D 靠测试续命 · F 重复校验

- **典型坏习惯**：
  - 全部 4 个生产日历实现（`CalendarService`、`CalendarEngine`、`ExecutionResourceCalendar`、`CalendarTimingMemo`）都实现了 `add_effective_hours` 与 `working_hours_between`，且 `slot_estimator.py` 在 `add_effective_hours` 路径下本来就直接调用 `calendar.working_hours_between(...)`；
  - 但 `slot_estimator.py` 与 `due_span.py` 为了伺候 7 个没写全方法的测试日历桩，分别写了 `getattr(calendar, "add_effective_hours", None)` 与 `getattr(calendar, "working_hours_between", None)`；更荒唐的是，当 `add_effective_hours` 存在时（即 100% 的生产运行），`_estimate_attempt` 恒返回 `efficiency_fallback_used=False`，导致只有测试桩才会走进的 `_resolve_efficiency` 回退分支竟然跨 5 个生产模块派生出了 `efficiency_fallback_used` 字段、`efficiency_fallback_count` 统计，以及 `calendar_efficiency_unknown` HTTP 422 拒绝分支（生产与测试对 `calendar_efficiency_unknown` 均为 0 触发）。
- **正确做法**：生产协议必有的方法直接调用，严禁用 `getattr` 能力探测为残缺测试桩开后门；测试桩缺方法就补齐测试桩方法，把附着在测试回退分支上的死字段、死计数与生产不可达错误码连根删净。

### 119. 严禁“围绕上游从不写入的‘幽灵元数据键’、只写 `True` 的恒真标志或已统一为严格模式的旧开关维护下游解析器、降级告警与恒值摘要字段”（Downstream Parsers & Constant Summary Fields for Zero-Writer or Always-True Metadata Keys）
> 母题：A 死分支、死参数、伪可选 · C 清理不彻底 · D 靠测试续命 · F 重复校验

- **典型坏习惯**：
  - 上游生产写入侧（如 `resource_pool_builder.py`）面对早已在快照阶段校验完毕的维护窗口字典，只往内存 `maintenance_meta` 写死 `load_ok = True` 和 `extend_ok = True`（全仓 0 处写 `False`、0 处写 `partial_fail_count` 或样本键）；但下游 `summary_degradation.py` 与 `_meta_bool_state` 却把本进程内存字典当成外部脏文本防，写了整套 `"yes"/"on"/int` 布尔解析、`parse_failed` 标志、带 `try/except` 的 `_meta_int_state`/`_meta_sample`、4 条失败告警文案，并在结果摘要里常年输出恒定为 `True`/`False`/`0`/`None` 的 6 个死字段（后端、前端、模板 0 处读取），仅靠单测手工塞入伪造脏字典保活；
  - 同理，上游 `input_builder.py` 与 `freeze_window.py` 早已统一为严格模式（失败直接抛错，不再走宽松降级），下游公开摘要却仍保留整套宽松降级分支，常年输出恒定为 `"strict"`/`False`/`[]` 的 4 个恒值字段。
- **正确做法**：对摘要与降级字典的每一个键，必须逆向核查上游生产写入点；凡全仓无生产写入方、只写恒定常量或上游已统一为严格报错的键，连同下游的文本/异常解析函数、告警分支、恒值摘要字段及伪造输入的单测一并连根删除。

### 120. 严禁“单调用方函数先 `raise` 异常、唯一调用方紧接着 `try/except` 接住转成 `None` 的跨函数‘自抛自接’（Single-Caller Throw-and-Catch Circle），以及只为单一策略存在的跨层包装与分发伪抽象（Single-Policy Dispatch）”
> 母题：B 多余的层 · K 吞错与静默降级 · A 死分支、死参数、伪可选

- **典型坏习惯**：
  - **跨函数“自抛自接”**：`calendar_engine(tables)` 构造 `ArchivedCalendar`，看到 `factory.engine is None` 就 `raise ValidationError("日历数据无效...")`；而它在全仓的**唯一调用方** `SimulationValidator.__init__` 紧接着写 `try: self.engine = calendar_engine(self.tables) except (AppError, ValueError, TypeError, OverflowError): self.engine = None; self.calendar_issue = issue("calendar_unproven", ...)`——把原本已经是 `None` 的值先抛异常再捕回 `None`，还顺带用四类宽泛异常把 `calendar_engine` 自己抛的文案吞掉；
  - **单一策略的伪分发链**：生产 `optimize_schedule` 先调用 `profile_summary` 生成字典包进 `{"graph_optimization": summary}` 层层透传给 `build_search_stages` → `resolve_profiles_and_metrics`，后者再从字典里读出刚写进去的 `"candidate_policy": "objective_aware_portfolio"` 去 if/else 分发（另一条 `"weight_grid"` 分支生产 0 调用），然后再用 `summary.get("candidate_policy") == "objective_aware_portfolio"`（恒 `True`）去给下游配置类传参。
- **正确做法**：底层已经用 `None` 表达结构不可用时，直接返回 `Optional` 由唯一调用方显式判 `is None`，严禁跨函数“先 `raise` 再宽泛 `except` 变回 `None`”；生产只有唯一策略时直接调用该策略构造函数，删掉自导自演的字典打包、策略分发器与恒真 `enabled` 开关。

### 121. 严禁“把纯派生值存成可写字段，改源字段（如 `dataclasses.replace`）后失步，全靠下游冗余重复转换顺手纠正”（Storing Purely Derived Value as Mutable Field Corrected Only by Downstream Redundant Re-Coercion）
> 母题：J 口径没在源头统一 · F 重复校验 · M 无用拷贝与往返转换

- **典型坏习惯**：
  - `ConfigSnapshot.downstream_weight` 的真实语义完全由 `critical_weight` 和 `impact_weight` 唯一决定（两者皆为 0 时取 0，否则取 1），却被定义成了 dataclass 可写字段，仅在 `snapshot_from_values` 首次构造时计算一次；
  - 当调用方通过 `dataclasses.replace(cfg, critical_weight=0, impact_weight=0)` 修改源权重时，`downstream_weight` 仍停留在旧值 `1` 与源权重失步，过去全靠下游 `candidate_runner.py` 冗余调用 `ensure_config_snapshot` 把已经是快照的对象再跑一遍 `snapshot_from_values` 才顺手把值改回 `0`。
- **正确做法**：凡完全由同对象其他字段决定的派生值，必须在模型上定义为只读 `@property` 在源头实时派生，严禁存成可与源字段失步的冗余字段再靠下游重复转换补偿。

### 122. 严禁“在上游构造函数无脑浅拷贝 `[dict(row) for row in items]` 替远处的原地修改者擦屁股，以及让两个视图共享同一批可变 dict 靠原地改写‘顺带’同步”（Upstream Defensive Copy Masking Downstream In-Place Mutation & Shared Mutable Aliasing Across Views）
> 母题：M 无用拷贝与往返转换 · I 偷偷传状态

- **典型坏习惯**：
  - `simulation.py:validation(issues)` 在每次构造校验结果时无条件执行 `[dict(row) for row in issues]`，表面看似无用拷贝，实则是因为下游保存路径 `service.py:_remap_issue_refs` 直接原地改写 `row["task_ref"] = mapping[...]`，若上游不拷贝就会把共享的 `admission["base_issues"]` 污染；
  - 更隐蔽的是，`projection.py` 里单条任务的 `task["issues"]` 与顶层 `validation["issues"]` 共用同一批 `issue` 字典引用，原本全靠 `_remap_issue_refs` 原地改写 `validation["issues"]` 时“顺带”把 `task["issues"]` 里的 `task_ref` 一起改了。
- **正确做法**：遵循**“谁转换谁负责”**——上游构造/聚合函数直接持有原列表，不做预防性浅拷贝；下游需要重映射引用时由转换函数返回新字典列表，并对所有需要重映射的视图字段（`validation["issues"]` 与每个 `task["issues"]`）各自显式赋值，严禁靠共享可变字典的原地修改“顺带”同步另一处视图。

### 123. 严禁“两处写 `# Mirrors ...` 注释各自维护同一套输入归类逻辑，且被镜像函数内部又把同一套判断分快慢两段重复写两遍”（Duplicate "Mirrored" Classification Functions & Redundant Fast/Slow Cascading Branches）
> 母题：J 口径没在源头统一 · F 重复校验 · G 重复计算

- **典型坏习惯**：
  - `CalendarEngine._priority_class` 写着注释 `# Mirrors DayPolicy.is_priority_allowed: urgent and critical share the urgent flag, everything else is normal`，单独维护一份 `str(priority or _NORMAL_PRIORITY).strip().lower()` 归类；
  - 而被它“镜像”的 `DayPolicy.is_priority_allowed` 内部又分成前后两段：前半段先判 `priority is None or priority == "normal"` 和 `priority in ("urgent", "critical")`，后半段再写 `# 防御：priority 可能大小写不一致/非字符串/空值` 做 `str(priority or _NORMAL_PRIORITY).strip().lower()` 后把完全相同的判断再写一遍——结果同一条“urgent/critical 归入 urgent，其余归入 normal”的规则在两个文件里被写了三遍。
- **正确做法**：删掉“镜像函数”，在领域源头抽成唯一的归类函数 `priority_class(priority)`（已规范化字符串 $O(1)$ 查表，仅对外部未归一化原始值做一次折算），缓存键构造与日策略判断共同调用同一函数。

### 124. 严禁“生产主入口 100% 必传的事务读取上下文在十几个内部函数上全标 `context=None`，并处处写伪独立回退链保活死仓储方法”（Ubiquitous `context=None` Fallback Chains Across Internal Layers Keeping Dead Repository Methods Alive）
> 母题：A 死分支、死参数、伪可选 · C 清理不彻底 · F 重复校验

- **典型坏习惯**：
  - 计划读取的每个生产入口在开启只读事务时都会构造 `PlanReadContext(conn)` 并向下传递；
  - 但下层 6 个模块里的 18 个内部函数却统统声明 `context=None`，并在函数体里到处写 `context.references if context is not None else PlanIdentityRepository(conn)`、`context.task_identities(ref) if context is not None else PlanBaselineRepository(conn).raw_task_refs_by_plan(ref)`，假装每个内部 helper 都能脱离 `PlanReadContext` 独立运行，甚至在 `PlanBaselineRepository` 里专门留着 `raw_task_refs_by_plan`、`history_version_exists`、`list_task_refs_by_plan` 三个 0 生产调用的仓储方法给这些死回退分支续命。
- **正确做法**：凡生产主入口已统一构造并传入的事务级上下文，下层内部函数一律收紧为必填参数 `context: PlanReadContext`，删光所有 `if context is not None else Repo(conn)...` 双轨回退分支，并顺藤摸瓜删掉只被那些回退分支引用的死仓储方法。

### 125. 严禁“下层基类多写了一行读取侧 `.strip()` 污染已落库身份，上层不去修基类反而专门派生一个单方法子类覆写同一方法绕开 `.strip()`”（Subclassing Only to Bypass Base-Class Read-Side `.strip()` Instead of Fixing the Base Class）
> 母题：B 多余的层 · J 口径没在源头统一 · O 语义走样

- **典型坏习惯**：
  - 基类 `PlanQueryService.resolve_plan_view` 在读取侧对 `scenario_id` 多写了 `str(scenario_id or "").strip()`，破坏了“写入口严防、读取侧保真”的身份契约；
  - 上层 `read_context.py` 发现后不去删基类的 `.strip()`，而是专门定义一个只有 4 行代码的子类 `class PlanReadQuery(PagePlanQueryService)`，覆写 `resolve_plan_view` 并写 docstring `"""Permanent scenario keys reach the existing resolver exactly as stored."""` 来绕开基类的 `.strip()`。
- **正确做法**：基类违反“读取侧保真”契约时，直接在基类源头删掉读取侧 `.strip()`，并连根删掉上层只为绕过基类缺陷而派生的单方法包装子类。

### 126. 严禁“存储整数解析器给 `NOT NULL` 列配 `default=0/1`、对 `bytes` 用 `errors='ignore'` 静默吞脏字节，或用 `str.isdigit()` / `int(value)` 糊弄十进制与精确整数校验”（Swallowing Corrupt `NOT NULL` Storage Integers via `default=` & `errors="ignore"`, or Misusing `str.isdigit()` / `int()` as Integer Validators）
> 母题：K 吞错与静默降级 · A 死分支、死参数、伪可选 · O 语义走样

- **典型坏习惯**：
  - `models/helpers.py:parse_int(value, default=...)` 给 `NOT NULL` 列（如 `version`、`base_version`、`issue_count`、`row_count`）配 `default=0` 或 `default=1` 并为此维护两套 `@overload`——因为列是 `NOT NULL`，`default` 唯一生效的时候就是存储数据损坏成 `'N/A'`、`None` 或 `'1.5'` 的时候，等于把数据库损坏静默伪装成合法的 `0` 或 `1`；同时对 `bytes` 用 `decode("utf-8", errors="ignore")` 把含非法字节的损坏数据硬抠出数字；
  - `safe_non_negative_int` 与 `parse_positive_int` 用 `text.isdigit()` 当作 `int(text)` 可解析的前提，遇到 Unicode 上标数字 `"²"`（`isdigit()` 为 `True` 但 `isdecimal()` 为 `False`）直接让声称“永不抛错”的公开安全投影炸出 `ValueError`；`strict_non_negative_int` 只拦 `float` 却直接调 `int(value)`，把 `Decimal("1.5")` 静默截断成 `1` 并放行文本/字节串。
- **正确做法**：拆清 `parse_int(value, *, field) -> int`（用于 `NOT NULL` 列，空值与坏值一律抛 `ValueError`，绝不配 `default`）与 `parse_optional_int(value, *, field) -> Optional[int]`（用于可空列，仅 `None`/空白串返回 `None`，坏值同样抛 `ValueError`）；字节串必须严格 UTF-8 解码；十进制数字文本判断统一用 `str.isdecimal()`，SQLite `INTEGER` 列精确类型判断统一用 `isinstance(v, int) and not isinstance(v, bool)`（`is_int`）。

### 127. 严禁“在 `finally` 里对关闭失败直接 `raise` 覆盖 `try` 块已拿到的业务结果并被外层宽泛 `except` 张冠李戴，或循环中途抛错却把半截结果留在外层共享列表里”（`finally` Close Re-Raise Masking Result & Mid-Loop Exception Leaving Partial Rows in Shared List）
> 母题：K 吞错与静默降级 · N 事务、锁与状态生命周期

- **典型坏习惯**：
  - `read_exit_backup_enabled` 在 `try` 块已经读出配置开关后，`finally` 里 `conn.close()` 抛错时先记一条关闭失败日志又直接 `raise`，不仅把刚读出的结果顶掉，还被外层 `run_exit_backup` 的宽泛 `except` 捕获后打上“维护状态或停止屏障未核实”的错位原因；
  - `log_records` 在遍历 500 条操作日志的循环里直接往外层共享的 `rows.append(...)` 追加，一旦第 100 条日志投影抛错，`except` 块把 `sources` 标成 `"state": "error", "count": None`，但前 99 条半截记录却留在了返回的 `rows` 里，造成来源状态与实际条数自相矛盾。
- **正确做法**：关闭连接失败本身需要阻止后续动作时，在函数内显式捕获、记录准确原因并返回 `None`（未确认态），绝不在 `finally` 里裸 `raise` 让外层张冠李戴；窗口/批量投影必须先写入局部列表 `batch_rows`，待整个窗口无异常完成后再一次性 `rows.extend(batch_rows)` 原子并入。

### 128. 严禁“前端单包应用在入口自检同一产物内的几十个全局变量、在根部把空上下文 `{}` 转成 `undefined` 逼所有子工作区各写一遍 `|| {}`，或由 `typeof onNavigate === 'function'` 派生 `canNavigate` 层层透传”（Same-Bundle Global Self-Checks, Root `{} -> undefined` Forcing Downstream `|| {}`, & `typeof` Callback Probing Cascading `canNavigate` Props）
> 母题：A 死分支、死参数、伪可选 · F 重复校验 · C 清理不彻底

- **典型坏习惯**：
  - 前端入口启动时对明明打包在同一个产物且按固定顺序先行定义的 26 个工作区/组件逐个写 `typeof window.XxxWorkspace !== 'function'` 自检；同时在入口把空的 `page.context`（`{}`）转成 `Object.keys(context).length ? context : undefined`，逼着下游十几个工作区各自写 `initialContext = {}`、`props.initialContext || {}`、`typeof initialContext === 'object'`；
  - 生产宿主 100% 必传 `onNavigate` / `renderPreview` / `onRefresh` 等回调，子组件却到处写 `typeof onNavigate === 'function'`，甚至由此派生出布尔 prop `canNavigate={typeof onNavigate === 'function'}` 跨多层组件透传。
- **正确做法**：启动自检只检查独立外部脚本（如 `React`、`ReactDOM`、`AppTheme`）；空上下文在入口源头保持为 `{}` 直接下传，删掉所有子工作区的 `|| {}` 兜底；同包父组件恒传的回调直接调用，删光 `typeof fn === 'function'` 守卫及由它派生的 `canNavigate` 透传链（测试夹具缺回调就补夹具）。

### 129. 严禁“在无静态作用域检查的 JS/JSX 组件中删形参（如 `canNavigate`），只删外层引用却漏掉 `.map()` 循环体内的同名引用，因单测夹具恰好传空数组 `runs: []` 逃过测试，非空渲染即炸 `ReferenceError`”（Deleting JS/JSX Component Parameter While Leaving Reference Inside Unexercised `.map()` Callback）
> 母题：C 清理不彻底 · D 靠测试续命

- **典型坏习惯**：
  - `CandidatePanels.jsx` 清理冗余的 `canNavigate` 形参时，把 `function Candidates({ data, navigate, canNavigate })` 改为 `function Candidates({ data, navigate })` 并删掉了表头两个按钮上的 `reason={!canNavigate ? ...}`，却漏掉了超长单行 JSX 内 `c.runs.map(r => ... <Button ... reason={!canNavigate ? ...}>)` 里的第 3 处 `canNavigate`；
  - 因为 JavaScript 在定义箭头函数时不校验自由变量，而现有前端单测夹具里 `candidate_catalog.runs` 恰好是空数组 `[]`（从不进入 `.map()` 回调），所有既有单测全绿，一旦生产环境有执行记录进入该面板就当场抛 `ReferenceError: canNavigate is not defined`。
- **正确做法**：在 JS/JSX 中每删除一个形参、局部变量或导入符号，必须用 `rg -n '\b<name>\b'` 全文件及构建产物复核 0 残留（尤其警惕超长单行 JSX 与 `.map()`/`.filter()` 内部回调），并用非空列表渲染断言锁定。

### 130. 严禁“中间层 `pop('strict_mode', False)` 校验后丢弃不透传，让下层 `strict_mode: bool = False` 默认参数在生产环境常年跑在静默降级轨；或用 `except Exception: return result, set()` 把测试 `MagicMock` 解包异常连同生产算法故障一并吞掉”（Intermediate Layer Dropping `strict_mode` Leaving Downstream Running Lenient Path, & `except Exception` Masking Broken Test Mocks）
> 母题：A 死分支、死参数、伪可选 · K 吞错与静默降级 · D 靠测试续命

- **典型坏习惯**：
  - `run_context.py:dispatch_task` 先写 `strict_mode = bool(call_kwargs.pop("strict_mode", False))` 校验调用参数，紧接着调 `self.task_callback(*args, **call_kwargs)` 时却把已 `pop` 掉的 `strict_mode` 丢了，导致下层 `TaskScheduler._dispatch_task(..., strict_mode: bool = False)` 与 `task_estimator._estimate_duration` 在生产环境即便外层显式传了 `strict_mode=True` 也常年静默退回 `strict_mode=False` 宽松分支；
  - `group_order.py` 用 `except Exception: return result, set()` 兜住任务放置回调，表面叫“兼容回退”，实则把 `test_task_effort.py` 里未配置二元组返回值 `(result, blocked)` 的 `MagicMock` 解包 `ValueError` 连同真实算法异常一并吞掉，形成“靠吞错保绿坏测试、靠坏测试证明吞错删不得”的死锁。
- **正确做法**：彻底铲除 `strict_mode` 这类双轨开关，让全链路只有唯一严格口径；严禁在核心调度主干写 `except Exception` 吞错，删掉吞错后暴露出的残缺测试桩（如未配返回元组的 `MagicMock`）直接在测试夹具里补齐真实返回契约。

### 131. 严禁“大小预算守卫用默认 `json.dumps(obj)`（带空格分隔且放行 `NaN`）丈量、实际落库却写紧凑 `canonical_json(obj)`，或重抛 `ValidationError(..., details=exc.details)` 时只改 `exc.field` 却原样透传旧 `details['field']` 造成顶层与明细自相矛盾”（Size Guard Measuring with Non-Canonical `json.dumps` Instead of Storage `canonical_json`, & Re-Raising `ValidationError` With Stale `details["field"]`）
> 母题：J 口径没在源头统一 · O 语义走样

- **典型坏习惯**：
  - 体积守卫函数 `summary_size_bytes` 用 `len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))`（默认带 `, ` 和 `: ` 空格且 `allow_nan=True`）计算摘要体积上限，而真正写库的 `canonical_json(obj)` 用的是 `separators=(",", ":"), sort_keys=True, allow_nan=False`——导致守卫算出的字节数比真实落库体积虚高，且对 `NaN`/`Infinity` 的拒绝口径不一致；
  - 键解析模块捕获底层 `ValidationError` 后重抛 `ValidationError(str(exc), field=field, code=exc.code, details=exc.details)`，把顶层 `exc.field` 改成了外层字段名，却把内层旧字段名留在 `exc.details["field"]` 里，触发契约层“顶层 field 与 details.field 必须一致”校验失败。
- **正确做法**：体积预算守卫与实际持久化必须复用同一个序列化函数 `canonical_json(obj)`；包装重抛结构化异常并改写 `field` 时，必须同步更新或重建 `details` 中的同名定位字段，保证异常顶层属性与 `details` 载荷一致。

### 132. 严禁“后台异步执行不复用受理时归档的确定性快照，反而重新调挂钟时间 `date.today()` 重算导致跨午夜结果漂移，或后端硬编码多字段常量字典 `source_verification`、前端再逐字断言等于同一串常量”（Worker Recomputing Admission-Archived Dispositions with Wall-Clock `date.today()` Causing Midnight Drift, & Hardcoded Constant `source_verification` Echo-Chamber）
> 母题：A 死分支、死参数、伪可选 · F 重复校验 · J 口径没在源头统一 · O 语义走样

- **典型坏习惯**：
  - 异步任务受理（`jobs.py`）时已经基于当时的业务事实算好并归档了任务准入分类 `dispositions`（含前置资源就绪日期），后台 Worker（`compute.py -> prepare_run_input`）却丢掉已归档的 `dispositions`，重新调 `classify_rows` 并用挂钟时间 `date.today().isoformat()` 判断资源就绪——一旦排队任务跨午夜执行，哪怕数据库事实一字未改，资源就绪判定也会静默漂移；
  - 后端两处对比视图（`candidate_comparison.py` 与 `candidate_baseline.py`）各自硬编码写死 5 个恒定常量的 `"source_verification": {"facts_content_verified": True, "baseline_matches_archived": True, "execution_verified_at": "run_admission", ...}`，前端契约再煞有介事地 `C.equal(g.source_verification, { ... })` 比对这串写死的常量，全链路没有任何动态分支或信息量。
- **正确做法**：已受理任务的后台执行与后续重放必须直接消费受理归档快照（如 `admitted_dispositions`），严禁在异步执行期重新调用挂钟时间（`date.today()` / `datetime.now()`）重算受理期事实；任何前后端全链路恒为硬编码常量的“伪核验凭证”字典直接整块删除。

### 133. 严禁“用 `None` 兼任可选参数缺省哨兵，让存储损坏读出的 `null` 伪装成‘未传参’静默回退重算”以及“删掉 `typeof obj.fn === 'function'` 时漏查局部 `{ ...obj, fn: undefined }` 屏蔽包装”（`None` Default Sentinel Conflating Omitted Argument with Corrupt Stored `null`, & `{ ...obj, fn: undefined }` Wrapper Breaking After Removing `typeof` Probe）
> 母题：C 清理不彻底 · K 吞错与静默降级 · O 语义走样

- **典型坏习惯**：
  - `prepare_run_input(..., admitted_dispositions=None)` 用 `if admitted is None: classify_rows(...)` 区分“受理期现算”和“Worker 消费归档处置”；一旦数据库里归档的 `dispositions` 损坏成 JSON `null`（Python `None`），Worker 传入 `admitted_dispositions=admission["dispositions"]`（即 `None`）直接命中 `is None` 分支，静默退回 `classify_rows` 重新分类；
  - `resource-session.js` 删掉 `typeof adapter.readPending === 'function'` 改为直接调 `adapter.readPending()` 时，只搜了 `create(` 工厂，漏查 `MaintenanceWindowPanel.jsx` 用 `{ ...adapter, readPending: undefined }` 覆盖方法来屏蔽父级挂起意图，导致打开维护窗口弹窗抛 `TypeError` 被 `catch` 吞进 `phase: 'pending'` 永久锁死。
- **正确做法**：当参数传入值来自反序列化存储（可能因损坏出现 `None`）时，缺省哨兵必须使用模块私有 `object()`（如 `_CLASSIFY_CURRENT = object()`），对传入的 `None` 走存储损坏拒绝；删除 `typeof obj.fn === 'function'` 探测时，必须全仓 `rg '\bfn\b'` 检查所有读写与对象展开覆盖点（将 `fn: undefined` 改为显式空实现 `fn: () => null`）。

### 134. 严禁“只看同文件内部调用方就删共享 helper 的合法 `null` 分支（漏查挂到全局对象上的别名调用方）”以及“测试 helper 绕过生产准入预处理直接传原始 `dict`，反向养活生产里的重复 `_key_valid` 校验”（Tightening Shared Helper Without Checking Exported Alias Callers, & Test Helper Bypassing Production Admission to Keep Redundant Validation Alive）
> 母题：C 清理不彻底 · D 靠测试续命 · F 重复校验 · O 语义走样

- **典型坏习惯**：
  - `SharedTerms.js` 的 `missingArrangements(unplannedCount, deferredCount)` 除被同文件的候选摘要 `completionSummary`（入参恒为非负整数）调用外，还以 `missing_arrangements: missingArrangements` 挂到 `window.SharedTerms` 上，被 `CandidatePanels.jsx` 和 `DecisionDetails.jsx` 用于展示正式计划基准的未分配任务数（首次运行无正式基准时合法传入 `(null, null)`）；只看同文件调用点就删掉 `unplannedCount == null && deferredCount == null` 分支，导致首次运行丢失“未分配任务数待确认”提示；
  - `test_priority_pruning.py` 的 `_pruning` 在创建 `session` 后，不调生产的 `_prepare_graph_state`（会将 key 映射准入为 `ValidatedPriorityKeys`），而是直接拿原始 `dict` 构造 `graph` 传给 `PriorityPruning`，从而使生产不可达的 `type(keys) is not ValidatedPriorityKeys` 分支和 `_key_valid` 逐项重验在 `priority_pruning.py` 里靠测试续命。
- **正确做法**：收紧任何共享函数前，必须同时检索其内部函数名与导出属性名（如 `missingArrangements` 与 `missing_arrangements`）的全部跨文件调用链，区分“无生产来源的单 `null` 死分支”与“无正式基准时的合法双 `null` 分支”；测试辅助函数必须走完真实的生产准入/预处理链路（如 `_prepare_graph_state`），并同步删掉生产代码中只为未准入测试输入保留的重复校验。

### 135. 严禁“准入/加载函数‘校验即丢弃’不向下游传递已解析产物，逼下游拿到原始输入再完整归一化/构造第二遍”（Validate-and-Discard Causing Downstream Re-Normalization & Duplicate Fact Construction）
> 母题：E 先算后扔 · G 重复计算 · F 重复校验

- **典型坏习惯**：
  - `workflow_commands.py:normalize_workflow_input("route_confirm", payload)` 调 `normalize_route_preview_input(payload["route"])` 跑完最多 2,000 个流程步骤的结构校验与序列化后，直接丢弃返回的 `WorkflowPreviewInput`、只返回原始 `payload["route"]`，几毫秒后 `prepare_route -> parser.preview` 又对同一份 `payload["route"]` 从头跑第二遍 `normalize_route_preview_input`；
  - `candidate_archive.py:load_candidate` 内部已构造 `SnapshotFacts(capture)` 校验任务引用，却未随返回元组带出，导致两个生产调用方紧接着各自再构造一遍 `SnapshotFacts(capture)`；
  - `original_duration(original, *, effort=None)` 在调用方已算好并传入 `effort = new_slot_effort(original)` 时，仍写 `else: _original_target(task, group, execution)` 重验一遍数量与耗时。
- **正确做法**：准入、预检或加载函数一旦完成了重型解析或事实构造，必须通过返回值或命令上下文显式将结构化产物传给下游复用，严禁“入口验完即扔、下游拿到原始输入再算一遍”。

### 136. 严禁“对本模块刚产出的固定键字典用 `.get(k, default)` 装不熟，或先伪造哨兵上限常数再立刻拿同一上限 `min()` 裁剪”（Self-Produced Fixed-Key Dict `.get()` & Fake Sentinel Constants）
> 母题：F 重复校验 · K 吞错与静默降级 · A 死分支、死参数、伪可选

- **典型坏习惯**：
  - 为绕开 `getattr`/`hasattr` 扫描，改用字典 `.get(key, default)` 读取同一受控链路刚刚构造、键集合 100% 固定的内部字典：如 `exporters/xlsx.py` 对报表服务刚产出的固定键字典连写 37 个 `row.get(...)`；邻域搜索模块对上游 `enrich_metrics` 刚填齐的指标字典到处写 `metric.get("due_pressure", 0.0)`、`metric.get("due_deadline_hours", 1e9)`；`local_search_state.py` 对自产 `candidate` 写 `candidate.get("order") or self.current_order`，一旦缺键就静默回退旧状态；
  - 特征计算模块先给无截止时间的任务伪造 `start_dt + timedelta(days=30)` 的假截止期传进 `residual_window_end`，紧接着该函数内部又拿 `start_dt + timedelta(days=30)` 上限去 `min(...)` 裁剪。
- **正确做法**：凡上游受控链路已保证写入的固定字典键，下游一律用 `row[key]` 直接下标读取（单测手造残缺字典缺键时去补齐单测夹具）；可选时间边界（如 `due_exclusive: Optional[datetime]`）直接传 `None` 由窗口函数按上限收敛，严禁先伪造哨兵常数再裁剪。

### 137. 严禁“只取计数或前 K 条摘要，却先跑全量分页投影、签发写凭据或无条件预建全表索引”（Full Page/Entity Projection Only to Read `.total` or Top-K Slice）
> 母题：E 先算后扔 · G 重复计算

- **典型坏习惯**：
  - `inventory_export.py` 导出预检只需知道筛选命中的记录总条数，却调用 `reader.page(PageRequest(**effective, size=1))` 走完分页实体投影与关联统计，最后只取 `page["total"]`；
  - `risk_items.py:build_incomplete_group_items` 公开契约只保留前 50 条未完成任务组明细（其余仅计总数 `count`），却先把全部未完成任务组逐组扫完 `results` 和 `failure_details` 构造完整字典列表，最后在出口 `items[:50]` 丢弃其余全部明细；
  - `report_import.py:_items` 不管导入文件里有没有通过格式校验的待处理行，进门先查全表 `self._resource_index()`。
- **正确做法**：只读条数时提供轻量 `count(query)` 方法；带 `top-K` 上限的明细构造先对有序 ID 切片 `[:K]`，再仅为这 `K` 条扫描构造明细；跨表资源索引推迟到确认存在有效待处理行时按需懒加载一次。

### 138. 严禁“业务只剩唯一实现后仍跨层保留多表/多角色/旧 Schema 版本的‘幽灵多态协议’”（Single-Implementation Ghost Polymorphism & Legacy Schema Version Probes）
> 母题：A 死分支、死参数、伪可选 · B 多余的层 · C 清理不彻底

- **典型坏习惯**：
  - 候选与沙盒数据早已独立存储，正式计划全仓只存于唯一的 `Plans` 表（`role == "active"`），但 `PlanQueryRepository`、`PlanCatalogRepository`、`PlanMetadataRepository`、`PlanQueryService`、`PlanReadContext` 仍层层保留 `source_table: str = SOURCE_PLANS` 参数、`_plan_rows_sql` 分发器、`(version, source_table)` 二元组缓存键，以及单元素包装 `_catalog_history`；
  - 数据库最低支持版本早已升至 `SchemaVersion >= 43`，但多个仓储每次读取仍查 `SchemaVersion` / `sqlite_master` 探测 `< 33/35/36` 的旧库缺表分支；沙盒查询层仍保留非 `facts-v2` 旧分支并保活 0 引用的 `legacy_simulation_query_repo.py`。
- **正确做法**：当多实现或多版本分支退役只剩唯一现行实现时，彻底剥离 `source_table` / `role` 等幽灵多态参数，缓存键简化为纯标量 `version`，删除 `SchemaVersion < N` 历史探针及伴生的孤儿模块。

### 139. 严禁“横切响应头（如 `Cache-Control: no-store`）不在响应工厂与路由装饰器源头收口，而在几十个路由里散落手写并衍生单行 `_response` 包装”（Scattered Cross-Cutting Header Stamps & Single-Use Response Wrappers）
> 母题：J 口径没在源头统一 · B 多余的层

- **典型坏习惯**：
  - 后端 API 与文件下载响应统一要求 `Cache-Control: no-store`，却不在底层响应工厂 `json_response` 与统一路由装饰器 `api_endpoint` 源头设置，而是在 30 多个路由文件里手工写 60 多处 `response.headers["Cache-Control"] = "no-store"`，还为此在多个路由文件里专门定义只做这一行赋值的 `_response(data, snapshot)` / `_command(result)` 二传手函数。
- **正确做法**：横切 HTTP 响应头必须在底层响应工厂（`json_response`、`file_response`）与统一路由装饰器（`api_endpoint`、`system_endpoint`）出口一次性收口，删光各业务路由文件中的散落赋值与二传手包装。

### 140. 严禁“把锁文件读取 `OSError` 与内容损坏混同降级触发过期自愈误删活跃锁（Fail-Open），或用 `float` 容差近似代替 `Decimal` 精确整数解析”（Conflating Lock Read `OSError` with Stale/Corrupt Content to Trigger Self-Healing Deletion, & Float-Tolerance Integer Parsing）
> 母题：K 吞错与静默降级 · N 事务、锁与状态生命周期 · O 语义走样

- **典型坏习惯**：
  - `maintenance_lock.py:_read_lock_file` 捕获 `OSError`（权限不足或 I/O 故障）返回 `(None, "io_error")`，外层 `verify_maintenance_lock` 未将其与内容损坏区分，继续按“无 PID + 旧 `mtime`”走进 `_try_self_heal_stale_lock` 把正在生效的维护锁删掉并返回“维护窗未激活”（Fail-Open）；
  - `strict_parse.py:is_exact_int_value` 用 `float(value)` + `abs(f - round(f)) <= 1e-9` 判定整数，把 `"1.0000000001"` 放行成 `1`，把 `"9007199254740993.0"` 精度截断改写成 `9007199254740992`。
- **正确做法**：锁与安全屏障读取必须遵循 **Fail-Closed**：`OSError` 读取失败独立标记为 `"unreadable"` 并直接判定为活跃阻断，严禁进入自愈删除分支；精确整数解析统一走 `Decimal` 十进制整数判定，严禁经过 `float` 容差近似。

