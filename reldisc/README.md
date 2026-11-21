# reldisc — 离线跨表引用候选发现器

在旧资料仓的 JSONL 表上做**离线包含关系发现**：找出"源表的一组列值是否被
另一张表的某组列覆盖"，并交付证据与未匹配原行。结果是**可供入仓校验的
跨表引用候选**——

* 不要求目标列唯一；
* 不把任何结果宣布为永久外键；
* 不做一张表内部的唯一键或函数依赖发现。

纯 Python 3 标准库实现，无第三方依赖。

## 快速开始

```bash
# 1) 从空目录把清单 + JSONL 一次性变成可重用的类型化磁盘结构
python3 reldisc.py prepare --manifest manifest.json --workdir work/

# 2) 完备搜索（k=1..3，空值口径 complete|equal）
python3 reldisc.py discover --workdir work/ --k 3 --nulls complete \
    --min-rows 100 --max-bad 2 --out discovery.json

# 3) 核查任意合法映射（进程重开只需 workdir）
python3 reldisc.py check --workdir work/ --mapping-json '{
  "source_table": "S", "source_columns": ["x", "y"],
  "target_table": "T", "target_columns": ["x", "y"],
  "nulls": "complete", "min_rows": 1, "max_bad": 2}'

# 4) 分页取不匹配 row_id 及完整元组（按 row_id 稳定排序）
python3 reldisc.py witnesses --workdir work/ --mapping-json '...' \
    --offset 0 --limit 100
```

也可以用 `PYTHONPATH=. python3 -m reldisc ...` 调用。`prepare` 之后
`discover` / `check` / `witnesses` 都只依赖 workdir，可在新进程中运行；
不要求断点恢复搜索。

## 输入模型

* **清单**（manifest.json）：表名、JSONL 路径、每列的名字与类型
  （`text` | `int64`）。表名与列名有独立身份：按字节比较，不修剪、
  不换大小写；`row_id` 是保留列名。
* **JSONL 资料**：每行一个 JSON 对象，键恰好是 `row_id` + 声明的列。
  `row_id` 为字符串或整数，表内唯一。值限定 text / int64 / null；
  **bool 不是整数**（`true` 出现在 int64 列是资料错误），int64 检查
  ±2^63 范围。
* **资料错误**（未知列、重复 row_id、坏类型、输入损坏、JSON 行内重复
  键……）一律中止命令并以退出码 3 报告，**绝不**被当成包含关系失败。
  参数/映射非法是退出码 2。

## 候选与判定语义

* 候选 = 源表列集合（按列名排序规范化）→ 另一张表的有序目标列元组；
  两侧元数相同（1..k）、列内不重复、对应位置类型一致。
  `(甲,乙)→(乙,甲)` 与 `(甲,乙)→(甲,乙)` 是**不同**的候选，分别枚举、
  分别判定。
* 每条候选按指定空值口径**单独**计算参与源行：
  * `complete`：源行在候选源列中含	null 即被排除（不计入参与）；
    目标侧含 null 的元组也不参与匹配；
  * `equal`：null 是有类型的共同值，所有源行参与，null 与 null 在
    类型匹配的位置上相等。
* 参与数 < `min_rows`（正整数）= **证据不足**，不作为候选列出；
  其余逐源行判断整个对应元组是否至少在目标出现一次：
  * 重复源行按各自 row_id 计数（同一未匹配值有多行时，每行都算、
    每行都出现在证据里）；
  * 重复目标不放大覆盖（目标元组按集合处理）。
* `bad <= max_bad`（非负整数）才列入结果；`max_bad=0` 即精确包含。
  所有合格的列映射都保留，不因另一候选更短而省略；每条列出
  参与 / 排除 / 匹配 / 不匹配四个计数，以及按 row_id 排序的小证据
  样本（默认前 10 条）。证据条目还带 `per_column_value_present`
  标记：当联合元组不存在、但每个单列值都在目标列中出现时
  （"边缘值齐全、联合引用却不存在"），`all_single_values_present`
  为真，帮助识别这类误判。

## 搜索完备性说明

给定 `(k, nulls, min_rows, max_bad)`，发现器枚举范围内**全部**
（源列集合 × 目标列元组）组合，并对每个枚举到的候选做**精确**判定，
不抽样、不以单列覆盖成立推断组合覆盖成立：

1. **精确编码而非哈希**：`prepare` 把每个类型的值映射到全局字典的
   整数码（text 与 int64 各一本字典，`"1"` 与 `1` 永不混淆），元组用
   混合进制打包成一个整数键。键相等 ⟺ 类型化元组相等，不存在碰撞；
   因此"不匹配行数"是精确计数而非估计。
2. **共享投影**：每个源列集合的（去重键 + 行数）投影只算一次，被它的
   所有目标元组复用；每个目标列元组的（去重键集合）投影只算一次，被
   同类型模式的所有源集合复用。任一时刻内存里只有当前类型模式组的
   源投影（紧凑数组）和**一个**目标投影；不会把全体候选的元组集合
   同时放进内存，也绝不为每个映射重读 JSONL。
3. **双重核验**：每个被接受的候选在列出前，还会用另一条独立的、
   逐行求值的代码路径（`evalmap`，`check`/`witnesses` 共用）重算一遍，
   两条路径计数不一致即报内部错误。测试里还有第三个实现——直接读
   JSONL 的暴力穷举器——与全部发现结果逐项比对。
4. **支持度剪枝（唯一的安全剪枝）**：`complete` 口径下增加列只会让
   参与集合缩小（`equal` 下恒为全表），所以"参与数 < min_rows"可以从
   一个源列集合传播到**同一表、同一列集合的超集**；绝不跨无关列集合
   传播。
5. **低阶失败不阻断高阶**：`complete` 口径下，低阶的不匹配行可能因
   新增列为 null 而退出参与集合，所以低阶不包含**未必**阻断高阶
   （测试 `TestHigherArityRescue` 演示：一元失败、二元成立）。发现器
   因此从不依据低阶坏行剪枝，各元数独立精确判定。
6. **未完成不谎报**：`--time-budget` / `--candidate-limit` 触界时输出
   `"status": "incomplete"` 并在 `uncovered` 中列出尚未覆盖的范围
   （按元数/类型模式/剩余目标元组计数），不会输出"全部发现完成"。

## 输出

`discover` 输出 JSON：`status`、`params`、`stats`（候选评估/接受/拒绝
数、支持度剪枝数、源/目标投影构建数、精确复核数、分元数耗时、峰值
RSS）、`insufficient`（证据不足的源列集合及原因）、`candidates`
（每条含四计数与证据样本）、以及"候选仅供校验、非永久外键"的声明。

`check` 输出 `verdict`（`accepted` / `rejected` /
`insufficient_evidence`）+ 四计数 + 证据样本；`witnesses` 分页输出
全部不匹配行的 row_id、完整元组与单列存在性标记，按 row_id 稳定排序。

## 磁盘结构

```
work/
  store.json            # 格式版本、各类型域大小、表/列元数据
  dict_text.json        # 全局 text 字典：码 -> 值
  dict_int64.json       # 全局 int64 字典：码 -> 值
  tables/t000/rowids.json
  tables/t000/c000.bin  # 该列的 int64 类型码（array('q') 原始字节）
```

## 测试与演练

```bash
python3 -m unittest discover -s tests   # 39 项：语义、错误处理、分页、
                                        # 重启续查、随机小表与暴力穷举逐项比对
python3 demo/run_demo.py                # 主演练：S/T 数据集，12 项预期行与数量
python3 bench/scale_bench.py            # 5 表 x 6 列 x 5 万行，k=3，256MiB 限额
python3 bench/scale_bench.py --equal    # 追加 equal 口径的一轮
```

基准在 256MiB 地址空间限制（`RLIMIT_AS`）的子进程中运行 prepare 与
discover，记录候选剪枝、精确核验量、耗时与峰值内存；生成的大文件位于
`bench/bench_work/`（已被 .gitignore 排除，不提交）。

本机一轮实测（5 表 × 6 列 × 50 000 行，k=3，min_rows=100，max_bad=2，
含大量"单列相关、复合不成立"的干扰）：

| 阶段 | 候选评估 | 接受 | 拒绝 | 共享投影 | 精确复核 | 耗时 | 峰值 RSS |
|---|---|---|---|---|---|---|---|
| prepare | — | — | — | — | — | 3.1s | 55 MiB |
| discover (complete) | 9 420 | 53 | 9 367 | 205 源 / 375 目标 | 53 | 14.6s | 110 MiB |
| discover (equal) | 9 420 | 47 | 9 373 | 205 源 / 375 目标 | 47 | 12.3s | 106 MiB |

所有候选均为精确评估（无抽样）；53/47 个接受候选各自再经独立的
逐行求值路径复核。植入期望（T1 整行采样自 T0 的全部子集包含、
T2 恰好 2 坏行、T3/T4 干扰被拒绝等）全部通过。

## 限制

* 值域仅限 text / int64 / null；k ≤ 3。
* 单表行数与候选规模受内存约束：当前实现把单表全部列码载入内存
  （每行每列 8 字节），并把一个类型模式组的源投影放在内存；更大规模
  可按同样原则分片外排，接口不变。
* 发现结果是候选关系，供入仓校验使用；数据漂移后需重新运行。
