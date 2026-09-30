# 供水管网故障隔离与恢复供水服务

基于 **Python 3.12 + FastAPI + PostgreSQL** 的供水管网故障隔离服务。

支持录入带编号的**水源(source)**、**用水点(sink)**、**连接点(junction)**及两端节点明确的**管段**（管段可设阀门）；
针对一条故障管段，预演并提交阀门关闭方案，自动判断**能否隔离**并计算**失供用水点**；
隔离提交后生成带编号的隔离事件，操作员可按**事件编号 + 当前管网版本**进行
**复供预演与确认**，安全地重新开阀、恢复供水；
现场测绘发现管网资料有误时，还可按**基准版本**对节点/管段做**局部增改删**，
**不清空隔离事件**、原子生效。

---

## 一、快速启动（Docker Compose 一键启动）

前置条件：安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build
```

该命令会：

1. 启动 PostgreSQL 16（端口 `5432`），并通过 `healthcheck` 等待数据库就绪；
2. 构建并启动 API 服务（端口 `8000`）；
3. API 启动时自动执行 `CREATE TABLE IF NOT EXISTS ...` 完成数据库初始化（无需手工建表）。

停止：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时清空数据库数据卷
```

## 二、服务地址与端口

| 服务 | 地址 |
| --- | --- |
| API 基础地址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| Swagger 交互文档 | <http://localhost:8000/docs> |
| ReDoc 文档 | <http://localhost:8000/redoc> |
| OpenAPI Schema | <http://localhost:8000/openapi.json> |
| PostgreSQL | `localhost:5432`（库/用户/密码默认均为 `water`） |

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://water:water@localhost:5432/water` | PostgreSQL 连接串；Compose 中指向 `db` 服务 |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库就绪的重试次数 |
| `DB_CONNECT_RETRY_INTERVAL` | `1.0` | 重试间隔秒数 |
| `HISTORY_LIMIT` | `100` | 历史记录接口返回上限 |

### 本地（非 Docker）运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 确保 PostgreSQL 已建库（create database water;）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 四、核心业务规则

**导入校验**（任一不满足即整体拒绝，返回全部问题明细，不写入数据）：

- 管段两端节点必须存在 —— 拒绝**悬空端点**；
- 节点编号、管段编号分别不可重复 —— 拒绝**重号**；
- 管段两端不可为同一节点 —— 拒绝**自环**；
- 没有阀门的管段不能标记为已关闭。

**隔离判定**（方案图 = 移除故障管段 + 关闭拟关阀门，**已关闭阀门继续生效**；
同时**其他待修复事件的故障管段视为不通、其关闭阀继续关闭**）：

- 仅当故障管段**两端**在方案图中都**无法从任一水源到达**，方案才 `isolatable=true`，方可提交；
- 失供用水点 = **方案前**由任一水源可达、**方案后**不可达的用水点，按编号（字符串字典序）排序；
  方案前已经失供的点（例如其他待修复事件已造成的失供）不会重复计入。

**提交约束**（任一不满足即拒绝且不改任何状态）：

- `version` 必须等于当前管网版本，否则 `409 VERSION_CONFLICT`（版本过期）；
- 故障管段、拟关管段必须存在；拟关管段必须**带阀门**；
- 拟关列表不得重复、不得包含故障管段本身；已关闭的阀门不得重复关闭；
- 方案必须可隔离（`isolatable=true`），否则 `422 NOT_ISOLATABLE`；
- 提交在**单个数据库事务**内完成：行级锁校验版本 → 更新阀位 → 版本号 +1 → 写隔离事件，原子生效。
- 整网导入为替换式：重写全部节点/管段、清空历史（**旧事件全部失效**）、版本重置为 `1`。

**隔离事件与复供**：

- 每次成功提交生成一条隔离事件，状态为 `pending`（**待修复**）；确认复供后变为 `restored`（**已复供**）。
- **复供预演** `POST /api/isolation/events/{id}/restore/preview`（**只读，不改任何状态**）返回：
  - 该事件提交时所关阀门（`closed_pipe_ids`）与确认时将重开的阀门（`reopened_pipe_ids`）；
  - 预计恢复供水的用水点（`recoverable_sink_ids`）与本次仍无法恢复的用水点（`still_blocked_sink_ids`）；
  - 阻断原因明细（`issues`）：`BLOCKED_BY_PENDING_FAULT`（其他待修复故障管段未修复）、
    `BLOCKED_BY_CLOSED_VALVE`（阀门仍被其他待修复事件占用）；重新进水风险为 `RESTORE_REWATER_RISK`。
- **通水计算**：重开本事件阀门时，继续排除**其他待修复事件的故障管段**与**仍关闭的阀门**；
  被多个待修复事件共用的阀门，在其余事件仍待修复期间保持关闭。
- **确认条件**：仅当重开后，其余每个待修复事件的故障管段**两端仍均无法从水源到达**
  （不会被重新进水）才允许确认，否则 `422 RESTORE_UNSAFE`。
- **确认复供** `POST /api/isolation/events/{id}/restore/confirm` 在**同一事务**内：
  行级锁复核版本 → 锁定事件行复核状态 → 用最新管网重新做安全性评估 →
  重开本事件阀门（被其他待修复事件占用的除外）→ 版本号 +1 → 事件置为 `restored`。
  版本过期（`409 VERSION_CONFLICT`）、事件不存在（`404 EVENT_NOT_FOUND`）、
  事件已复供（`409 EVENT_ALREADY_RESTORED`）、重新进水风险（`422 RESTORE_UNSAFE`）
  均明确拒绝，事务整体回滚，**不产生部分变更**。

**局部修订（现场测绘纠错，`POST /api/network/revise`）**：

- 不替换整网、**不清空任何隔离事件**（待修复事件继续生效），按基准版本做
  增（`add_nodes`/`add_pipes`）、改（`update_nodes`/`update_pipes`）、
  删（`delete_node_ids`/`delete_pipe_ids`）；每个变更都须**列明对象编号**。
- 编号记账：新增编号不得与既有/本次编号重复（`ADD_*_ID_EXISTS`、`ADD_*_DUPLICATE`）；
  更新/删除目标必须存在（`*_NOT_FOUND`）；删除列表不得重复（`DELETE_*_DUPLICATE`）；
  同一对象不得在同次请求中出现多次、不得既更新又删除
  （`CHANGE_TARGET_DUPLICATE`、`CHANGE_TARGET_CONFLICT`）。
- **阀位规则**：新增管段的阀门**默认开启**，不得在修订中置关
  （`NEW_PIPE_VALVE_CLOSED`）；现有 `valve_closed` **只能由隔离提交/复供确认改变**，
  修订中给出该字段即拒绝（`VALVE_STATE_IMMUTABLE`）。
- **候选管网结构校验**：在应用变更后的**完整候选管网**上执行与整网导入相同的校验
  ——编号唯一、端点存在（拒绝悬空端点）、无自环、无阀管段不可关闭
  （`DUPLICATE_*`、`PIPE_ENDPOINT_UNKNOWN`、`PIPE_SELF_LOOP`、`NON_VALVE_PIPE_CLOSED`）。
- **待修复事件引用保护**：待修复事件引用的**故障管段、关阀管段、失供用水点不得删除**
  （`PROTECTED_PIPE_DELETE`、`PROTECTED_SINK_DELETE`）；故障/关阀管段的**端点与阀门属性**、
  失供用水点的**节点类型不得改动**（`PROTECTED_PIPE_ENDPOINT_CHANGE`、
  `PROTECTED_PIPE_VALVE_CHANGE`、`PROTECTED_SINK_TYPE_CHANGE`）。
  违约明细会给出涉及的对象编号与事件编号（`event_id`）。
- **通水复核**：在“**当前关阀管段 ∪ 全部待修复故障管段**”断开的候选通水图上做 BFS，
  每条待修复故障管段**两端仍须均不可从水源到达**；任一端重新可达即拒绝
  （`422 PENDING_FAULT_REWATER`，携带事件与故障管段编号）。
- 版本过期返回 `409 VERSION_CONFLICT`；以上任一违约或版本过期都**不写入**。
- 成功时在**单个数据库事务**内：行级锁校验版本 → 增删改节点/管段（SQL 不触碰
  `valve_closed` 列）→ 版本号 **+1** → 写修订流水（`network_revisions` 表），
  原子返回**新版本号、修订流水号与全部变更编号**。整网导入会清空修订流水并从 1 重新累计。

## 五、HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/network/import` | 整网导入（版本重置为 1，旧隔离事件失效） |
| GET | `/api/network` | 读取当前管网与版本 |
| POST | `/api/network/revise` | 局部修订（按基准版本增改删，保留隔离事件，原子版本 +1） |
| POST | `/api/isolation/preview` | 隔离方案预演（只读，计入待修复故障管段） |
| POST | `/api/isolation/submit` | 提交隔离（原子关阀 + 版本 +1） |
| GET | `/api/isolation/events` | 隔离操作历史（含 `status`: pending/restored） |
| POST | `/api/isolation/events/{id}/restore/preview` | 复供预演（只读，不改状态） |
| POST | `/api/isolation/events/{id}/restore/confirm` | 确认复供（同事务复核 + 重开阀 + 版本 +1） |

业务错误统一返回：

```json
{ "error": { "code": "NOT_ISOLATABLE", "message": "...", "issues": [ ... ] } }
```

## 六、调用示例

### 1. 导入管网

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d @examples/sample_network.json
```

示例管网（`examples/sample_network.json`）：

```
S(水源) --P1[阀]-- A(连接点) --P2[阀]-- B(连接点) --P4[阀]-- D(连接点) --P6(无阀,故障)-- U3(用水点)
                    |                  |
                  P3[阀]             P5[阀]
                    |                  |
                   U1(用水点)          U2(用水点)
```

响应：

```json
{ "version": 1, "node_count": 7, "pipe_count": 6 }
```

导入非法数据（如悬空端点）会返回 `422 IMPORT_INVALID`：

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d '{"nodes":[{"id":"S","type":"source"}],"pipes":[{"id":"P1","from":"S","to":"X","has_valve":false}]}'
```

### 2. 读取版本

```bash
curl -s http://localhost:8000/api/network
```

### 3. 预演：只关 P5（不能隔离）

```bash
curl -s -X POST http://localhost:8000/api/isolation/preview \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P5"],"version":1}'
```

`D` 点仍可经 P4 进水，故障管段 P6 的 D 端仍可达，故 `isolatable=false`。

### 4. 预演：关 P4（可以隔离，仅 U3 失供）

```bash
curl -s -X POST http://localhost:8000/api/isolation/preview \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
```

响应：

```json
{
  "version": 1,
  "version_match": true,
  "fault_pipe_id": "P6",
  "close_pipe_ids": ["P4"],
  "isolatable": true,
  "affected_sink_ids": ["U3"],
  "issues": []
}
```

`version_match=false` 表示你读取的版本已过期（预演仍基于当前管网计算，但不应直接提交）。

### 5. 提交隔离

```bash
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
```

成功响应（P4 阀位原子置为关闭，版本变为 2）：

```json
{
  "version": 2,
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "affected_sink_ids": ["U3"],
  "event_id": 1
}
```

常见拒绝：

- 再次用 `"version":1` 提交 → `409 VERSION_CONFLICT`（版本过期，状态不变）；
- 取到版本 2 后再次关闭已关的 `P4` → `422 PLAN_INVALID` / `CLOSE_PIPE_DUPLICATE`；
- 关闭无阀管段 `P6` → `422 PLAN_INVALID` / `CLOSE_PIPE_NO_VALVE`；
- 提交不可隔离方案 → `422 NOT_ISOLATABLE`。

### 6. 隔离历史

```bash
curl -s http://localhost:8000/api/isolation/events
```

历史事件新增（不影响原有字段）：

```json
{
  "id": 1,
  "version_before": 1,
  "version_after": 2,
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "affected_sink_ids": ["U3"],
  "status": "restored",
  "restored_at": "2026-09-30T12:05:00Z",
  "restored_version": 3,
  "reopened_pipe_ids": ["P4"],
  "created_at": "2026-09-30T12:00:00Z"
}
```

### 7. 复供预演（按事件编号 + 当前版本，只读）

```bash
curl -s -X POST http://localhost:8000/api/isolation/events/1/restore/preview \
  -H 'Content-Type: application/json' \
  -d '{"version":2}'
```

响应（示例管网，事件 #1 为关 P4 隔离 P6，无其他待修复事件）：

```json
{
  "version": 2,
  "version_match": true,
  "event_id": 1,
  "status": "pending",
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "safe": true,
  "reopened_pipe_ids": ["P4"],
  "recoverable_sink_ids": ["U3"],
  "still_blocked_sink_ids": [],
  "remaining_closed_pipe_ids": [],
  "remaining_fault_pipe_ids": [],
  "issues": []
}
```

`safe=false` 时 `issues` 中会出现 `RESTORE_REWATER_RISK`（重开会使其他待修复
故障管段重新进水），并为仍无法恢复的用水点给出 `BLOCKED_BY_PENDING_FAULT` /
`BLOCKED_BY_CLOSED_VALVE` 阻断原因。预演不改阀位、版本与事件状态。

### 8. 确认复供

```bash
curl -s -X POST http://localhost:8000/api/isolation/events/1/restore/confirm \
  -H 'Content-Type: application/json' \
  -d '{"version":2}'
```

成功响应（P4 重开，版本变为 3，事件置为 restored）：

```json
{
  "version": 3,
  "event_id": 1,
  "status": "restored",
  "fault_pipe_id": "P6",
  "reopened_pipe_ids": ["P4"],
  "recoverable_sink_ids": ["U3"],
  "still_blocked_sink_ids": []
}
```

常见拒绝（均不产生部分变更）：

- 版本过期 → `409 VERSION_CONFLICT`；
- 事件编号不存在（含整网替换后旧事件失效）→ `404 EVENT_NOT_FOUND`；
- 事件已复供后再次预演/确认 → `409 EVENT_ALREADY_RESTORED`；
- 重开会使其他待修复故障管段重新进水 → `422 RESTORE_UNSAFE`。

### 9. 复供双故障场景演示脚本（标准库实现，无需安装依赖）

隔离区内存在另一条待修复故障管段时，必须先复供内层事件。
示例管网见 `examples/sample_restore_network.json`（D 点经 P7、P8 供 U4）：

```bash
python3 examples/restore_demo.py
# 或指定地址：
python3 examples/restore_demo.py http://localhost:8000
```

脚本依次演示：导入 → 提交事件 #1（关 P4 隔离 P6，U3/U4 失供）→
在隔离区内提交事件 #2（P8 故障，无需再关阀）→ 复供预演 #1 提示
`RESTORE_REWATER_RISK`（重开 P4 会让 P8 进水）→ 强确认被 `422 RESTORE_UNSAFE`
拒绝且无部分变更 → 先复供 #2（无阀重开，安全）→ 再复供 #1（P4 重开，
U3/U4 恢复）→ 重复复供 `409` → 历史中区分 pending/restored。

### 10. 隔离流程一键演示脚本（标准库实现，无需安装依赖）

```bash
python3 examples/demo.py
# 或指定地址：
python3 examples/demo.py http://localhost:8000
```

脚本依次演示：导入 → 读取版本 → 不可行/可行预演 → 过期版本 409 → 成功提交 → 重复提交 409 → 重复关阀 422 → 历史。

### 11. 局部修订（现场测绘纠错，标准库示例脚本）

一键演示（建议先在有隔离事件的状态下运行，脚本会自行导入并提交一条待修复事件）：

```bash
python3 examples/revise_demo.py
# 或指定地址：
python3 examples/revise_demo.py http://localhost:8000
```

脚本依次演示：导入 → 提交待修复事件 #1（关 P4 隔离 P6，U3 失供）→
合法局部修订（新增连接点 `E`、用水点 `U9`、新管段 `P9/P10`，新阀默认开启，
并给 `U1` 改名；事件不清空，版本 +1，返回全部变更编号）→
各类违约均被拒绝且不写入（保护对象删除/改动、旁通管致 `PENDING_FAULT_REWATER`、
新管段置关、修订改阀位、自环/悬空端点、版本过期）→ 修订后正常复供。

请求体（字段全部可选，至少给出一项增/改/删；`update_*` 中**省略的字段保持原值**，
`name` 显式传 `null` 表示清空名称）：

```json
{
  "version": 2,
  "add_nodes": [
    {"id": "E",  "type": "junction", "name": "现场补测连接点"},
    {"id": "U9", "type": "sink",     "name": "新建小区"}
  ],
  "add_pipes": [
    {"id": "P9",  "from": "A", "to": "E",  "has_valve": true,  "name": "现场补测管"},
    {"id": "P10", "from": "E", "to": "U9", "has_valve": false}
  ],
  "update_nodes": [{"id": "U1", "name": "幸福小区（测绘更名）"}],
  "update_pipes": [{"id": "P5", "name": "医院支管"}],
  "delete_node_ids": [],
  "delete_pipe_ids": []
}
```

成功响应（原子更新管网、版本 +1，返回新版本与变更编号）：

```json
{
  "version": 3,
  "revision_no": 1,
  "change_ids": [
    {"op": "add_node",    "node_id": "E",  "pipe_id": null},
    {"op": "add_node",    "node_id": "U9", "pipe_id": null},
    {"op": "add_pipe",    "node_id": null, "pipe_id": "P9"},
    {"op": "add_pipe",    "node_id": null, "pipe_id": "P10"},
    {"op": "update_node", "node_id": "U1", "pipe_id": null}
  ],
  "change_count": 5,
  "node_count": 9,
  "pipe_count": 8
}
```

`op` 取值：`add_node` / `add_pipe` / `update_node` / `update_pipe` /
`delete_node` / `delete_pipe`。

典型违约（均返回 `422 REVISION_INVALID`，`issues` 列明涉及对象/事件与原因，
不写入任何数据）：

- 删除待修复事件引用的故障管段/关阀管段 → `PROTECTED_PIPE_DELETE`；
- 删除失供用水点 → `PROTECTED_SINK_DELETE`；改动其类型 → `PROTECTED_SINK_TYPE_CHANGE`；
- 改动受保护管段端点 → `PROTECTED_PIPE_ENDPOINT_CHANGE`；改动阀门属性 → `PROTECTED_PIPE_VALVE_CHANGE`；
- 新增旁通管使待修复故障管段任一端重新可达 → `PENDING_FAULT_REWATER`；
- 新管段 `valve_closed:true` → `NEW_PIPE_VALVE_CLOSED`；
  在 `update_pipes` 中给 `valve_closed` → `VALVE_STATE_IMMUTABLE`；
- 候选管网自环/悬空端点/重号 → `PIPE_SELF_LOOP` / `PIPE_ENDPOINT_UNKNOWN` / `DUPLICATE_*`。

版本过期返回 `409 VERSION_CONFLICT`，同样不写入。

### 无数据库/依赖环境下的本地自测

没有 Docker / PostgreSQL / FastAPI 时，可用标准库内存后端（复用同一份
`app/planning.py` 领域逻辑与相同 HTTP 契约）跑通全部示例脚本，仅用于本地验证：

```bash
python3 tools/stdlib_server.py 8000        # 终端 A：启动内存后端
python3 examples/revise_demo.py            # 终端 B：局部修订示例
python3 examples/demo.py                   # 隔离流程示例
python3 examples/restore_demo.py           # 复供流程示例
```

> 该内存后端不持久化、重启即清空，**不是生产部署方式**；生产仍用
> `docker compose up --build`（FastAPI + PostgreSQL）。

## 七、测试

核心图算法与校验逻辑（`tests/test_planning.py`，含隔离、待修复故障计入、
复供安全性与阻断溯源共 24 个用例；`tests/test_revision.py`，含局部修订记账、
候选管网结构校验、待修复事件引用保护与通水复核共 33 个用例）不依赖数据库与
第三方库，可直接用标准库运行：

```bash
python3 -m unittest tests.test_planning tests.test_revision -v
```

路由层集成测试（`tests/test_api.py`，含复供预演/确认、版本与状态拒绝、
双故障复供顺序、局部修订成功/保护/重新进水/回滚等）用内存替身替换数据库，
覆盖完整 HTTP 契约，需先安装依赖：

```bash
pip install -r requirements.txt httpx
python3 -m unittest discover -s tests -v
```

## 八、项目结构

```
app/
  config.py       # 环境变量配置
  schemas.py      # Pydantic 请求/响应模型（含局部修订 RevisionIn/Out）
  planning.py     # 纯领域逻辑：导入校验、无向图可达性(BFS)、隔离方案评估、
                  #   复供安全性评估、局部修订（记账/引用保护/候选网校验/通水复核）
  db.py           # PostgreSQL：DDL、快照、整网导入、事务化隔离提交/复供确认/局部修订、历史
  serializers.py  # 响应序列化
  main.py         # FastAPI 路由与错误处理
examples/
  sample_network.json          # 隔离流程示例管网
  sample_restore_network.json  # 双故障复供示例管网
  demo.py                      # 隔离流程调用示例
  restore_demo.py              # 复供流程调用示例
  revise_demo.py               # 局部修订（增改删）调用示例
tools/
  stdlib_server.py             # 标准库内存后端（无依赖时本地验证 HTTP 契约，非生产部署）
tests/
  test_planning.py
  test_revision.py
  test_api.py
Dockerfile
docker-compose.yml
requirements.txt
```
