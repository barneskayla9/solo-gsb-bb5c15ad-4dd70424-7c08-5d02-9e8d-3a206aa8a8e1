"""API 路由层集成测试。

本机无 PostgreSQL，使用内存假连接替身替换 app.db 的数据访问函数，
覆盖 FastAPI 路由、请求校验、事务与错误码的完整行为。
运行：python3 -m unittest tests.test_api -v
（需要 fastapi/httpx 依赖）
"""
import json
import os
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.planning import Node, Pipe  # noqa: E402


class FakeStore:
    def __init__(self):
        self.version = 0
        self.updated_at = None
        self.nodes: dict[str, Node] = {}
        self.pipes: dict[str, Pipe] = {}
        self.events = []
        self.next_event_id = 1
        self.revision_no = 0
        self.fail_commit = False


STORE = FakeStore()


@contextmanager
def fake_get_conn():
    yield "FAKE_CONN"


@contextmanager
def fake_transaction():
    conn = "FAKE_TX"
    # 简单快照：提交时把暂存区落盘
    snapshot = (
        STORE.version,
        dict(STORE.nodes),
        {k: Pipe(**v.__dict__) for k, v in STORE.pipes.items()},
    )
    tmp = {
        "version": STORE.version,
        "nodes": dict(STORE.nodes),
        "pipes": dict(STORE.pipes),
        "events": list(STORE.events),
        "next_id": STORE.next_event_id,
    }
    try:
        yield conn
        if STORE.fail_commit:
            raise RuntimeError("simulated commit failure")
    except Exception:
        STORE.version = tmp["version"]
        STORE.nodes = tmp["nodes"]
        STORE.pipes = tmp["pipes"]
        STORE.events = tmp["events"]
        STORE.next_event_id = tmp["next_id"]
        raise


def fake_init_db():
    return None


def fake_load_snapshot(conn, for_update=False):
    return STORE.version, STORE.updated_at, dict(STORE.nodes), dict(STORE.pipes)


def fake_replace_network(conn, nodes, pipes):
    STORE.version = 1
    STORE.nodes = {n.id: n for n in nodes}
    STORE.pipes = {p.id: p for p in pipes}
    STORE.events = []
    STORE.next_event_id = 1
    STORE.revision_no = 0
    return 1


def fake_revision_change_ids(revision):
    # 与 db.revision_change_ids 相同的记账规则
    from app.planning import Revision  # noqa: F401

    changes = []
    changes.extend({"op": "add_node", "node_id": n.id} for n in revision.add_nodes)
    changes.extend({"op": "add_pipe", "pipe_id": p.id} for p in revision.add_pipes)
    changes.extend({"op": "update_node", "node_id": p.id} for p in revision.update_nodes)
    changes.extend({"op": "update_pipe", "pipe_id": p.id} for p in revision.update_pipes)
    changes.extend({"op": "delete_node", "node_id": nid} for nid in revision.delete_node_ids)
    changes.extend({"op": "delete_pipe", "pipe_id": pid} for pid in revision.delete_pipe_ids)
    return changes


def fake_apply_revision(conn, expected_version, revision, new_nodes, new_pipes, change_ids):
    # 复制 db.apply_revision 关键语义：锁行版本比对 -> 候选管网落盘 -> +1
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    STORE.nodes = dict(new_nodes)
    STORE.pipes = dict(new_pipes)
    before = STORE.version
    STORE.version = before + 1
    STORE.revision_no = getattr(STORE, "revision_no", 0) + 1
    return {
        "version": STORE.version,
        "revision_id": STORE.revision_no,
        "change_ids": change_ids,
    }


def fake_submit_isolation(conn, expected_version, fault_pipe_id, close_pipe_ids, result):
    # 复制 db.submit_isolation 的关键语义：锁行版本比对 -> 更新阀位 -> +1 -> 事件
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    for pid in close_pipe_ids:
        p = STORE.pipes[pid]
        STORE.pipes[pid] = Pipe(
            p.id, p.from_node, p.to_node, p.has_valve, True, p.name
        )
    before = STORE.version
    STORE.version += 1
    eid = STORE.next_event_id
    STORE.next_event_id += 1
    import datetime

    event = {
        "id": eid,
        "version_before": before,
        "version_after": STORE.version,
        "fault_pipe_id": fault_pipe_id,
        "closed_pipe_ids": list(close_pipe_ids),
        "affected_sink_ids": list(result.affected_sink_ids),
        "status": "pending",
        "restored_at": None,
        "restored_version": None,
        "reopened_pipe_ids": [],
        "created_at": datetime.datetime(2026, 9, 30, 12, 0, 0),
    }
    STORE.events.insert(0, event)
    return {"version": STORE.version, "event_id": eid, "created_at": event["created_at"]}


def fake_list_events(conn, limit):
    return STORE.events[:limit]


def fake_load_event(conn, event_id, for_update=False):
    for e in STORE.events:
        if e["id"] == event_id:
            return dict(e)
    return None


def fake_load_pending_events(conn):
    return [dict(e) for e in STORE.events if e["status"] == "pending"]


def fake_confirm_restoration(
    conn, expected_version, event_id, reopened_pipe_ids, recoverable_sink_ids
):
    import datetime

    # 复制 db.confirm_restoration 的关键语义：锁版本 -> 锁事件 -> 重开阀 -> +1 -> 置已复供
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    event = next((e for e in STORE.events if e["id"] == event_id), None)
    if event is None:
        raise db.EventNotFound(event_id)
    if event["status"] == "restored":
        raise db.EventAlreadyRestored(event_id)

    occupied = set()
    for e in STORE.events:
        if e["status"] == "pending" and e["id"] != event_id:
            occupied.update(e["closed_pipe_ids"])
    actual_reopen = [pid for pid in reopened_pipe_ids if pid not in occupied]
    for pid in actual_reopen:
        p = STORE.pipes[pid]
        STORE.pipes[pid] = Pipe(
            p.id, p.from_node, p.to_node, p.has_valve, False, p.name
        )

    STORE.version += 1
    event["status"] = "restored"
    event["restored_at"] = datetime.datetime(2026, 9, 30, 13, 0, 0)
    event["restored_version"] = STORE.version
    event["reopened_pipe_ids"] = list(actual_reopen)
    return {
        "version": STORE.version,
        "event_id": event_id,
        "reopened_pipe_ids": list(actual_reopen),
        "recoverable_sink_ids": list(recoverable_sink_ids),
    }


# 替换数据访问层
db.get_conn = fake_get_conn
db.transaction = fake_transaction
db.init_db = fake_init_db
db.load_snapshot = fake_load_snapshot
db.replace_network = fake_replace_network
db.apply_revision = fake_apply_revision
db.revision_change_ids = fake_revision_change_ids
db.submit_isolation = fake_submit_isolation
db.list_events = fake_list_events
db.load_event = fake_load_event
db.load_pending_events = fake_load_pending_events
db.confirm_restoration = fake_confirm_restoration


VALID_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source", "name": "一水厂"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
        {"id": "U3", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P5", "from": "B", "to": "U2", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
    ],
}

# 双故障复供场景：
# S--P1--A--P2--B--P4(v)--D--P6(故障)->U3
#                    |--P5(v)--U2      |--P7(v)--E--P8(故障)->U4
# A--P3(v)--U1
NESTED_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "E", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
        {"id": "U3", "type": "sink"},
        {"id": "U4", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P5", "from": "B", "to": "U2", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
        {"id": "P7", "from": "D", "to": "E", "has_valve": True},
        {"id": "P8", "from": "E", "to": "U4", "has_valve": False},
    ],
}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # lifespan 会调用 init_db（已替换为 fake）
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.fail_commit = False

    def _import(self):
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_full_happy_path(self):
        info = self._import()
        self.assertEqual(info["version"], 1)

        r = self.client.get("/api/network")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(len(body["nodes"]), 7)
        self.assertEqual(len(body["pipes"]), 6)
        # 序列化别名：from/to
        p1 = next(p for p in body["pipes"] if p["id"] == "P1")
        self.assertEqual(p1["from"], "S")
        self.assertEqual(p1["to"], "A")
        # 无阀管段 valve_closed 序列化为 null
        p6 = next(p for p in body["pipes"] if p["id"] == "P6")
        self.assertIsNone(p6["valve_closed"])

        # 不可行预演
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["isolatable"])

        # 可行预演
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertTrue(body["version_match"])

        # 提交成功
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertEqual(body["closed_pipe_ids"], ["P4"])
        self.assertEqual(body["event_id"], 1)

        # 阀位确实落库
        r = self.client.get("/api/network")
        p4 = next(p for p in r.json()["pipes"] if p["id"] == "P4")
        self.assertTrue(p4["valve_closed"])

        # 历史
        r = self.client.get("/api/isolation/events")
        self.assertEqual(r.status_code, 200)
        events = r.json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["fault_pipe_id"], "P6")
        self.assertEqual(events[0]["closed_pipe_ids"], ["P4"])

    def test_import_rejects_dangling_self_loop_duplicate(self):
        bad = {
            "nodes": [
                {"id": "S", "type": "source"},
                {"id": "S", "type": "junction"},
            ],
            "pipes": [
                {"id": "P1", "from": "S", "to": "X"},
                {"id": "P1", "from": "S", "to": "S"},
            ],
        }
        r = self.client.post("/api/network/import", json=bad)
        self.assertEqual(r.status_code, 422)
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertIn("DUPLICATE_NODE_ID", codes)
        self.assertIn("DUPLICATE_PIPE_ID", codes)
        self.assertIn("PIPE_ENDPOINT_UNKNOWN", codes)
        self.assertIn("PIPE_SELF_LOOP", codes)
        # 拒绝后状态不变（空网版本 0）
        r = self.client.get("/api/network")
        self.assertEqual(r.json()["version"], 0)

    def test_non_valve_pipe_initial_closed_rejected(self):
        bad = dict(VALID_NETWORK)
        bad["pipes"] = [
            dict(p, valve_closed=True) if p["id"] == "P6" else p
            for p in VALID_NETWORK["pipes"]
        ]
        r = self.client.post("/api/network/import", json=bad)
        self.assertEqual(r.status_code, 422)
        self.assertIn("NON_VALVE_PIPE_CLOSED", r.text)

    def test_submit_version_conflict(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 99},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 状态不变
        self.assertEqual(STORE.version, 1)

    def test_submit_no_valve_pipe(self):
        self._import()
        # 关闭无阀管段 P6（以 P5 为故障管段）-> 422 CLOSE_PIPE_NO_VALVE
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P6"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_NO_VALVE", r.text)
        self.assertEqual(STORE.version, 1)

    def test_submit_duplicate_and_already_closed(self):
        self._import()
        # 列表内重复
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4", "P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_DUPLICATE", r.text)

        # 先成功关 P4
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)

        # 版本 2 下再次关已关闭的 P4
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 2},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_DUPLICATE", r.text)

    def test_submit_not_isolatable(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "NOT_ISOLATABLE")
        self.assertEqual(STORE.version, 1)

    def test_submit_fault_pipe_as_valve(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P6"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_IS_FAULT", r.text)

    def test_preview_stale_version_flag(self):
        self._import()
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 5},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["version_match"])
        self.assertEqual(body["version"], 1)

    def test_request_body_validation_422(self):
        self._import()
        # version 不允许负数
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": [], "version": -1},
        )
        self.assertEqual(r.status_code, 422)

    def test_submit_against_empty_network_is_version_conflict(self):
        # 空网版本为 0：基于版本 1 提交应得到 409，而非参数校验错误
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")

    def test_reimport_resets_version_and_events(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(STORE.version, 2)
        # 重新导入 -> 版本重置为 1、历史清空
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.json()["version"], 1)
        self.assertEqual(STORE.events, [])

    def test_failed_submit_rolls_back(self):
        self._import()
        STORE.fail_commit = True
        # TestClient 默认抛出服务端异常；真实 HTTP 下客户端会收到 500。
        # 关键断言：事务回滚后版本与阀位均不变。
        with self.assertRaises(RuntimeError):
            self.client.post(
                "/api/isolation/submit",
                json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
            )
        self.assertEqual(STORE.version, 1)
        self.assertFalse(STORE.pipes["P4"].valve_closed)


class RestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.fail_commit = False

    def _import_nested(self):
        r = self.client.post("/api/network/import", json=NESTED_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)

    def _submit(self, fault, closes, version):
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": fault, "close_pipe_ids": closes, "version": version},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_restore_preview_does_not_change_state(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 2}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["safe"])
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["closed_pipe_ids"], ["P4"])
        self.assertEqual(body["reopened_pipe_ids"], ["P4"])
        self.assertEqual(body["recoverable_sink_ids"], ["U3", "U4"])
        self.assertEqual(body["still_blocked_sink_ids"], [])
        self.assertTrue(body["version_match"])
        # 只读：阀位、版本、事件状态均不变
        self.assertEqual(STORE.version, 2)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "pending")

    def test_restore_preview_stale_version_flag(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 1}
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["version_match"])
        self.assertEqual(r.json()["version"], 2)

    def test_restore_happy_path(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)

        # 确认复供：版本 2 -> 3，P4 重开，事件置为 restored，U3/U4 恢复
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 2}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 3)
        self.assertEqual(body["status"], "restored")
        self.assertEqual(body["reopened_pipe_ids"], ["P4"])
        self.assertEqual(body["recoverable_sink_ids"], ["U3", "U4"])

        self.assertFalse(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "restored")
        self.assertEqual(STORE.events[0]["restored_version"], 3)

        # 历史中可看到状态字段
        r = self.client.get("/api/isolation/events")
        ev = next(e for e in r.json() if e["id"] == 1)
        self.assertEqual(ev["status"], "restored")
        self.assertEqual(ev["reopened_pipe_ids"], ["P4"])

    def test_restore_event_not_found(self):
        self._import_nested()
        for path in ("restore/preview", "restore/confirm"):
            r = self.client.post(
                f"/api/isolation/events/999/{path}", json={"version": 1}
            )
            self.assertEqual(r.status_code, 404, r.text)
            self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")

    def test_restore_already_restored_rejected(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 2}
        )
        self.assertEqual(r.status_code, 200)

        # 重复确认 -> 409，状态不变
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 3}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "EVENT_ALREADY_RESTORED")

        # 对已复供事件预演同样明确拒绝
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "EVENT_ALREADY_RESTORED")

    def test_restore_version_conflict_no_partial_change(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 1}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 无部分变更
        self.assertEqual(STORE.version, 2)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "pending")

    def test_nested_fault_restore_order_and_rewater_risk(self):
        self._import_nested()
        # E1：关 P4 隔离 P6（U3/U4 失供）
        e1 = self._submit("P6", ["P4"], 1)
        self.assertEqual(e1["affected_sink_ids"], ["U3", "U4"])
        # E2：在 E1 隔离区内再隔离 P8（无需关阀；U4 已失供不重复计入）
        e2 = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P8", "close_pipe_ids": [], "version": 2},
        )
        self.assertEqual(e2.status_code, 200, e2.text)
        self.assertEqual(e2.json()["affected_sink_ids"], [])

        # 直接复供 E1：重开 P4 会让 E2 的故障管段 P8 重新进水 -> 422 RESTORE_UNSAFE
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 3}
        )
        body = r.json()
        self.assertFalse(body["safe"])
        self.assertEqual(body["recoverable_sink_ids"], ["U3"])
        self.assertEqual(body["still_blocked_sink_ids"], ["U4"])
        codes = {i["code"] for i in body["issues"]}
        self.assertIn("RESTORE_REWATER_RISK", codes)
        self.assertIn("BLOCKED_BY_PENDING_FAULT", codes)

        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 3}
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "RESTORE_UNSAFE")
        # 拒绝后无部分变更：P4 仍关、版本仍 3、E1 仍待修复
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.version, 3)
        self.assertEqual(STORE.events[1]["status"], "pending")

        # 先复供 E2：无阀门可重开，安全；P8 仍被 P4 隔离
        r = self.client.post(
            "/api/isolation/events/2/restore/confirm", json={"version": 3}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["reopened_pipe_ids"], [])
        self.assertTrue(STORE.pipes["P4"].valve_closed)

        # 再复供 E1：安全，P4 重开，U3/U4 恢复
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 4}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 5)
        self.assertEqual(body["reopened_pipe_ids"], ["P4"])
        self.assertEqual(body["recoverable_sink_ids"], ["U3", "U4"])
        self.assertFalse(STORE.pipes["P4"].valve_closed)

    def test_isolation_preview_counts_pending_faults(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        # E1 待修复时预演 E2：P6 故障管段与 P4 关闭阀继续计入，
        # U3/U4 在方案前已不可达，不重复计入失供
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P8", "close_pipe_ids": [], "version": 2},
        )
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], [])

    def test_reimport_invalidates_old_events(self):
        self._import_nested()
        self._submit("P6", ["P4"], 1)
        # 整网替换后旧事件失效（历史清空），再按旧编号复供 -> 404
        r = self.client.post("/api/network/import", json=NESTED_NETWORK)
        self.assertEqual(r.status_code, 200)
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 1}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")


class RevisionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.revision_no = 0
        STORE.fail_commit = False

    def _import(self):
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)

    def _isolate_p6(self):
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_revise_add_pipe_and_nodes_happy_path(self):
        self._import()
        payload = {
            "version": 1,
            "add_nodes": [
                {"id": "X", "type": "junction"},
                {"id": "U9", "type": "sink", "name": "新区"},
            ],
            "add_pipes": [
                {"id": "Q1", "from": "A", "to": "X", "has_valve": True},
                {"id": "Q2", "from": "X", "to": "U9", "has_valve": False},
            ],
        }
        r = self.client.post("/api/network/revise", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["revision_no"], 1)
        self.assertEqual(body["change_count"], 4)
        ops = [(c["op"], c.get("node_id") or c.get("pipe_id")) for c in body["change_ids"]]
        self.assertEqual(
            ops,
            [
                ("add_node", "X"),
                ("add_node", "U9"),
                ("add_pipe", "Q1"),
                ("add_pipe", "Q2"),
            ],
        )
        # 新管段阀门默认开启
        r = self.client.get("/api/network")
        q1 = next(p for p in r.json()["pipes"] if p["id"] == "Q1")
        self.assertFalse(q1["valve_closed"])
        # 隔离事件未被清空（本场景本来就无事件）
        self.assertEqual(STORE.version, 2)

    def test_revise_update_and_delete(self):
        self._import()
        payload = {
            "version": 1,
            "update_nodes": [{"id": "B", "name": "二号点"}],
            "update_pipes": [{"id": "P5", "name": "医院支管"}],
            "delete_pipe_ids": ["P3"],
            "delete_node_ids": ["U1"],
        }
        r = self.client.post("/api/network/revise", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["node_count"], 6)
        self.assertEqual(body["pipe_count"], 5)
        r = self.client.get("/api/network")
        b = next(n for n in r.json()["nodes"] if n["id"] == "B")
        self.assertEqual(b["name"], "二号点")
        self.assertNotIn("U1", {n["id"] for n in r.json()["nodes"]})

    def test_revise_version_conflict(self):
        self._import()
        r = self.client.post(
            "/api/network/revise",
            json={"version": 99, "update_nodes": [{"id": "B", "name": "x"}]},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        self.assertEqual(STORE.version, 1)

    def test_revise_empty_changeset(self):
        self._import()
        r = self.client.post("/api/network/revise", json={"version": 1})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "REVISION_INVALID")
        self.assertIn("CHANGESET_EMPTY", r.text)

    def test_revise_new_pipe_valve_must_default_open(self):
        self._import()
        # 新管段不接受 valve_closed 入参（新阀默认开启，关阀走隔离提交）
        r = self.client.post(
            "/api/network/revise",
            json={
                "version": 1,
                "add_nodes": [{"id": "X", "type": "junction"}],
                "add_pipes": [
                    {"id": "Q1", "from": "A", "to": "X", "has_valve": True,
                     "valve_closed": True}
                ],
            },
        )
        self.assertEqual(r.status_code, 422)
        # 拒绝后无部分变更
        self.assertEqual(STORE.version, 1)
        self.assertNotIn("X", STORE.nodes)

    def test_revise_new_pipe_defaults_open_without_field(self):
        self._import()
        r = self.client.post(
            "/api/network/revise",
            json={
                "version": 1,
                "add_nodes": [{"id": "X", "type": "junction"}],
                "add_pipes": [
                    {"id": "Q1", "from": "A", "to": "X", "has_valve": True}
                ],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        q1 = next(
            p for p in self.client.get("/api/network").json()["pipes"] if p["id"] == "Q1"
        )
        self.assertFalse(q1["valve_closed"])

    def test_revise_existing_valve_state_immutable(self):
        self._import()
        r = self.client.post(
            "/api/network/revise",
            json={
                "version": 1,
                "update_pipes": [{"id": "P1", "valve_closed": True}],
            },
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("VALVE_STATE_IMMUTABLE", r.text)

    def test_revise_candidate_self_loop_and_dangling(self):
        self._import()
        r = self.client.post(
            "/api/network/revise",
            json={
                "version": 1,
                "add_pipes": [
                    {"id": "Q1", "from": "A", "to": "A"},
                    {"id": "Q2", "from": "A", "to": "ZZ"},
                ],
            },
        )
        self.assertEqual(r.status_code, 422)
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertIn("PIPE_SELF_LOOP", codes)
        self.assertIn("PIPE_ENDPOINT_UNKNOWN", codes)

    def test_revise_protects_pending_event_objects(self):
        self._import()
        self._isolate_p6()  # 事件 #1 待修复：故障 P6、关阀 P4、失供 U3
        # 版本现为 2；删除受保护对象
        r = self.client.post(
            "/api/network/revise",
            json={
                "version": 2,
                "delete_pipe_ids": ["P6", "P4"],
                "delete_node_ids": ["U3"],
            },
        )
        self.assertEqual(r.status_code, 422, r.text)
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertIn("PROTECTED_PIPE_DELETE", codes)
        self.assertIn("PROTECTED_SINK_DELETE", codes)

        # 改动受保护属性：P6 端点、P4 阀门属性、U3 类型
        r = self.client.post(
            "/api/network/revise",
            json={
                "version": 2,
                "update_pipes": [
                    {"id": "P6", "from": "B"},
                    {"id": "P4", "has_valve": False},
                ],
                "update_nodes": [{"id": "U3", "type": "junction"}],
            },
        )
        self.assertEqual(r.status_code, 422, r.text)
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertIn("PROTECTED_PIPE_ENDPOINT_CHANGE", codes)
        self.assertIn("PROTECTED_PIPE_VALVE_CHANGE", codes)
        self.assertIn("PROTECTED_SINK_TYPE_CHANGE", codes)
        # 事务回滚：版本/阀位/管网不变，事件保留
        self.assertEqual(STORE.version, 2)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "pending")

    def test_revise_bypass_rewaters_pending_fault(self):
        self._import()
        self._isolate_p6()
        # 新增 B-U3 旁通管（阀门默认开启）：绕过关闭的 P4 -> P6 的 U3 端重新进水
        r = self.client.post(
            "/api/network/revise",
            json={
                "version": 2,
                "add_pipes": [{"id": "P9", "from": "B", "to": "U3", "has_valve": True}],
            },
        )
        self.assertEqual(r.status_code, 422)
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertIn("PENDING_FAULT_REWATER", codes)
        issue = next(
            i for i in r.json()["error"]["issues"] if i["code"] == "PENDING_FAULT_REWATER"
        )
        self.assertEqual(issue["event_id"], 1)
        self.assertEqual(issue["pipe_id"], "P6")
        # 拒绝后不写入
        self.assertNotIn("P9", STORE.pipes)
        self.assertEqual(STORE.version, 2)

    def test_revise_safe_while_pending_event_kept(self):
        self._import()
        self._isolate_p6()
        # 在与隔离区无关的支路上改名，且隔离事件不清空
        r = self.client.post(
            "/api/network/revise",
            json={
                "version": 2,
                "update_nodes": [{"id": "U1", "name": "幸福北苑"}],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 3)
        # 待修复事件仍在，P4 仍关闭
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(len(STORE.events), 1)
        self.assertEqual(STORE.events[0]["status"], "pending")
        # 版本 3 下仍可安全复供
        r = self.client.post(
            "/api/isolation/events/1/restore/confirm", json={"version": 3}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(STORE.pipes["P4"].valve_closed)

    def test_revise_rollback_on_failure_leaves_state_untouched(self):
        self._import()
        STORE.fail_commit = True
        with self.assertRaises(RuntimeError):
            self.client.post(
                "/api/network/revise",
                json={"version": 1, "update_nodes": [{"id": "B", "name": "x"}]},
            )
        self.assertEqual(STORE.version, 1)
        self.assertIsNone(STORE.nodes["B"].name)


if __name__ == "__main__":
    unittest.main()
