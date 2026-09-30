"""管网故障隔离的纯领域逻辑（不依赖数据库 / Web 框架）。

规则概述：
- 管网为无向图，节点分为水源(source)/用水点(sink)/连接点(junction)。
- 已关闭阀门的管段视为不可通行。
- 方案图 = 当前管网移除故障管段，再移除/关闭所有“拟关闭管段”，
  同时已关闭阀门继续保持关闭，**其他待修复事件的故障管段同样视为不通**。
- 只有故障管段**两端**在方案图中都无法从任一水源到达，方案才可隔离(isolatable)。
- 失供用水点 = 方案前由任一水源可达、方案后不可达的 sink，按编号排序。

复供（恢复供水）：
- 待修复事件 = 已提交隔离、尚未确认复供的事件；其故障管段仍需保持断水。
- 复供预演/确认在“重开本事件阀门”的图上评估，同时继续排除其他待修复
  事件的故障管段以及其余仍关闭的阀门。
- 仅当重开后，其他待修复事件的故障管段**两端仍均无法从水源到达**
  （不会被重新进水）时，才允许确认复供。

局部修订（现场测绘纠错，evaluate_revision）：
- 按基准版本增改删节点/管段，不清空隔离事件；每个变更须带对象编号。
- 新管段阀门默认开启；现有阀位只能由隔离提交/复供确认改变。
- 先在完整候选管网上做与导入相同的结构校验（编号唯一/端点存在/无自环/无阀管段不可关闭）。
- 待修复事件引用的故障管段、关阀管段、失供用水点不得删除；
  其管段端点、阀门属性与用水点类型不得改动。
- 修订后按“当前关阀 ∪ 待修复故障管段”断开的通水图复核：每条待修复故障管段
  两端仍须均不可从水源到达，否则拒绝（PENDING_FAULT_REWATER）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque

# 问题代码 ---------------------------------------------------------------

# 阻断类问题：出现任一即不能隔离、提交时拒绝（422）
FAULT_PIPE_NOT_FOUND = "FAULT_PIPE_NOT_FOUND"
CLOSE_PIPE_NOT_FOUND = "CLOSE_PIPE_NOT_FOUND"
CLOSE_PIPE_NO_VALVE = "CLOSE_PIPE_NO_VALVE"
CLOSE_PIPE_DUPLICATE = "CLOSE_PIPE_DUPLICATE"
CLOSE_PIPE_IS_FAULT = "CLOSE_PIPE_IS_FAULT"
# 复供阻断：重开本事件阀门会让其他待修复故障管段重新进水
RESTORE_REWATER_RISK = "RESTORE_REWATER_RISK"
# 复供阻断原因（提示性，不单独构成拒绝）：用水点仍被其他故障/阀门阻断
BLOCKED_BY_PENDING_FAULT = "BLOCKED_BY_PENDING_FAULT"
BLOCKED_BY_CLOSED_VALVE = "BLOCKED_BY_CLOSED_VALVE"
# 导入期问题
DUPLICATE_NODE_ID = "DUPLICATE_NODE_ID"
DUPLICATE_PIPE_ID = "DUPLICATE_PIPE_ID"
PIPE_SELF_LOOP = "PIPE_SELF_LOOP"
PIPE_ENDPOINT_UNKNOWN = "PIPE_ENDPOINT_UNKNOWN"
NON_VALVE_PIPE_CLOSED = "NON_VALVE_PIPE_CLOSED"

# 局部修订期问题 -----------------------------------------------------------
# 变更集记账
CHANGESET_EMPTY = "CHANGESET_EMPTY"
ADD_NODE_ID_EXISTS = "ADD_NODE_ID_EXISTS"
ADD_PIPE_ID_EXISTS = "ADD_PIPE_ID_EXISTS"
ADD_NODE_DUPLICATE = "ADD_NODE_DUPLICATE"
ADD_PIPE_DUPLICATE = "ADD_PIPE_DUPLICATE"
UPDATE_NODE_NOT_FOUND = "UPDATE_NODE_NOT_FOUND"
UPDATE_PIPE_NOT_FOUND = "UPDATE_PIPE_NOT_FOUND"
DELETE_NODE_NOT_FOUND = "DELETE_NODE_NOT_FOUND"
DELETE_PIPE_NOT_FOUND = "DELETE_PIPE_NOT_FOUND"
DELETE_NODE_DUPLICATE = "DELETE_NODE_DUPLICATE"
DELETE_PIPE_DUPLICATE = "DELETE_PIPE_DUPLICATE"
CHANGE_TARGET_DUPLICATE = "CHANGE_TARGET_DUPLICATE"
CHANGE_TARGET_CONFLICT = "CHANGE_TARGET_CONFLICT"
# 阀位规则：新管段阀门默认开启；现有阀位只能由隔离/复供流程改变
NEW_PIPE_VALVE_CLOSED = "NEW_PIPE_VALVE_CLOSED"
VALVE_STATE_IMMUTABLE = "VALVE_STATE_IMMUTABLE"
# 待修复事件引用保护
PROTECTED_PIPE_DELETE = "PROTECTED_PIPE_DELETE"
PROTECTED_SINK_DELETE = "PROTECTED_SINK_DELETE"
PROTECTED_PIPE_ENDPOINT_CHANGE = "PROTECTED_PIPE_ENDPOINT_CHANGE"
PROTECTED_PIPE_VALVE_CHANGE = "PROTECTED_PIPE_VALVE_CHANGE"
PROTECTED_SINK_TYPE_CHANGE = "PROTECTED_SINK_TYPE_CHANGE"
# 修订通水复核：修订使待修复故障管段端点重新可从水源到达
PENDING_FAULT_REWATER = "PENDING_FAULT_REWATER"

# 隔离事件状态
EVENT_PENDING = "pending"      # 待修复
EVENT_RESTORED = "restored"    # 已复供

# 结构性阻断问题（预演会给出，提交据此拒绝）
BLOCKING_ISSUE_CODES = frozenset(
    {
        FAULT_PIPE_NOT_FOUND,
        CLOSE_PIPE_NOT_FOUND,
        CLOSE_PIPE_NO_VALVE,
        CLOSE_PIPE_DUPLICATE,
        CLOSE_PIPE_IS_FAULT,
    }
)


@dataclass(frozen=True)
class Node:
    id: str
    type: str  # source / sink / junction
    name: str | None = None


@dataclass(frozen=True)
class Pipe:
    id: str
    from_node: str
    to_node: str
    has_valve: bool = False
    valve_closed: bool = False
    name: str | None = None


@dataclass
class Issue:
    code: str
    message: str
    pipe_id: str | None = None
    # 可选的附加引用：阻断用水点的待修复事件 / 具体管段编号
    event_id: int | None = None
    related_pipe_id: str | None = None
    # 节点类问题引用的节点编号（可选，保持向后兼容）
    node_id: str | None = None

    def as_dict(self) -> dict:
        d = {"code": self.code, "message": self.message, "pipe_id": self.pipe_id}
        if self.event_id is not None:
            d["event_id"] = self.event_id
        if self.related_pipe_id is not None:
            d["related_pipe_id"] = self.related_pipe_id
        if self.node_id is not None:
            d["node_id"] = self.node_id
        return d


@dataclass(frozen=True)
class IsolationEvent:
    """一条隔离事件的领域视图（预演/提交/复供共用）。"""

    id: int
    fault_pipe_id: str
    closed_pipe_ids: tuple[str, ...]
    affected_sink_ids: tuple[str, ...] = ()
    status: str = EVENT_PENDING
    version_before: int | None = None
    version_after: int | None = None


@dataclass
class PlanResult:
    isolatable: bool
    affected_sink_ids: list[str]
    issues: list[Issue] = field(default_factory=list)
    # 方案中实际被阻断的管段（已关 ∪ 拟关，故障管段单独移除）
    blocked_pipe_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass
class RestoreResult:
    """复供评估结果。

    safe=True 表示重开本事件阀门不会让其他待修复故障管段重新进水，
    可以确认复供；False 时 issues 中含 RESTORE_REWATER_RISK。
    """

    safe: bool
    reopened_pipe_ids: list[str]
    recoverable_sink_ids: list[str]
    # 本事件原失供点中本次仍无法恢复的点 -> 阻断原因明细
    still_blocked_sink_ids: list[str]
    issues: list[Issue] = field(default_factory=list)
    # 重开后仍保持关闭的阀门（被其他待修复事件占用）
    remaining_closed_pipe_ids: list[str] = field(default_factory=list)
    # 重开后仍排除的其他待修复故障管段
    remaining_fault_pipe_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 导入校验


def validate_import(
    nodes: list[Node], pipes: list[Pipe]
) -> list[Issue]:
    """校验整网导入数据：重号、自环、悬空端点、无阀管段初始关闭。"""
    issues: list[Issue] = []

    node_ids: set[str] = set()
    for n in nodes:
        if n.id in node_ids:
            issues.append(Issue(DUPLICATE_NODE_ID, f"节点编号重复：{n.id}", n.id))
        node_ids.add(n.id)

    pipe_ids: set[str] = set()
    for p in pipes:
        if p.id in pipe_ids:
            issues.append(Issue(DUPLICATE_PIPE_ID, f"管段编号重复：{p.id}", p.id))
        pipe_ids.add(p.id)

        if p.from_node == p.to_node:
            issues.append(Issue(PIPE_SELF_LOOP, f"管段自环（两端为同一节点）：{p.id}", p.id))
        for endpoint in (p.from_node, p.to_node):
            if endpoint not in node_ids:
                issues.append(
                    Issue(
                        PIPE_ENDPOINT_UNKNOWN,
                        f"管段 {p.id} 的端点 {endpoint} 未在节点列表中定义（悬空端点）",
                        p.id,
                    )
                )
        if p.valve_closed and not p.has_valve:
            issues.append(
                Issue(
                    NON_VALVE_PIPE_CLOSED,
                    f"管段 {p.id} 没有阀门，不能设置为关闭状态",
                    p.id,
                )
            )
    return issues


# ---------------------------------------------------------------- 可达性


def _reachable(
    nodes: dict[str, Node],
    pipes: list[Pipe],
    removed_pipe_ids: frozenset[str],
) -> set[str]:
    """无向 BFS：从全部水源出发，返回可达节点集合。

    removed_pipe_ids 中的管段视为不存在/不可通行。
    """
    return _bfs(nodes, pipes, removed_pipe_ids)[0]


def _bfs(
    nodes: dict[str, Node],
    pipes: list[Pipe],
    removed_pipe_ids: frozenset[str],
) -> tuple[set[str], dict[str, str | None], dict[str, str]]:
    """无向 BFS，返回 (可达节点, 前驱节点, 到达该节点经过的管段)。

    供复供阻断原因溯源使用；removed_pipe_ids 中的管段不可通行。
    """
    adjacency: dict[str, list[tuple[str, str]]] = {nid: [] for nid in nodes}
    for p in pipes:
        if p.id in removed_pipe_ids:
            continue
        if p.from_node not in adjacency or p.to_node not in adjacency:
            continue
        adjacency[p.from_node].append((p.to_node, p.id))
        adjacency[p.to_node].append((p.from_node, p.id))

    reachable: set[str] = set()
    parent: dict[str, str | None] = {}
    via_pipe: dict[str, str] = {}
    queue: deque[str] = deque()
    for nid, node in nodes.items():
        if node.type == "source":
            if nid not in reachable:
                reachable.add(nid)
                parent[nid] = None
                queue.append(nid)

    while queue:
        current = queue.popleft()
        for nxt, pipe_id in adjacency.get(current, ()):
            if nxt not in reachable:
                reachable.add(nxt)
                parent[nxt] = current
                via_pipe[nxt] = pipe_id
                queue.append(nxt)
    return reachable, parent, via_pipe


# ---------------------------------------------------------------- 方案评估


def validate_plan_request(
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    *,
    for_submit: bool,
) -> list[Issue]:
    """校验故障/拟关列表本身的合法性。

    for_submit=True 时额外校验“已关闭阀门不得重复关闭”。
    """
    issues: list[Issue] = []

    if fault_pipe_id not in pipes:
        issues.append(
            Issue(FAULT_PIPE_NOT_FOUND, f"故障管段不存在：{fault_pipe_id}", fault_pipe_id)
        )

    seen: set[str] = set()
    for pid in close_pipe_ids:
        if pid in seen:
            issues.append(Issue(CLOSE_PIPE_DUPLICATE, f"拟关闭管段重复：{pid}", pid))
            continue
        seen.add(pid)

        if pid == fault_pipe_id:
            issues.append(
                Issue(CLOSE_PIPE_IS_FAULT, "不能把故障管段本身作为关闭阀门", pid)
            )
            continue
        pipe = pipes.get(pid)
        if pipe is None:
            issues.append(Issue(CLOSE_PIPE_NOT_FOUND, f"拟关闭管段不存在：{pid}", pid))
        elif not pipe.has_valve:
            issues.append(Issue(CLOSE_PIPE_NO_VALVE, f"管段没有阀门，无法关闭：{pid}", pid))
        elif for_submit and pipe.valve_closed:
            issues.append(
                Issue(
                    CLOSE_PIPE_DUPLICATE,
                    f"阀门已处于关闭状态，不能重复关闭：{pid}",
                    pid,
                )
            )
    return issues


def evaluate_plan(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    *,
    for_submit: bool = False,
    pending_events: list[IsolationEvent] | None = None,
) -> PlanResult:
    """评估隔离方案。

    预演(for_submit=False)：只做结构校验，已关闭阀门不会产生“重复关闭”问题；
    提交(for_submit=True)：对已关闭阀门再次关闭视为 CLOSE_PIPE_DUPLICATE。

    pending_events 为当前其他“待修复”隔离事件：其故障管段在方案前/方案后
    均视为不通（仍在检修），其关闭的阀门继续保持关闭。当前故障管段若已被
    其他待修复事件占用，则自动从待修复集合中排除（本次重新隔离同一条管段）。
    """
    issues = validate_plan_request(
        pipes, fault_pipe_id, close_pipe_ids, for_submit=for_submit
    )
    blocking = [i for i in issues if i.code in BLOCKING_ISSUE_CODES]

    fault = pipes.get(fault_pipe_id)
    if fault is None or blocking:
        # 结构不合法：无法进行可达性评估
        return PlanResult(
            isolatable=False, affected_sink_ids=[], issues=issues
        )

    pipe_list = list(pipes.values())

    # 其他待修复事件：故障管段仍断水，阀门仍关闭（不含本故障管段自身）
    others = [
        e
        for e in (pending_events or [])
        if e.status == EVENT_PENDING and e.fault_pipe_id != fault_pipe_id
    ]
    pending_fault_ids = frozenset(e.fault_pipe_id for e in others)
    pending_closed: set[str] = set()
    for e in others:
        pending_closed.update(e.closed_pipe_ids)

    # 方案前（当前态）：已关闭阀门 + 待修复故障管段不可通行，本次故障管段尚未移除
    already_closed = frozenset(
        p.id for p in pipe_list if p.has_valve and p.valve_closed
    ) | frozenset(pending_closed)
    before_removed = already_closed | pending_fault_ids
    before_reachable = _reachable(nodes, pipe_list, before_removed)

    # 方案后：故障管段移除；已关阀门继续生效；拟关阀门全部关闭
    blocked = already_closed | frozenset(close_pipe_ids)
    after_removed = blocked | pending_fault_ids | frozenset({fault_pipe_id})
    after_reachable = _reachable(nodes, pipe_list, after_removed)

    both_ends_isolated = (
        fault.from_node not in after_reachable
        and fault.to_node not in after_reachable
    )

    affected = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink" and nid in before_reachable and nid not in after_reachable
    )

    return PlanResult(
        isolatable=both_ends_isolated,
        affected_sink_ids=affected,
        issues=issues,
        blocked_pipe_ids=frozenset(blocked),
    )


# ---------------------------------------------------------------- 复供评估


def _block_reason_for_sink(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    sink_id: str,
    pending_faults: dict[str, IsolationEvent],
    closed_valve_ids: frozenset[str],
) -> Issue | None:
    """在“所有阀门开启、无故障管段移除”的完整图上找一条到 sink 的最短路径，
    沿路径找出当前使其不通的阻断物（优先返回离用水点最近的一个），
    归类为其他待修复故障管段或仍关闭的阀门。
    """
    _, _parent, _via = _bfs(nodes, pipes, frozenset())
    if sink_id not in _parent:
        return None  # 完整图上本就不可达，无法归因（无路径）

    # 回溯最短路径上的管段（从 sink 向水源）
    path_pipes: list[str] = []
    cur = sink_id
    while _parent.get(cur) is not None:
        path_pipes.append(_via[cur])
        cur = _parent[cur]

    for pid in path_pipes:
        if pid in pending_faults:
            evt = pending_faults[pid]
            return Issue(
                BLOCKED_BY_PENDING_FAULT,
                f"用水点 {sink_id} 仍无法恢复：待修复事件 #{evt.id} 的故障管段 {pid} 尚未修复",
                pipe_id=sink_id,
                event_id=evt.id,
                related_pipe_id=pid,
            )
        if pid in closed_valve_ids:
            return Issue(
                BLOCKED_BY_CLOSED_VALVE,
                f"用水点 {sink_id} 仍无法恢复：阀门 {pid} 仍处于关闭状态（其他待修复事件需要）",
                pipe_id=sink_id,
                related_pipe_id=pid,
            )
    return None


def evaluate_restoration(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    event: IsolationEvent,
    pending_events: list[IsolationEvent],
) -> RestoreResult:
    """评估复供方案（只读，不改任何状态）。

    通水图 = 当前管网重开“本事件关闭的阀门”，但继续排除：
      - 其他待修复事件的故障管段；
      - 仍被其他待修复事件占用（关闭）的阀门。

    安全性：重开后，其余每个待修复事件的故障管段两端必须仍均无法从水源
    到达；任一一端重新可达即存在“重新进水”风险，safe=False。
    """
    pipe_list = list(pipes.values())

    others = [
        e
        for e in pending_events
        if e.status == EVENT_PENDING and e.id != event.id
    ]
    pending_faults: dict[str, IsolationEvent] = {}
    for e in others:
        # 极端情况下多个待修复事件可能指向同一故障管段；记录其一即可
        pending_faults.setdefault(e.fault_pipe_id, e)

    # 仍关闭的阀门 = 被其他待修复事件占用的阀门 ∩ 本事件关过的阀门（去重）
    occupied: set[str] = set()
    for e in others:
        occupied.update(e.closed_pipe_ids)
    event_valves = frozenset(event.closed_pipe_ids)
    reopened = sorted(event_valves - occupied)
    still_closed = event_valves & occupied

    # 当前实际处于关闭状态的全部阀门（与阀位真值一致）
    closed_now = frozenset(
        p.id for p in pipe_list if p.has_valve and p.valve_closed
    )
    remaining_closed = closed_now - event_valves | still_closed

    pending_fault_ids = frozenset(pending_faults)

    # 复供前（本事件仍隔离中）：当前关闭阀门 + 全部待修复故障管段（含本事件）
    before_removed = closed_now | pending_fault_ids | frozenset({event.fault_pipe_id})
    before_reachable = _reachable(nodes, pipe_list, before_removed)

    # 复供后：重开本事件阀门（未被其他事件占用的部分），其余阻断保持
    after_removed = remaining_closed | pending_fault_ids
    after_reachable = _reachable(nodes, pipe_list, after_removed)

    # ---- 安全性：其他待修复故障管段两端仍均不可从水源到达 ----
    issues: list[Issue] = []
    risky = False
    for fault_id, evt in sorted(pending_faults.items()):
        fault = pipes.get(fault_id)
        if fault is None:
            continue
        reachable_ends = [
            end for end in (fault.from_node, fault.to_node) if end in after_reachable
        ]
        if reachable_ends:
            risky = True
            issues.append(
                Issue(
                    RESTORE_REWATER_RISK,
                    (
                        f"禁止复供：重开阀门会使待修复事件 #{evt.id} 的故障管段 "
                        f"{fault_id} 的端点 {sorted(reachable_ends)} 重新从水源进水"
                    ),
                    pipe_id=fault_id,
                    event_id=evt.id,
                )
            )

    # ---- 预计恢复 / 仍阻断的用水点 ----
    # 预计恢复：复供前不可达、复供后可达（且属于本事件原失供范围）
    recoverable = sorted(
        sid
        for sid in event.affected_sink_ids
        if sid not in before_reachable and sid in after_reachable
    )
    still_blocked = sorted(
        sid for sid in event.affected_sink_ids if sid not in after_reachable
    )
    for sid in still_blocked:
        reason = _block_reason_for_sink(
            nodes,
            pipe_list,
            sid,
            pending_faults=pending_faults,
            closed_valve_ids=remaining_closed,
        )
        if reason is not None:
            issues.append(reason)

    return RestoreResult(
        safe=not risky,
        reopened_pipe_ids=reopened,
        recoverable_sink_ids=recoverable,
        still_blocked_sink_ids=still_blocked,
        issues=issues,
        remaining_closed_pipe_ids=sorted(remaining_closed),
        remaining_fault_pipe_ids=sorted(pending_fault_ids),
    )


# ---------------------------------------------------------------- 局部修订
#
# 现场测绘发现管网资料有误时，操作员按基准版本对管网做局部增改删，
# 不清空既有隔离事件（含待修复事件）。修订规则：
#   - 请求列明每个变更与其对象编号；
#   - 新管段阀门默认开启（不得在修订中置为关闭）；
#   - 现有阀位只能由隔离提交/复供确认改变，修订不得改 valve_closed；
#   - 先在“完整候选管网”上做与整网导入相同的结构校验
#     （编号唯一、端点存在、无自环、无阀管段不可关闭）；
#   - 待修复事件引用的故障管段、关阀管段、失供用水点不得删除；
#     故障/关阀管段的端点、阀门属性与失供用水点类型不得改动；
#   - 修订后按“当前关阀 ∪ 待修复故障管段”断开的通水图复核：
#     每条待修复故障管段两端仍须均不可从水源到达。

# 补丁字段“保持原值”的哨兵（None 表示显式置空名称）
UNCHANGED = object()


@dataclass(frozen=True)
class NodePatch:
    id: str
    type: str | object = UNCHANGED
    name: str | object | None = UNCHANGED


@dataclass(frozen=True)
class PipePatch:
    id: str
    from_node: str | object = UNCHANGED
    to_node: str | object = UNCHANGED
    has_valve: bool | object = UNCHANGED
    # 阀位不可由修订改变；仅当请求显式给出时用于产生违规明细
    valve_closed: bool | object = UNCHANGED
    name: str | object | None = UNCHANGED


@dataclass(frozen=True)
class Revision:
    """一次局部修订请求（编号即为对象自身的业务编号）。"""

    add_nodes: tuple[Node, ...] = ()
    add_pipes: tuple[Pipe, ...] = ()
    update_nodes: tuple[NodePatch, ...] = ()
    update_pipes: tuple[PipePatch, ...] = ()
    delete_node_ids: tuple[str, ...] = ()
    delete_pipe_ids: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.add_nodes
            or self.add_pipes
            or self.update_nodes
            or self.update_pipes
            or self.delete_node_ids
            or self.delete_pipe_ids
        )


@dataclass
class RevisionResult:
    issues: list[Issue]
    nodes: dict[str, Node] = field(default_factory=dict)
    pipes: dict[str, Pipe] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.issues


def evaluate_revision(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    revision: Revision,
    pending_events: list[IsolationEvent] | None = None,
) -> RevisionResult:
    """评估局部修订：返回全部违约明细；无违约时给出修订后的候选管网。

    评估过程不修改入参。版本比对与原子写入由调用方（db 层）负责。
    """
    issues: list[Issue] = []

    if revision.is_empty():
        issues.append(Issue(CHANGESET_EMPTY, "变更集为空：至少需要一项增、改或删"))
        return RevisionResult(issues)

    pending = [e for e in (pending_events or []) if e.status == EVENT_PENDING]
    protected_pipes: dict[str, list[IsolationEvent]] = {}
    protected_sinks: dict[str, list[IsolationEvent]] = {}
    for e in pending:
        protected_pipes.setdefault(e.fault_pipe_id, []).append(e)
        for pid in e.closed_pipe_ids:
            protected_pipes.setdefault(pid, []).append(e)
        for sid in e.affected_sink_ids:
            protected_sinks.setdefault(sid, []).append(e)

    # ---- 1. 变更集记账：重号 / 不存在 / 同对象多操作 / 阀位规则 ----------
    add_node_by_id, add_pipe_by_id = _audit_revision(nodes, pipes, revision, issues)

    # ---- 2. 待修复事件引用保护 ------------------------------------------
    _check_revision_protection(
        revision, protected_pipes, protected_sinks, issues
    )

    # ---- 3. 构建候选管网：先删，再增/改 ---------------------------------
    cand_nodes, cand_pipes = _apply_revision_to_snapshot(nodes, pipes, revision)

    # ---- 4. 完整候选管网结构校验（同整网导入） --------------------------
    issues.extend(
        validate_import(
            [cand_nodes[nid] for nid in sorted(cand_nodes)],
            [cand_pipes[pid] for pid in sorted(cand_pipes)],
        )
    )

    # ---- 5. 关阀 + 待修复故障断开图上的通水复核 -------------------------
    closed_valves = frozenset(
        p.id
        for p in cand_pipes.values()
        if p.has_valve and p.valve_closed and p.id not in revision.delete_pipe_ids
    )
    removed = closed_valves | frozenset(protected_pipes)
    reachable = _reachable(cand_nodes, list(cand_pipes.values()), removed)
    seen_fault: set[str] = set()
    for e in sorted(pending, key=lambda ev: ev.id):
        fid = e.fault_pipe_id
        if fid in seen_fault:
            continue
        seen_fault.add(fid)
        fault = cand_pipes.get(fid)
        if fault is None:
            # 已在结构/保护检查中给出明细，跳过可达性复核
            continue
        wet_ends = sorted(
            end for end in (fault.from_node, fault.to_node) if end in reachable
        )
        if wet_ends:
            issues.append(
                Issue(
                    PENDING_FAULT_REWATER,
                    (
                        f"修订被拒绝：待修复事件 #{e.id} 的故障管段 {fid} 的端点 "
                        f"{wet_ends} 在修订后的通水图中可重新从水源到达（重新进水）"
                    ),
                    pipe_id=fid,
                    event_id=e.id,
                )
            )

    if issues:
        return RevisionResult(issues)
    return RevisionResult([], cand_nodes, cand_pipes)


def _audit_revision(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    revision: Revision,
    issues: list[Issue],
) -> tuple[dict[str, Node], dict[str, Pipe]]:
    """变更集本身的记账校验，返回新增对象映射（重复/冲突时不取首个之外的）。"""
    add_nodes: dict[str, Node] = {}
    for n in revision.add_nodes:
        if n.id in nodes:
            issues.append(
                Issue(
                    ADD_NODE_ID_EXISTS,
                    f"新增节点编号已存在（如需修改请走更新）：{n.id}",
                    node_id=n.id,
                )
            )
        if n.id in add_nodes:
            issues.append(
                Issue(ADD_NODE_DUPLICATE, f"本次新增节点编号重复：{n.id}", node_id=n.id)
            )
        else:
            add_nodes[n.id] = n

    add_pipes: dict[str, Pipe] = {}
    for p in revision.add_pipes:
        if p.id in pipes:
            issues.append(
                Issue(
                    ADD_PIPE_ID_EXISTS,
                    f"新增管段编号已存在（如需修改请走更新）：{p.id}",
                    pipe_id=p.id,
                )
            )
        if p.id in add_pipes:
            issues.append(
                Issue(ADD_PIPE_DUPLICATE, f"本次新增管段编号重复：{p.id}", pipe_id=p.id)
            )
        else:
            add_pipes[p.id] = p
        # 新管段阀门默认开启：不允许在修订中直接置为关闭
        if p.valve_closed:
            issues.append(
                Issue(
                    NEW_PIPE_VALVE_CLOSED,
                    f"新增管段 {p.id} 的阀门必须默认开启，不能在修订中关闭",
                    pipe_id=p.id,
                )
            )

    update_node_ids: set[str] = set()
    for patch in revision.update_nodes:
        if patch.id in update_node_ids:
            issues.append(
                Issue(
                    CHANGE_TARGET_DUPLICATE,
                    f"同一节点在本次变更中出现多次：{patch.id}",
                    node_id=patch.id,
                )
            )
        update_node_ids.add(patch.id)
        if patch.id not in nodes:
            issues.append(
                Issue(
                    UPDATE_NODE_NOT_FOUND,
                    f"待更新节点不存在：{patch.id}",
                    node_id=patch.id,
                )
            )

    update_pipe_ids: set[str] = set()
    for patch in revision.update_pipes:
        if patch.id in update_pipe_ids:
            issues.append(
                Issue(
                    CHANGE_TARGET_DUPLICATE,
                    f"同一管段在本次变更中出现多次：{patch.id}",
                    pipe_id=patch.id,
                )
            )
        update_pipe_ids.add(patch.id)
        if patch.id not in pipes:
            issues.append(
                Issue(
                    UPDATE_PIPE_NOT_FOUND,
                    f"待更新管段不存在：{patch.id}",
                    pipe_id=patch.id,
                )
            )
        # 现有阀位只能由隔离/复供流程改变
        if patch.valve_closed is not UNCHANGED:
            issues.append(
                Issue(
                    VALVE_STATE_IMMUTABLE,
                    f"管段 {patch.id} 的阀位只能由隔离提交或复供确认改变，修订不得修改",
                    pipe_id=patch.id,
                )
            )

    delete_node_ids: set[str] = set()
    for nid in revision.delete_node_ids:
        if nid in delete_node_ids:
            issues.append(
                Issue(
                    DELETE_NODE_DUPLICATE,
                    f"本次删除节点编号重复：{nid}",
                    node_id=nid,
                )
            )
        delete_node_ids.add(nid)
        if nid not in nodes:
            issues.append(
                Issue(
                    DELETE_NODE_NOT_FOUND,
                    f"待删除节点不存在：{nid}",
                    node_id=nid,
                )
            )

    delete_pipe_ids: set[str] = set()
    for pid in revision.delete_pipe_ids:
        if pid in delete_pipe_ids:
            issues.append(
                Issue(
                    DELETE_PIPE_DUPLICATE,
                    f"本次删除管段编号重复：{pid}",
                    pipe_id=pid,
                )
            )
        delete_pipe_ids.add(pid)
        if pid not in pipes:
            issues.append(
                Issue(
                    DELETE_PIPE_NOT_FOUND,
                    f"待删除管段不存在：{pid}",
                    pipe_id=pid,
                )
            )

    # 同一对象不得在同一请求中既增/改又删
    for nid in update_node_ids & delete_node_ids:
        issues.append(
            Issue(
                CHANGE_TARGET_CONFLICT,
                f"节点 {nid} 不能在同一次修订中既更新又删除",
                node_id=nid,
            )
        )
    for pid in update_pipe_ids & delete_pipe_ids:
        issues.append(
            Issue(
                CHANGE_TARGET_CONFLICT,
                f"管段 {pid} 不能在同一次修订中既更新又删除",
                pipe_id=pid,
            )
        )
    return add_nodes, add_pipes


def _check_revision_protection(
    revision: Revision,
    protected_pipes: dict[str, list[IsolationEvent]],
    protected_sinks: dict[str, list[IsolationEvent]],
    issues: list[Issue],
) -> None:
    """待修复事件引用对象的保护检查（每个对象每类违约只报一次）。"""
    node_patch_by_id = {p.id: p for p in revision.update_nodes}
    pipe_patch_by_id = {p.id: p for p in revision.update_pipes}
    delete_nodes = set(revision.delete_node_ids)
    delete_pipes = set(revision.delete_pipe_ids)

    def _event_refs(evts: list[IsolationEvent]) -> str:
        return ", ".join(f"#{e.id}" for e in sorted(evts, key=lambda ev: ev.id))

    for pid, evts in protected_pipes.items():
        ref = _event_refs(evts)
        if pid in delete_pipes:
            issues.append(
                Issue(
                    PROTECTED_PIPE_DELETE,
                    f"管段 {pid} 被待修复事件（{ref}）引用（故障管段或关阀管段），不得删除",
                    pipe_id=pid,
                    event_id=evts[0].id,
                )
            )
        patch = pipe_patch_by_id.get(pid)
        if patch is None:
            continue
        if patch.from_node is not UNCHANGED or patch.to_node is not UNCHANGED:
            issues.append(
                Issue(
                    PROTECTED_PIPE_ENDPOINT_CHANGE,
                    f"管段 {pid} 被待修复事件（{ref}）引用，其端点不得改动",
                    pipe_id=pid,
                    event_id=evts[0].id,
                )
            )
        if patch.has_valve is not UNCHANGED:
            issues.append(
                Issue(
                    PROTECTED_PIPE_VALVE_CHANGE,
                    f"管段 {pid} 被待修复事件（{ref}）引用，其阀门属性不得改动",
                    pipe_id=pid,
                    event_id=evts[0].id,
                )
            )

    for sid, evts in protected_sinks.items():
        ref = _event_refs(evts)
        if sid in delete_nodes:
            issues.append(
                Issue(
                    PROTECTED_SINK_DELETE,
                    f"用水点 {sid} 是待修复事件（{ref}）的失供用水点，不得删除",
                    node_id=sid,
                    event_id=evts[0].id,
                )
            )
        patch = node_patch_by_id.get(sid)
        if patch is not None and patch.type is not UNCHANGED:
            issues.append(
                Issue(
                    PROTECTED_SINK_TYPE_CHANGE,
                    f"用水点 {sid} 被待修复事件（{ref}）引用，其节点类型不得改动",
                    node_id=sid,
                    event_id=evts[0].id,
                )
            )


def _apply_revision_to_snapshot(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    revision: Revision,
) -> tuple[dict[str, Node], dict[str, Pipe]]:
    """按“先删、再增/改”应用变更集，得到候选管网（不校验合法性）。"""
    cand_nodes = dict(nodes)
    cand_pipes = dict(pipes)

    for nid in revision.delete_node_ids:
        cand_nodes.pop(nid, None)
    for pid in revision.delete_pipe_ids:
        cand_pipes.pop(pid, None)

    for n in revision.add_nodes:
        cand_nodes[n.id] = n
    for patch in revision.update_nodes:
        cur = cand_nodes.get(patch.id)
        if cur is None:
            continue
        cand_nodes[patch.id] = Node(
            id=cur.id,
            type=cur.type if patch.type is UNCHANGED else patch.type,
            name=cur.name if patch.name is UNCHANGED else patch.name,
        )

    for p in revision.add_pipes:
        # 新管段阀门默认开启：阀位永远以开启入库
        cand_pipes[p.id] = Pipe(
            id=p.id,
            from_node=p.from_node,
            to_node=p.to_node,
            has_valve=p.has_valve,
            valve_closed=False,
            name=p.name,
        )
    for patch in revision.update_pipes:
        cur = cand_pipes.get(patch.id)
        if cur is None:
            continue
        cand_pipes[patch.id] = Pipe(
            id=cur.id,
            from_node=cur.from_node if patch.from_node is UNCHANGED else patch.from_node,
            to_node=cur.to_node if patch.to_node is UNCHANGED else patch.to_node,
            has_valve=cur.has_valve if patch.has_valve is UNCHANGED else patch.has_valve,
            # 阀位不随修订变化（显式传 valve_closed 已在记账阶段判违规）
            valve_closed=cur.valve_closed,
            name=cur.name if patch.name is UNCHANGED else patch.name,
        )

    return cand_nodes, cand_pipes
