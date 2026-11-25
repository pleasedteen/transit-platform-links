# 轮椅乘客跨运营商换车核对服务

一个仅使用 **Python 3 标准库 + SQLite** 的 HTTP 服务。它把多运营商站台名录、设施普查和单日班次导入同一个 SQLite 数据库，按公开的名称/位置规则和调度员裁决计算站群，给出只经过“确认无障碍通行”站群的最早到达行程。

## 一条命令自动验收

在项目根目录执行：

```bash
python3 run_checks.py
# 或 ./test.sh
```

该命令会：

1. 在临时目录创建 SQLite 数据库并启动真实 HTTP 子进程；
2. 通过 HTTP 验证分批导入、版本覆盖、同版本幂等；
3. 验证连续同站证据、设施普查优先级、同类最新观测；
4. 验证三分钟换乘、同一班次通过不可下车的中途站；
5. 验证确认/驳回、从新到旧撤销及拆并历史；
6. 经 HTTP 导入确定性的 **200,010 次停靠**压力夹具；
7. 核对枢纽两侧各至少 10,000 次停靠、晚段受影响线路不足 1,000、其他线路仍在运行；
8. 报告每次行程查询、裁决、撤销的实际停靠处理量；
9. `SIGKILL` 杀掉服务后重启，核对已确认的撤销和完整状态仍然有效。

本机实测总耗时约 **4–5 秒**；脚本以 80 秒为硬验收线，超时即失败。

---

## 启动和离线数据装载

要求 Python 3.11+，无需安装第三方包。

### 1. 启动 HTTP 服务

```bash
# 任选一种
./start.sh
python3 server.py --db data/wheelchair.db --host 127.0.0.1 --port 8080
python3 -m wheelchair.app --db data/wheelchair.db --host 127.0.0.1 --port 8080
```

`start.sh` 默认监听 `127.0.0.1:8080`，可用 `HOST=0.0.0.0 PORT=9000 ./start.sh` 覆盖。看到下面的就绪 JSON 后即可请求：

```json
{"event":"ready","host":"127.0.0.1","port":8080,...}
```

### 2. 装载小夹具

另开终端：

```bash
python3 -m wheelchair.load \
  --base-url http://127.0.0.1:8080 \
  --fixture small
```

也可以直接使用已生成的离线文件：

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/imports \
  -H 'Content-Type: application/json' \
  --data-binary @fixtures/small.json
```

### 3. 生成并装载 20 万停靠压力夹具

仓库已附带生成好的确定性 JSONL：

```bash
python3 -m wheelchair.load \
  --base-url http://127.0.0.1:8080 \
  --file fixtures/stress-batches.jsonl
```

也可以不读文件、直接由确定性生成器经 HTTP 分批装载：

```bash
python3 -m wheelchair.load \
  --base-url http://127.0.0.1:8080 \
  --fixture stress
```

重新生成离线夹具文件：

```bash
python3 -m wheelchair.fixtures --out fixtures
```

---

## 常用接口

| 方法与路径 | 说明 |
|---|---|
| `GET /health` | 健康检查和主要表行数 |
| `GET /v1/matching-rules` | 公开的同站匹配规则及三分钟换乘常量 |
| `POST /v1/matching/evaluate` | 用已导入来源引用或原始名称/坐标试算一对记录 |
| `POST /v1/imports` | 原子分批导入站台、普查结论、班次和停靠时刻 |
| `GET /v1/records?source=OA&stop_id=HUB_EAST` | 查来源记录、当前版本和所属站群 |
| `GET /v1/clusters` | 列站群 |
| `GET /v1/clusters/{cluster_id}` | 查站群成员、无障碍结论和全部结论来源 |
| `GET /v1/clusters/{cluster_id}/history` | 查该站群历次拆并缘由 |
| `GET /v1/history` | 全局拆并事件 |
| `GET /v1/adjudications` | 列裁决（含已撤销） |
| `POST /v1/adjudications` | 确认或驳回一对来源记录的直接关系 |
| `POST /v1/adjudications/undo` | 从最新有效裁决开始向旧撤销一次 |
| `POST /v1/adjudications/{id}/undo` | 同上；若 id 不是当前最新有效裁决则返回冲突 |
| `GET /v1/itinerary?...` | 最早到达可行行程 |
| `GET /README.md` or `/docs` | 通过 HTTP 读取本说明 |
| `GET /v1/admin/stop-counts?cluster_id=...` | 夹具规模核对用聚合，不计入操作级处理量 |
| `POST /v1/admin/reset` | 测试用重置，请求体 `{"confirm":"reset"}` |

### 行程查询示例

```bash
curl -sG 'http://127.0.0.1:8080/v1/itinerary' \
  --data-urlencode 'from_source=OA' \
  --data-urlencode 'from_stop_id=WEST_ORIGIN' \
  --data-urlencode 'to_source=OB' \
  --data-urlencode 'to_stop_id=EAST_DEST' \
  --data-urlencode 'earliest_departure=22:00:00'
```

压力夹具中：

- 枢纽未接通/撤销后：乘 `TARGET_A_1` 到 `XFER_A`，在自动同站的 `XFER_B` 换 `TARGET_B_SLOW`，到达 **22:45**；
- 调度员确认 `OA:HUB_EAST` 与 `OB:HUB_WEST` 后：可在枢纽换 `TARGET_B_FAST`，到达 **22:27**。

返回中的 `legs`、`transfers`、`intermediate_stops` 给出上下车站台、班次、时刻、换乘秒数和经过但不下车的中途站；`stats` 是本次查询的实际停靠处理量。

---

## 导入和版本规则

`POST /v1/imports` 的一个批次是一个 SQLite 原子事务。示例：

```json
{
  "batch_id": "batch-2026-11-25-01",
  "source": "OA",
  "source_kind": "operator",
  "service_date": "2026-11-25",
  "reason": "早班更正",
  "sites": [
    {
      "source": "OA",
      "stop_id": "A100",
      "version": 2,
      "name": "中央枢纽东",
      "lat": 31.22,
      "lon": 121.22,
      "accessible": true,
      "observed_at": "2026-11-25T06:00:00Z"
    }
  ],
  "trips": [
    {
      "source": "OA",
      "trip_id": "T100",
      "version": 2,
      "route_id": "R1",
      "stop_times": [
        {"seq": 1, "stop_id": "A100", "departure": "08:00:00"},
        {"seq": 2, "stop_id": "A101", "arrival": "08:05:00", "departure": "08:06:00"}
      ]
    }
  ]
}
```

也支持 `stops`、`facilities`、`surveys`、`accessibility`、`records` 数组别名。普查记录设置 `"source_kind": "census"`。

语义：

- 每个来源的 `(source, stop_id, version)` 唯一；
- 当前值取该来源编号下 `version` 最大的完整记录；
- 相同 `(source, id, version)` 和相同内容重送只生效一次；
- 相同版本但内容不同返回 `409`，整批不生效；
- 旧版本即使晚到也不会覆盖新版本；
- 相同 `batch_id` 重送直接返回首次摘要；相同 `batch_id` 搭配不同内容返回 `409`；
- 班次版本升级时，该班次的 `stop_time` 在同一事务内整体替换；
- 批次中任意时间或字段错误，整批回滚。

---

## 同站判断和站群

规则版本：`name-location-v1`，可通过 `/v1/matching-rules` 获取。

1. 名称做 Unicode NFKC、`casefold`、标点清理和通用词移除；
2. 用 `difflib.SequenceMatcher` 计算规范化名称相似度；
3. 用 haversine 计算 WGS84 坐标距离；
4. 同时满足以下条件形成一条自动证据边：
   - 距离不超过 **200 米**；
   - 名称相似度至少 **0.72**；
5. 规范化核心名完全相同时，距离上限放宽到 **300 米**；
6. 由连续证据边连接的记录做连通分量，得到站群；
7. 空间网格只用于减少候选比较，最终仍逐对执行上述公开规则。

调度员可以增加或覆盖边：

```bash
curl -sS -X POST http://127.0.0.1:8080/v1/adjudications \
  -H 'Content-Type: application/json' \
  -d '{
    "a": {"source":"OA","stop_id":"HUB_EAST"},
    "b": {"source":"OB","stop_id":"HUB_WEST"},
    "verdict":"confirm",
    "reason":"现场站厅连通，轮椅可通行",
    "by":"dispatcher-li"
  }'
```

- `verdict: "confirm"`：即使不满足自动规则也连通；
- `verdict: "reject"`：即使满足自动规则也删除该直接边；
- 同一对记录有多条裁决时，以最新有效裁决为准；
- `POST /v1/adjudications/undo` 总是撤销当前最新的有效裁决，因此严格从新到旧；撤销最新裁决后，上一条同对裁决会重新生效；
- 裁决只追加并标记撤销，不物理删除，便于审计；
- 之后导入的新记录会和全部有效裁决一起重新参与连通分量计算。

### 无障碍结论

每个站群汇总当前成员的已知结论：

1. **第一优先级：设施普查/调查的已知结论**（`source_kind=census|survey`）；
2. **第二优先级：运营商已知结论**（`source_kind=operator`）；
3. 同一优先级内按最新 `observed_at`，再用 `version` 决胜；
4. 没有已知 `true/false` 时状态为 `unknown`，按不可通行处理。

`GET /v1/clusters/{id}` 的 `conclusion.sources` 按“普查优先、同类最新”的顺序返回所有结论来源。

---

## 行程算法和换乘约束

- 仅当起点、终点、上车站群、下车站群的结论为 `accessible=true` 时上下车；
- 不可通行或未知站群中的中途站不会生成下车标签；
- 但乘客可以继续留在同一班次上经过这些中途站，之后在无障碍站群下车；
- 改变班次时，下一班的发车时刻必须满足：

```text
next_departure >= previous_arrival + 180 秒
```

- 索引 `stop_time(stop_source, stop_id, departure_s, arrival_s)` 使晚时段查询直接跳过早已过去的上万条枢纽停靠；
- 算法按最早到达站群做时间依赖搜索，并扫描所乘班次的后续停靠，因此同一班次直达更远站群不需要三分钟重复换乘；
- 返回的每个换乘点都给出实际间隔和 `valid: true/false`。

---

## 并发读取、原子性和崩溃恢复

- 数据库运行在 **SQLite WAL** 模式；
- HTTP 请求使用独立连接；
- 读请求开启只读快照事务，写请求使用 `BEGIN IMMEDIATE` 串行提交；
- 导入、站群重建、裁决/撤销和历史写入在**同一个 SQLite 事务**中提交；
- 写入期间，其他查询继续看到上一完整快照，不会读到半批数据或只改了一半的站群；
- 写事务提交成功后关闭旧快照的新查询会立即看到变更；
- `synchronous=FULL`；进程被 `SIGKILL` 或意外退出后，SQLite/WAL 在下次打开时做崩溃恢复，只保留已提交的完整事务；
- `run_checks.py` 包含真实 `SIGKILL` + 重启核对。

服务没有后台扫描或异步改写 20 万停靠表的任务。裁决只修改规范化的站群图和成员映射，不把 `cluster_id` 冗余改写进每条 `stop_time`，所以枢纽合并/拆分不会重写两侧的两万多条停靠。

---

## 压力夹具和处理量报告

`wheelchair/fixtures.py` 中的数据完全确定，不使用随机数。总停靠数：

```text
OA 压力短线：10,000 班 × 2 停靠 = 20,000
OB 压力短线：10,000 班 × 2 停靠 = 20,000
核心行程：10
DC 干扰线：64 条 × 50 班 × 50 停靠 = 160,000
合计：200,010
```

每个枢纽来源站台恰有 `10,001` 次停靠（10,000 次压力 + 1 次核心行程）。压力短线集中在 05:00–18:55；22:00 查询时，OA/OB 受影响来源在查询时刻之后只剩 10 条停靠，而 DC 其他线路在 22:00 后仍有 6,990 条停靠。

所有行程和裁决响应都带计量：

```json
"stats": {
  "stop_time_db_reads": 14,
  "stop_objects_built": 14,
  "stop_rows_written": 0,
  "accounted_stop_touches": 28,
  "background_stop_processing": 0
}
```

`accounted_stop_touches` 是保守口径：数据库物化读取、内存停靠对象生成、插入/更新/删除都计入；自动测试要求每次压力用例查询、裁决、撤销的该值不超过 **5,000**。当前确定性结果为：

| 操作 | 保守停靠触达 |
|---|---:|
| 拆分状态行程查询 | 28 |
| 确认枢纽裁决 | 0 |
| 接通状态行程查询 | 32 |
| 撤销裁决 | 0 |
| 撤销后行程查询 | 28 |

裁决/撤销为 0 是因为它们只重算站台节点图，不读取或改写 `stop_time`；测试仍输出完整计量字段，而不是省略计数。

---

## 代码结构

```text
run_checks.py             一条命令端到端/压力/重启验收
test.sh                   等价的测试 shell 入口
server.py / start.sh      HTTP 服务入口
wheelchair/
  app.py                  ThreadingHTTPServer、路由和事务边界
  db.py                   SQLite schema、WAL、索引
  importer.py             幂等版本导入和班次更正
  matching.py             公开名称/位置规则与空间候选
  clusters.py             连通分量、无障碍结论、拆并历史
  adjudicate.py           裁决和从新到旧撤销
  planner.py              最早到达、180 秒换乘、同车续乘
  observability.py        停靠处理量计量和 API 错误
  fixtures.py             小型功能夹具与 200,010 停靠压力夹具
  load.py                 HTTP/JSON/JSONL 离线装载器
fixtures/
  small.json
  stress-batches.jsonl
  stress-manifest.json
```
