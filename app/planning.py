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

# 局部修订：变更清单本身的问题（422 REVISION_INVALID）
REVISION_DUPLICATE_CHANGE = "REVISION_DUPLICATE_CHANGE"
REVISION_NODE_NOT_FOUND = "REVISION_NODE_NOT_FOUND"
REVISION_PIPE_NOT_FOUND = "REVISION_PIPE_NOT_FOUND"
REVISION_ID_ALREADY_EXISTS = "REVISION_ID_ALREADY_EXISTS"
# 新增/修改管段时不得通过修订改变阀位（阀位只能由隔离/复供流程改变）
REVISION_VALVE_CLOSED_FORBIDDEN = "REVISION_VALVE_CLOSED_FORBIDDEN"
# 局部修订：待修复事件保护（422 REVISION_INVALID）
PROTECTED_PIPE_DELETE_BY_EVENT = "PROTECTED_PIPE_DELETE_BY_EVENT"
PROTECTED_NODE_DELETE_BY_EVENT = "PROTECTED_NODE_DELETE_BY_EVENT"
PROTECTED_PIPE_ENDPOINT_CHANGE = "PROTECTED_PIPE_ENDPOINT_CHANGE"
PROTECTED_PIPE_VALVE_CHANGE = "PROTECTED_PIPE_VALVE_CHANGE"
PROTECTED_SINK_TYPE_CHANGE = "PROTECTED_SINK_TYPE_CHANGE"
# 局部修订：修订后通水复核失败（待修复故障管段端点重新可达）
REVISION_FAULT_REACHABLE = "REVISION_FAULT_REACHABLE"

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
    # 涉及的节点编号（节点类问题；保持在末尾以向后兼容旧的构造方式）
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


# ---------------------------------------------------------------- 局部管网修订
#
# 现场测绘发现资料有误时，操作员基于基准版本对节点/管段做局部增改删，
# 不清空隔离事件。阀位只能由隔离、复供流程改变，因此：
#   - 修订不接受 valve_closed：新增管段的阀门默认开启（valve_closed=False）；
#   - 修改管段时阀位沿用当前真值；
#   - 待修复事件引用的故障管段、关阀管段、失供用水点受保护，不得删除，
#     其管段端点、阀门属性及用水点类型不得改动。
# 修订后须按“当前关阀 ∪ 待修复故障管段”断开的通水图复核：每条待修复
# 故障管段两端仍须均不可从水源到达，否则整次修订拒绝（不写入）。


@dataclass(frozen=True)
class NodeChange:
    """一次节点新增/修改。"""

    id: str
    type: str  # source / sink / junction
    name: str | None = None


@dataclass(frozen=True)
class PipeChange:
    """一次管段新增/修改。

    valve_closed 仅用于识别调用方是否显式传入 true：
    显式 true 一律拒绝；None 表示未提供。修订成功后：
    新增管段阀位为 False，修改管段阀位沿用旧真值。
    """

    id: str
    from_node: str
    to_node: str
    has_valve: bool = False
    valve_closed: bool | None = None
    name: str | None = None


@dataclass
class RevisionCheck:
    """局部修订的校验与复核结果。

    valid=True 时 new_nodes/new_pipes 为应用变更后的完整候选管网，
    可直接交给持久层原子写入；valid=False 时 issues 给出全部违约明细。
    """

    valid: bool
    issues: list[Issue]
    new_nodes: dict[str, Node] = field(default_factory=dict)
    new_pipes: dict[str, Pipe] = field(default_factory=dict)
    added_node_ids: list[str] = field(default_factory=list)
    updated_node_ids: list[str] = field(default_factory=list)
    deleted_node_ids: list[str] = field(default_factory=list)
    added_pipe_ids: list[str] = field(default_factory=list)
    updated_pipe_ids: list[str] = field(default_factory=list)
    deleted_pipe_ids: list[str] = field(default_factory=list)

    @property
    def change_ids(self) -> dict[str, list[str]]:
        return {
            "added_node_ids": list(self.added_node_ids),
            "updated_node_ids": list(self.updated_node_ids),
            "deleted_node_ids": list(self.deleted_node_ids),
            "added_pipe_ids": list(self.added_pipe_ids),
            "updated_pipe_ids": list(self.updated_pipe_ids),
            "deleted_pipe_ids": list(self.deleted_pipe_ids),
        }


def _pending(events: list[IsolationEvent] | None) -> list[IsolationEvent]:
    return [e for e in (events or []) if e.status == EVENT_PENDING]


def check_revision(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    *,
    added_nodes: list[NodeChange],
    updated_nodes: list[NodeChange],
    deleted_node_ids: list[str],
    added_pipes: list[PipeChange],
    updated_pipes: list[PipeChange],
    deleted_pipe_ids: list[str],
    pending_events: list[IsolationEvent] | None = None,
) -> RevisionCheck:
    """校验局部修订并构建修订后的完整候选管网。

    分阶段执行，任一阶段存在问题即短路返回（issues 携带对象/事件编号与原因）：
      1. 变更清单语义：同一对象编号不得出现多个变更；改/删对象必须已存在，
         新增编号不得与现存编号冲突；
      2. 待修复事件保护：故障管段、关阀管段、失供用水点不得删除；
         管段端点、阀门属性、用水点类型不得改动；
      3. 完整候选管网结构校验：编号唯一、端点存在、无自环、无阀管段不可关闭
         （复用整网导入校验）；
      4. 通水复核：按当前关阀与待修复故障管段断开后，每条待修复故障管段
         两端仍须均不可从水源到达。
    """
    issues: list[Issue] = []

    # ---- 1. 变更清单语义 ------------------------------------------------
    node_buckets = (
        ("add", added_nodes, lambda c: c.id),
        ("update", updated_nodes, lambda c: c.id),
        ("delete", deleted_node_ids, lambda x: x),
    )
    pipe_buckets = (
        ("add", added_pipes, lambda c: c.id),
        ("update", updated_pipes, lambda c: c.id),
        ("delete", deleted_pipe_ids, lambda x: x),
    )

    def collect(buckets):
        where: dict[str, list[str]] = {}
        for label, items, keyof in buckets:
            for item in items:
                where.setdefault(keyof(item), []).append(label)
        return where

    node_where = collect(node_buckets)
    pipe_where = collect(pipe_buckets)

    for nid, labels in sorted(node_where.items()):
        if len(labels) > 1:
            issues.append(
                Issue(
                    REVISION_DUPLICATE_CHANGE,
                    f"节点 {nid} 在同一次修订中出现多个变更：{'/'.join(labels)}",
                    node_id=nid,
                )
            )
    for pid, labels in sorted(pipe_where.items()):
        if len(labels) > 1:
            issues.append(
                Issue(
                    REVISION_DUPLICATE_CHANGE,
                    f"管段 {pid} 在同一次修订中出现多个变更：{'/'.join(labels)}",
                    pipe_id=pid,
                )
            )

    for c in added_nodes:
        if len(node_where[c.id]) == 1 and c.id in nodes:
            issues.append(
                Issue(
                    REVISION_ID_ALREADY_EXISTS,
                    f"新增节点编号已存在：{c.id}",
                    node_id=c.id,
                )
            )
    for c in updated_nodes:
        if len(node_where[c.id]) == 1 and c.id not in nodes:
            issues.append(
                Issue(REVISION_NODE_NOT_FOUND, f"待修改节点不存在：{c.id}", node_id=c.id)
            )
    for nid in deleted_node_ids:
        if len(node_where[nid]) == 1 and nid not in nodes:
            issues.append(
                Issue(REVISION_NODE_NOT_FOUND, f"待删除节点不存在：{nid}", node_id=nid)
            )

    for c in added_pipes:
        if len(pipe_where[c.id]) == 1 and c.id in pipes:
            issues.append(
                Issue(
                    REVISION_ID_ALREADY_EXISTS,
                    f"新增管段编号已存在：{c.id}",
                    pipe_id=c.id,
                )
            )
        if c.valve_closed is True:
            issues.append(
                Issue(
                    REVISION_VALVE_CLOSED_FORBIDDEN,
                    f"新增管段 {c.id} 的阀门默认开启，不能在修订中标记为关闭；"
                    "阀位只能由隔离/复供流程改变",
                    pipe_id=c.id,
                )
            )
    for c in updated_pipes:
        if len(pipe_where[c.id]) == 1 and c.id not in pipes:
            issues.append(
                Issue(REVISION_PIPE_NOT_FOUND, f"待修改管段不存在：{c.id}", pipe_id=c.id)
            )
        if c.valve_closed is True:
            issues.append(
                Issue(
                    REVISION_VALVE_CLOSED_FORBIDDEN,
                    f"管段 {c.id} 的阀位只能由隔离/复供流程改变，不能在修订中关闭",
                    pipe_id=c.id,
                )
            )
    for pid in deleted_pipe_ids:
        if len(pipe_where[pid]) == 1 and pid not in pipes:
            issues.append(
                Issue(REVISION_PIPE_NOT_FOUND, f"待删除管段不存在：{pid}", pipe_id=pid)
            )

    if issues:
        return RevisionCheck(valid=False, issues=issues)

    # ---- 2. 待修复事件保护 ----------------------------------------------
    pending = _pending(pending_events)
    # 受保护端点节点 -> 首个引用它的事件编号（用于给出事件引用）
    protected_node_events: dict[str, int] = {}

    for event in pending:
        fault = pipes.get(event.fault_pipe_id)
        if fault is not None:
            for end in (fault.from_node, fault.to_node):
                protected_node_events.setdefault(end, event.id)

        # 故障管段 / 关阀管段：不得删除
        for pid in (*event.closed_pipe_ids, event.fault_pipe_id):
            if pid in deleted_pipe_ids:
                issues.append(
                    Issue(
                        PROTECTED_PIPE_DELETE_BY_EVENT,
                        f"管段 {pid} 被待修复事件 #{event.id} 引用"
                        f"（{'关阀管段' if pid in event.closed_pipe_ids else '故障管段'}），"
                        "修复复供前不得删除",
                        pipe_id=pid,
                        event_id=event.id,
                    )
                )
            new = next((c for c in updated_pipes if c.id == pid), None)
            if new is not None:
                old = pipes.get(pid)
                if old is not None:
                    if {old.from_node, old.to_node} != {new.from_node, new.to_node}:
                        issues.append(
                            Issue(
                                PROTECTED_PIPE_ENDPOINT_CHANGE,
                                f"管段 {pid} 被待修复事件 #{event.id} 引用，"
                                f"其端点不得由 {old.from_node}-{old.to_node} "
                                f"改为 {new.from_node}-{new.to_node}",
                                pipe_id=pid,
                                event_id=event.id,
                            )
                        )
                    if old.has_valve != new.has_valve:
                        issues.append(
                            Issue(
                                PROTECTED_PIPE_VALVE_CHANGE,
                                f"管段 {pid} 被待修复事件 #{event.id} 引用，"
                                f"其阀门属性不得由 has_valve={old.has_valve} "
                                f"改为 has_valve={new.has_valve}",
                                pipe_id=pid,
                                event_id=event.id,
                            )
                        )

        # 失供用水点：不得删除、类型不得改动
        for sink_id in event.affected_sink_ids:
            if sink_id in deleted_node_ids:
                issues.append(
                    Issue(
                        PROTECTED_NODE_DELETE_BY_EVENT,
                        f"用水点 {sink_id} 是待修复事件 #{event.id} 的失供用水点，"
                        "修复复供前不得删除",
                        node_id=sink_id,
                        event_id=event.id,
                        related_pipe_id=event.fault_pipe_id,
                    )
                )
            new_node = next((c for c in updated_nodes if c.id == sink_id), None)
            old_node = nodes.get(sink_id)
            if new_node is not None and old_node is not None and new_node.type != old_node.type:
                issues.append(
                    Issue(
                        PROTECTED_SINK_TYPE_CHANGE,
                        f"用水点 {sink_id} 是待修复事件 #{event.id} 的失供用水点，"
                        f"其类型不得由 {old_node.type} 改为 {new_node.type}",
                        node_id=sink_id,
                        event_id=event.id,
                        related_pipe_id=event.fault_pipe_id,
                    )
                )

    # 受保护管段（故障/关阀）的端点节点不得删除，否则会悬空或改变引用拓扑。
    # 已作为失供用水点上报的节点不重复上报。
    sink_delete_events = {
        nid
        for nid in deleted_node_ids
        if any(nid in event.affected_sink_ids for event in pending)
    }
    for nid in deleted_node_ids:
        if nid in protected_node_events and nid not in sink_delete_events:
            issues.append(
                Issue(
                    PROTECTED_NODE_DELETE_BY_EVENT,
                    f"节点 {nid} 是待修复事件 #{protected_node_events[nid]} 引用的"
                    "故障/关阀管段端点，修复复供前不得删除",
                    node_id=nid,
                    event_id=protected_node_events[nid],
                )
            )

    if issues:
        return RevisionCheck(valid=False, issues=issues)

    # ---- 3. 应用变更，构建完整候选管网 ----------------------------------
    new_nodes = dict(nodes)
    for nid in deleted_node_ids:
        new_nodes.pop(nid, None)
    for c in (*added_nodes, *updated_nodes):
        new_nodes[c.id] = Node(id=c.id, type=c.type, name=c.name)

    new_pipes = dict(pipes)
    for pid in deleted_pipe_ids:
        new_pipes.pop(pid, None)
    for c in added_pipes:
        # 新增管段阀门默认开启
        new_pipes[c.id] = Pipe(
            id=c.id,
            from_node=c.from_node,
            to_node=c.to_node,
            has_valve=c.has_valve,
            valve_closed=False,
            name=c.name,
        )
    for c in updated_pipes:
        old = pipes[c.id]
        # 修改管段不改变阀位真值（阀位只能由隔离/复供流程改变）；
        # 去掉阀门后 valve_closed 规范为 False（结构校验同样会拒绝
        # “无阀且关闭”的候选，二者保持一致）
        new_pipes[c.id] = Pipe(
            id=c.id,
            from_node=c.from_node,
            to_node=c.to_node,
            has_valve=c.has_valve,
            valve_closed=bool(old.valve_closed and c.has_valve),
            name=c.name,
        )

    structure_issues = validate_import(
        list(new_nodes.values()), list(new_pipes.values())
    )
    if structure_issues:
        return RevisionCheck(valid=False, issues=structure_issues)

    # ---- 4. 修订后通水复核 ----------------------------------------------
    # 断开通水图 = 当前关闭的阀门 ∪ 待修复故障管段。
    removed = frozenset(
        p.id for p in new_pipes.values() if p.has_valve and p.valve_closed
    ) | frozenset(e.fault_pipe_id for e in pending)
    reachable = _reachable(new_nodes, list(new_pipes.values()), removed)

    seen_faults: set[str] = set()
    for event in pending:
        fid = event.fault_pipe_id
        if fid in seen_faults:
            continue
        seen_faults.add(fid)
        fault = new_pipes.get(fid)
        if fault is None:
            # 理论上不会发生（第 2 阶段已拦截删除），仍显式拒绝以防部分状态
            issues.append(
                Issue(
                    PROTECTED_PIPE_DELETE_BY_EVENT,
                    f"待修复事件 #{event.id} 的故障管段 {fid} 在修订后不存在",
                    pipe_id=fid,
                    event_id=event.id,
                )
            )
            continue
        reachable_ends = [
            end for end in (fault.from_node, fault.to_node) if end in reachable
        ]
        if reachable_ends:
            issues.append(
                Issue(
                    REVISION_FAULT_REACHABLE,
                    f"修订后待修复事件 #{event.id} 的故障管段 {fid} 的端点 "
                    f"{sorted(reachable_ends)} 可从水源到达，隔离被破坏，禁止修订",
                    pipe_id=fid,
                    event_id=event.id,
                )
            )

    if issues:
        return RevisionCheck(valid=False, issues=issues)

    return RevisionCheck(
        valid=True,
        issues=[],
        new_nodes=new_nodes,
        new_pipes=new_pipes,
        added_node_ids=sorted(c.id for c in added_nodes),
        updated_node_ids=sorted(c.id for c in updated_nodes),
        deleted_node_ids=sorted(deleted_node_ids),
        added_pipe_ids=sorted(c.id for c in added_pipes),
        updated_pipe_ids=sorted(c.id for c in updated_pipes),
        deleted_pipe_ids=sorted(deleted_pipe_ids),
    )
