"""管网局部修订核心逻辑的单元测试（仅依赖标准库）。

运行：python3 -m unittest tests.test_revision -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.planning import (  # noqa: E402
    Node,
    Pipe,
    NodePatch,
    PipePatch,
    Revision,
    IsolationEvent,
    UNCHANGED,
    evaluate_revision,
    CHANGESET_EMPTY,
    ADD_NODE_ID_EXISTS,
    ADD_PIPE_ID_EXISTS,
    ADD_NODE_DUPLICATE,
    ADD_PIPE_DUPLICATE,
    UPDATE_NODE_NOT_FOUND,
    UPDATE_PIPE_NOT_FOUND,
    DELETE_NODE_NOT_FOUND,
    DELETE_PIPE_NOT_FOUND,
    DELETE_NODE_DUPLICATE,
    DELETE_PIPE_DUPLICATE,
    CHANGE_TARGET_DUPLICATE,
    CHANGE_TARGET_CONFLICT,
    NEW_PIPE_VALVE_CLOSED,
    VALVE_STATE_IMMUTABLE,
    PROTECTED_PIPE_DELETE,
    PROTECTED_SINK_DELETE,
    PROTECTED_PIPE_ENDPOINT_CHANGE,
    PROTECTED_PIPE_VALVE_CHANGE,
    PROTECTED_SINK_TYPE_CHANGE,
    PENDING_FAULT_REWATER,
    DUPLICATE_NODE_ID,
    DUPLICATE_PIPE_ID,
    PIPE_SELF_LOOP,
    PIPE_ENDPOINT_UNKNOWN,
    NON_VALVE_PIPE_CLOSED,
)


def build_net():
    """演示管网（同 test_planning.build_demo）：

    S --P1(v)-- A --P2(v)-- B --P4(v)-- D --P6(无阀)-- U3
                 |           |
               P3(v)       P5(v)
                 |           |
                 U1          U2
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
        "U3": Node("U3", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "U1", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True, valve_closed=True),
        "P5": Pipe("P5", "B", "U2", has_valve=True),
        "P6": Pipe("P6", "D", "U3", has_valve=False),
    }
    return nodes, pipes


def pending_event():
    # 待修复事件 #1：关 P4 隔离 P6，U3 失供
    return IsolationEvent(
        id=1,
        fault_pipe_id="P6",
        closed_pipe_ids=("P4",),
        affected_sink_ids=("U3",),
        version_before=1,
        version_after=2,
    )


def codes(result):
    return {i.code for i in result.issues}


class RevisionBookkeepingTests(unittest.TestCase):
    def setUp(self):
        self.nodes, self.pipes = build_net()

    def test_empty_changeset_rejected(self):
        result = evaluate_revision(self.nodes, self.pipes, Revision())
        self.assertFalse(result.ok)
        self.assertIn(CHANGESET_EMPTY, codes(result))

    def test_add_node_id_already_exists(self):
        rev = Revision(add_nodes=(Node("A", "junction"),))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(ADD_NODE_ID_EXISTS, codes(result))

    def test_add_pipe_id_already_exists(self):
        rev = Revision(add_pipes=(Pipe("P1", "S", "A"),))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(ADD_PIPE_ID_EXISTS, codes(result))

    def test_duplicate_added_ids(self):
        rev = Revision(
            add_nodes=(Node("X", "junction"), Node("X", "source")),
            add_pipes=(Pipe("Q1", "S", "X"), Pipe("Q1", "S", "X")),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(ADD_NODE_DUPLICATE, codes(result))
        self.assertIn(ADD_PIPE_DUPLICATE, codes(result))

    def test_update_missing_targets(self):
        rev = Revision(
            update_nodes=(NodePatch("ZZ"),),
            update_pipes=(PipePatch("ZZ"),),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(UPDATE_NODE_NOT_FOUND, codes(result))
        self.assertIn(UPDATE_PIPE_NOT_FOUND, codes(result))

    def test_delete_missing_targets(self):
        rev = Revision(delete_node_ids=("ZZ",), delete_pipe_ids=("ZZ",))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(DELETE_NODE_NOT_FOUND, codes(result))
        self.assertIn(DELETE_PIPE_NOT_FOUND, codes(result))

    def test_duplicate_delete_ids(self):
        rev = Revision(delete_node_ids=("U1", "U1"), delete_pipe_ids=("P5", "P5"))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(DELETE_NODE_DUPLICATE, codes(result))
        self.assertIn(DELETE_PIPE_DUPLICATE, codes(result))

    def test_duplicate_update_target(self):
        rev = Revision(
            update_nodes=(NodePatch("U1", name="a"), NodePatch("U1", name="b")),
            update_pipes=(PipePatch("P5", name="a"), PipePatch("P5", name="b")),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(CHANGE_TARGET_DUPLICATE, codes(result))

    def test_update_and_delete_same_target_conflict(self):
        rev = Revision(
            update_nodes=(NodePatch("U1"),),
            delete_node_ids=("U1",),
            update_pipes=(PipePatch("P5"),),
            delete_pipe_ids=("P5",),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev)
        conflict = [i for i in result.issues if i.code == CHANGE_TARGET_CONFLICT]
        refs = {i.node_id or i.pipe_id for i in conflict}
        self.assertEqual(refs, {"U1", "P5"})

    def test_new_pipe_closed_valve_rejected(self):
        rev = Revision(
            add_nodes=(Node("X", "junction"),),
            add_pipes=(Pipe("Q1", "X", "A", has_valve=True, valve_closed=True),),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(NEW_PIPE_VALVE_CLOSED, codes(result))

    def test_existing_valve_state_immutable(self):
        rev = Revision(update_pipes=(PipePatch("P5", valve_closed=True),))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(VALVE_STATE_IMMUTABLE, codes(result))


class RevisionCandidateValidationTests(unittest.TestCase):
    def setUp(self):
        self.nodes, self.pipes = build_net()

    def test_new_pipe_dangling_endpoint_rejected(self):
        rev = Revision(add_pipes=(Pipe("Q1", "A", "ZZ"),))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(PIPE_ENDPOINT_UNKNOWN, codes(result))
        # 无违约时才返回候选管网
        self.assertEqual(result.nodes, {})
        self.assertEqual(result.pipes, {})

    def test_new_pipe_self_loop_rejected(self):
        rev = Revision(add_pipes=(Pipe("Q1", "A", "A"),))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(PIPE_SELF_LOOP, codes(result))

    def test_update_endpoint_creates_self_loop_rejected(self):
        rev = Revision(update_pipes=(PipePatch("P5", from_node="U2"),))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(PIPE_SELF_LOOP, codes(result))

    def test_remove_valve_from_closed_pipe_rejected(self):
        # P4 当前阀位为关闭：去掉其阀门属性 -> 候选网存在“无阀却关闭”的管段
        rev = Revision(update_pipes=(PipePatch("P4", has_valve=False),))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(NON_VALVE_PIPE_CLOSED, codes(result))

    def test_duplicate_ids_cannot_arise_through_revision(self):
        # 编号不可通过新增与既有编号冲突（记账阶段已拦截），这里确认代码常量存在
        rev = Revision(add_nodes=(Node("U1", "junction"),))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(ADD_NODE_ID_EXISTS, codes(result))
        self.assertTrue(DUPLICATE_NODE_ID and DUPLICATE_PIPE_ID)


class RevisionProtectionTests(unittest.TestCase):
    def setUp(self):
        self.nodes, self.pipes = build_net()
        self.events = [pending_event()]

    def test_delete_fault_pipe_rejected(self):
        rev = Revision(delete_pipe_ids=("P6",))
        result = evaluate_revision(self.nodes, self.pipes, rev, self.events)
        issues = [i for i in result.issues if i.code == PROTECTED_PIPE_DELETE]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].pipe_id, "P6")
        self.assertEqual(issues[0].event_id, 1)

    def test_delete_closed_valve_pipe_rejected(self):
        rev = Revision(delete_pipe_ids=("P4",))
        result = evaluate_revision(self.nodes, self.pipes, rev, self.events)
        self.assertIn(PROTECTED_PIPE_DELETE, codes(result))

    def test_delete_affected_sink_rejected(self):
        rev = Revision(delete_node_ids=("U3",))
        result = evaluate_revision(self.nodes, self.pipes, rev, self.events)
        issues = [i for i in result.issues if i.code == PROTECTED_SINK_DELETE]
        self.assertEqual(issues[0].node_id, "U3")
        self.assertEqual(issues[0].event_id, 1)

    def test_change_protected_pipe_endpoints_rejected(self):
        rev = Revision(
            update_pipes=(PipePatch("P6", from_node="B"), PipePatch("P4", to_node="A"))
        )
        result = evaluate_revision(self.nodes, self.pipes, rev, self.events)
        ep = [i for i in result.issues if i.code == PROTECTED_PIPE_ENDPOINT_CHANGE]
        self.assertEqual({i.pipe_id for i in ep}, {"P4", "P6"})

    def test_change_protected_pipe_valve_attr_rejected(self):
        rev = Revision(update_pipes=(PipePatch("P6", has_valve=True),))
        result = evaluate_revision(self.nodes, self.pipes, rev, self.events)
        self.assertIn(PROTECTED_PIPE_VALVE_CHANGE, codes(result))

    def test_change_affected_sink_type_rejected(self):
        rev = Revision(update_nodes=(NodePatch("U3", type="junction"),))
        result = evaluate_revision(self.nodes, self.pipes, rev, self.events)
        self.assertIn(PROTECTED_SINK_TYPE_CHANGE, codes(result))

    def test_restored_events_do_not_protect(self):
        e = IsolationEvent(
            id=9,
            fault_pipe_id="P6",
            closed_pipe_ids=("P4",),
            affected_sink_ids=("U3",),
            status="restored",
        )
        rev = Revision(
            delete_pipe_ids=("P4", "P6"),
            delete_node_ids=("U3",),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev, [e])
        # 已复供事件不保护；删除后候选网无悬空/自环（P4/P6 及其端点同时移除）
        self.assertTrue(result.ok, [i.code for i in result.issues])
        self.assertNotIn("P4", result.pipes)
        self.assertNotIn("U3", result.nodes)


class RevisionReWaterTests(unittest.TestCase):
    def setUp(self):
        self.nodes, self.pipes = build_net()
        self.events = [pending_event()]

    def test_safe_revision_keeps_fault_isolated(self):
        # 在与故障区无关的支路 U1 上改名：故障 P6 两端仍不可达
        rev = Revision(
            update_nodes=(NodePatch("U1", name="新名称"),),
            update_pipes=(PipePatch("P3", name="支管阀"),),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev, self.events)
        self.assertTrue(result.ok, [i.code for i in result.issues])
        self.assertEqual(result.nodes["U1"].name, "新名称")
        self.assertEqual(result.pipes["P3"].name, "支管阀")
        # 阀位保持
        self.assertTrue(result.pipes["P4"].valve_closed)

    def test_add_bypass_pipe_rewaters_fault(self):
        # 新增 B--P9(v,开)-->U3：绕过关闭的 P4 直达故障管段端点 U3
        rev = Revision(add_pipes=(Pipe("P9", "B", "U3", has_valve=True),))
        result = evaluate_revision(self.nodes, self.pipes, rev, self.events)
        issues = [i for i in result.issues if i.code == PENDING_FAULT_REWATER]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].event_id, 1)
        self.assertEqual(issues[0].pipe_id, "P6")

    def test_add_path_via_new_nodes_rewaters_fault(self):
        # 新增节点 X 与 A-X-U3 通路 -> 重新进水
        rev = Revision(
            add_nodes=(Node("X", "junction"),),
            add_pipes=(
                Pipe("Q1", "A", "X", has_valve=False),
                Pipe("Q2", "X", "U3", has_valve=False),
            ),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev, self.events)
        self.assertIn(PENDING_FAULT_REWATER, codes(result))

    def test_add_pipe_within_isolated_zone_is_safe(self):
        # 在隔离区内部（D 与 U3 之间，P6 两端本就都不可达）新增无阀旁通管：
        # 两端都在断水区内，故障两端仍不可达
        rev = Revision(
            add_nodes=(Node("X", "junction"),),
            add_pipes=(
                Pipe("Q1", "D", "X", has_valve=False),
                Pipe("Q2", "X", "U3", has_valve=False),
            ),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev, self.events)
        self.assertTrue(result.ok, [i.code for i in result.issues])
        self.assertIn("X", result.nodes)
        # 新管段阀门默认开启
        self.assertFalse(result.pipes["Q1"].valve_closed)

    def test_without_pending_events_any_topology_change_is_allowed(self):
        # 没有待修复事件：同样的旁通管不再触发通水复核
        rev = Revision(add_pipes=(Pipe("P9", "B", "U3", has_valve=True),))
        result = evaluate_revision(self.nodes, self.pipes, rev, [])
        self.assertTrue(result.ok, [i.code for i in result.issues])


class RevisionApplyShapeTests(unittest.TestCase):
    def setUp(self):
        self.nodes, self.pipes = build_net()

    def test_add_node_and_pipe_default_open_valve(self):
        rev = Revision(
            add_nodes=(Node("X", "junction"), Node("U9", "sink")),
            add_pipes=(
                Pipe("Q1", "A", "X", has_valve=True),
                Pipe("Q2", "X", "U9", has_valve=False),
            ),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertTrue(result.ok, [i.code for i in result.issues])
        self.assertFalse(result.pipes["Q1"].valve_closed)
        self.assertEqual(len(result.nodes), 9)
        self.assertEqual(len(result.pipes), 8)

    def test_update_name_only_keeps_other_fields(self):
        rev = Revision(
            update_nodes=(NodePatch("B", name="二号点"),),
            update_pipes=(PipePatch("P5", name="医院支管"),),
        )
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertTrue(result.ok)
        self.assertEqual(result.nodes["B"].type, "junction")
        p5 = result.pipes["P5"]
        self.assertEqual(p5.from_node, "B")
        self.assertEqual(p5.to_node, "U2")
        self.assertTrue(p5.has_valve)
        self.assertFalse(p5.valve_closed)
        self.assertEqual(p5.name, "医院支管")

    def test_delete_node_with_connected_pipe_leaves_dangling_pipe_rejected(self):
        # 删除节点 U2 但保留 P5 -> P5 端点悬空
        rev = Revision(delete_node_ids=("U2",))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertIn(PIPE_ENDPOINT_UNKNOWN, codes(result))

    def test_delete_pipe_and_leaf_node_together_ok(self):
        # 同时删 P5 与叶子用水点 U2：候选网合法
        rev = Revision(delete_pipe_ids=("P5",), delete_node_ids=("U2",))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertTrue(result.ok, [i.code for i in result.issues])
        self.assertNotIn("P5", result.pipes)
        self.assertNotIn("U2", result.nodes)
        self.assertEqual(len(result.pipes), 5)

    def test_switch_sink_type_changes_reachability_semantics_but_valid(self):
        # U2 改为 junction 不违反结构校验（无待修复事件引用 U2）
        rev = Revision(update_nodes=(NodePatch("U2", type="junction"),))
        result = evaluate_revision(self.nodes, self.pipes, rev)
        self.assertTrue(result.ok)
        self.assertEqual(result.nodes["U2"].type, "junction")


if __name__ == "__main__":
    unittest.main()
