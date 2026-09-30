#!/usr/bin/env python3
"""标准库内存后端：复用 app.planning 领域层，提供与 FastAPI 服务相同的 HTTP 契约。

仅用于在没有 PostgreSQL / FastAPI 的环境中本地验证与跑示例脚本
（examples/demo.py、restore_demo.py、revise_demo.py）。
生产部署仍使用 Docker Compose（FastAPI + PostgreSQL）。

用法：python3 tools/stdlib_server.py [port]
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.planning import (  # noqa: E402
    Node,
    Pipe,
    NodePatch,
    PipePatch,
    Revision,
    UNCHANGED,
    EVENT_PENDING,
    evaluate_plan,
    evaluate_restoration,
    evaluate_revision,
    validate_import,
    BLOCKING_ISSUE_CODES,
    IsolationEvent,
)

STATE = {
    "version": 0,
    "updated_at": None,
    "nodes": {},
    "pipes": {},
    "events": [],
    "next_event_id": 1,
    "revision_no": 0,
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue_dict(i):
    d = {"code": i.code, "message": i.message, "pipe_id": i.pipe_id}
    if i.event_id is not None:
        d["event_id"] = i.event_id
    if i.related_pipe_id is not None:
        d["related_pipe_id"] = i.related_pipe_id
    if getattr(i, "node_id", None) is not None:
        d["node_id"] = i.node_id
    return d


def serialize_pipe(p):
    return {
        "id": p.id,
        "from": p.from_node,
        "to": p.to_node,
        "has_valve": p.has_valve,
        "valve_closed": p.valve_closed if p.has_valve else None,
        "name": p.name,
    }


def serialize_node(n):
    return {"id": n.id, "type": n.type, "name": n.name}


def snapshot():
    return {
        "version": STATE["version"],
        "updated_at": STATE["updated_at"],
        "nodes": [serialize_node(STATE["nodes"][k]) for k in sorted(STATE["nodes"])],
        "pipes": [serialize_pipe(STATE["pipes"][k]) for k in sorted(STATE["pipes"])],
    }


def domain_events():
    out = []
    for r in STATE["events"]:
        out.append(
            IsolationEvent(
                id=r["id"],
                fault_pipe_id=r["fault_pipe_id"],
                closed_pipe_ids=tuple(r["closed_pipe_ids"]),
                affected_sink_ids=tuple(r["affected_sink_ids"]),
                status=r["status"],
                version_before=r["version_before"],
                version_after=r["version_after"],
            )
        )
    return out


def pending_events():
    return [e for e in domain_events() if e.status == EVENT_PENDING]


def build_nodes(items):
    return [Node(id=n["id"], type=n["type"], name=n.get("name")) for n in items]


def build_pipes(items, *, allow_valve_closed=True):
    out = []
    for p in items:
        if not allow_valve_closed and "valve_closed" in p:
            raise ValueError("add_pipes 不接受 valve_closed：新管段阀门默认开启")
        out.append(
            Pipe(
                id=p["id"],
                from_node=p["from"],
                to_node=p["to"],
                has_valve=p.get("has_valve", False),
                valve_closed=p.get("valve_closed", False) if allow_valve_closed else False,
                name=p.get("name"),
            )
        )
    return out


def build_revision(payload):
    """返回 Revision；若新增管段非法携带 valve_closed 抛 ValueError。"""
    add_nodes = tuple(build_nodes(payload.get("add_nodes", [])))
    add_pipes = tuple(build_pipes(payload.get("add_pipes", []), allow_valve_closed=False))

    node_patches = []
    for p in payload.get("update_nodes", []):
        provided = set(p.keys())
        node_patches.append(
            NodePatch(
                id=p["id"],
                type=p["type"] if "type" in provided else UNCHANGED,
                name=p["name"] if "name" in provided else UNCHANGED,
            )
        )
    pipe_patches = []
    for p in payload.get("update_pipes", []):
        provided = set(p.keys())
        pipe_patches.append(
            PipePatch(
                id=p["id"],
                from_node=p["from"] if "from" in provided else UNCHANGED,
                to_node=p["to"] if "to" in provided else UNCHANGED,
                has_valve=p["has_valve"] if "has_valve" in provided else UNCHANGED,
                valve_closed=p["valve_closed"] if "valve_closed" in provided else UNCHANGED,
                name=p["name"] if "name" in provided else UNCHANGED,
            )
        )
    return Revision(
        add_nodes=add_nodes,
        add_pipes=add_pipes,
        update_nodes=tuple(node_patches),
        update_pipes=tuple(pipe_patches),
        delete_node_ids=tuple(payload.get("delete_node_ids", [])),
        delete_pipe_ids=tuple(payload.get("delete_pipe_ids", [])),
    )


def revision_changes(rev):
    changes = []
    changes.extend({"op": "add_node", "node_id": n.id, "pipe_id": None} for n in rev.add_nodes)
    changes.extend({"op": "add_pipe", "pipe_id": p.id, "node_id": None} for p in rev.add_pipes)
    changes.extend(
        {"op": "update_node", "node_id": p.id, "pipe_id": None} for p in rev.update_nodes
    )
    changes.extend(
        {"op": "update_pipe", "pipe_id": p.id, "node_id": None} for p in rev.update_pipes
    )
    changes.extend(
        {"op": "delete_node", "node_id": nid, "pipe_id": None}
        for nid in rev.delete_node_ids
    )
    changes.extend(
        {"op": "delete_pipe", "pipe_id": pid, "node_id": None}
        for pid in rev.delete_pipe_ids
    )
    return changes


def err(code, message, status=422, issues=None, **extra):
    body = {"error": {"code": code, "message": message}}
    if issues:
        body["error"]["issues"] = [issue_dict(i) for i in issues]
    body["error"].update(extra)
    return status, body


def handle(path, payload):
    # ---- 整网导入 ----
    if path == "/api/network/import":
        nodes = build_nodes(payload.get("nodes", []))
        pipes = build_pipes(payload.get("pipes", []))
        issues = validate_import(nodes, pipes)
        if issues:
            return err(
                "IMPORT_INVALID",
                "管网数据校验失败：存在悬空端点、重号、自环或无阀管段被标记关闭",
                issues=issues,
            )
        STATE["version"] = 1
        STATE["updated_at"] = now_iso()
        STATE["nodes"] = {n.id: n for n in nodes}
        STATE["pipes"] = {p.id: p for p in pipes}
        STATE["events"] = []
        STATE["next_event_id"] = 1
        STATE["revision_no"] = 0
        return 200, {"version": 1, "node_count": len(nodes), "pipe_count": len(pipes)}

    if path == "/api/network":
        return 200, snapshot()

    # ---- 局部修订 ----
    if path == "/api/network/revise":
        if STATE["version"] != payload["version"]:
            return err(
                "VERSION_CONFLICT",
                f"版本过期：当前版本为 {STATE['version']}，操作基于版本 {payload['version']}",
                status=409,
                current_version=STATE["version"],
                expected_version=payload["version"],
            )
        try:
            rev = build_revision(payload)
        except ValueError:
            return 422, {"detail": "add_pipes 不接受 valve_closed：新管段阀门默认开启"}
        result = evaluate_revision(STATE["nodes"], STATE["pipes"], rev, pending_events())
        if not result.ok:
            return err(
                "REVISION_INVALID",
                "管网局部修订被拒绝（违约明细见 issues）",
                issues=result.issues,
            )
        changes = revision_changes(rev)
        STATE["nodes"] = result.nodes
        STATE["pipes"] = result.pipes
        STATE["version"] += 1
        STATE["updated_at"] = now_iso()
        STATE["revision_no"] += 1
        return 200, {
            "version": STATE["version"],
            "revision_no": STATE["revision_no"],
            "change_ids": changes,
            "change_count": len(changes),
            "node_count": len(result.nodes),
            "pipe_count": len(result.pipes),
        }

    # ---- 隔离预演 / 提交 ----
    if path == "/api/isolation/preview":
        result = evaluate_plan(
            STATE["nodes"],
            STATE["pipes"],
            payload["fault_pipe_id"],
            list(payload.get("close_pipe_ids", [])),
            for_submit=False,
            pending_events=pending_events(),
        )
        return 200, {
            "version": STATE["version"],
            "version_match": STATE["version"] == payload["version"],
            "fault_pipe_id": payload["fault_pipe_id"],
            "close_pipe_ids": payload.get("close_pipe_ids", []),
            "isolatable": result.isolatable,
            "affected_sink_ids": result.affected_sink_ids,
            "issues": [issue_dict(i) for i in result.issues],
        }

    if path == "/api/isolation/submit":
        if STATE["version"] != payload["version"]:
            return err(
                "VERSION_CONFLICT",
                f"版本过期：当前版本为 {STATE['version']}，操作基于版本 {payload['version']}",
                status=409,
                current_version=STATE["version"],
                expected_version=payload["version"],
            )
        close_ids = list(dict.fromkeys(payload.get("close_pipe_ids", [])))
        result = evaluate_plan(
            STATE["nodes"],
            STATE["pipes"],
            payload["fault_pipe_id"],
            list(payload.get("close_pipe_ids", [])),
            for_submit=True,
            pending_events=pending_events(),
        )
        blocking = [i for i in result.issues if i.code in BLOCKING_ISSUE_CODES]
        if blocking:
            return err("PLAN_INVALID", "隔离方案非法", issues=blocking)
        if not result.isolatable:
            return err(
                "NOT_ISOLATABLE",
                "该方案无法隔离故障管段",
                issues=result.issues,
            )
        for pid in close_ids:
            p = STATE["pipes"][pid]
            STATE["pipes"][pid] = Pipe(p.id, p.from_node, p.to_node, p.has_valve, True, p.name)
        before = STATE["version"]
        STATE["version"] = before + 1
        eid = STATE["next_event_id"]
        STATE["next_event_id"] += 1
        event = {
            "id": eid,
            "version_before": before,
            "version_after": STATE["version"],
            "fault_pipe_id": payload["fault_pipe_id"],
            "closed_pipe_ids": close_ids,
            "affected_sink_ids": result.affected_sink_ids,
            "status": EVENT_PENDING,
            "restored_at": None,
            "restored_version": None,
            "reopened_pipe_ids": [],
            "created_at": now_iso(),
        }
        STATE["events"].append(event)
        return 200, {
            "version": STATE["version"],
            "fault_pipe_id": payload["fault_pipe_id"],
            "closed_pipe_ids": close_ids,
            "affected_sink_ids": result.affected_sink_ids,
            "event_id": eid,
        }

    if path == "/api/isolation/events":
        rows = []
        for e in sorted(STATE["events"], key=lambda r: r["id"], reverse=True):
            rows.append(
                {
                    "id": e["id"],
                    "version_before": e["version_before"],
                    "version_after": e["version_after"],
                    "fault_pipe_id": e["fault_pipe_id"],
                    "closed_pipe_ids": e["closed_pipe_ids"],
                    "affected_sink_ids": e["affected_sink_ids"],
                    "status": e["status"],
                    "restored_at": e["restored_at"],
                    "restored_version": e["restored_version"],
                    "reopened_pipe_ids": e["reopened_pipe_ids"],
                    "created_at": e["created_at"],
                }
            )
        return 200, rows

    m = re.match(r"^/api/isolation/events/(\d+)/restore/(preview|confirm)$", path)
    if m:
        event_id, action = int(m.group(1)), m.group(2)
        row = next((e for e in STATE["events"] if e["id"] == event_id), None)
        if row is None:
            return err("EVENT_NOT_FOUND", f"隔离事件不存在：{event_id}", status=404, event_id=event_id)
        if row["status"] == "restored":
            return err(
                "EVENT_ALREADY_RESTORED",
                f"隔离事件 {event_id} 已复供，不能重复操作",
                status=409,
                event_id=event_id,
            )
        event = next(e for e in domain_events() if e.id == event_id)
        result = evaluate_restoration(
            STATE["nodes"], STATE["pipes"], event, domain_events()
        )
        if action == "preview":
            return 200, {
                "version": STATE["version"],
                "version_match": STATE["version"] == payload["version"],
                "event_id": event_id,
                "status": "pending",
                "fault_pipe_id": event.fault_pipe_id,
                "closed_pipe_ids": list(event.closed_pipe_ids),
                "safe": result.safe,
                "reopened_pipe_ids": result.reopened_pipe_ids,
                "recoverable_sink_ids": result.recoverable_sink_ids,
                "still_blocked_sink_ids": result.still_blocked_sink_ids,
                "remaining_closed_pipe_ids": result.remaining_closed_pipe_ids,
                "remaining_fault_pipe_ids": result.remaining_fault_pipe_ids,
                "issues": [issue_dict(i) for i in result.issues],
            }
        # confirm
        if STATE["version"] != payload["version"]:
            return err(
                "VERSION_CONFLICT",
                f"版本过期：当前版本为 {STATE['version']}，操作基于版本 {payload['version']}",
                status=409,
                current_version=STATE["version"],
                expected_version=payload["version"],
            )
        if not result.safe:
            risky = [i for i in result.issues if i.code == "RESTORE_REWATER_RISK"]
            return err("RESTORE_UNSAFE", "复供被拒绝", issues=risky or result.issues)
        occupied = set()
        for e in pending_events():
            if e.id != event_id:
                occupied.update(e.closed_pipe_ids)
        actual = [pid for pid in result.reopened_pipe_ids if pid not in occupied]
        for pid in actual:
            p = STATE["pipes"][pid]
            STATE["pipes"][pid] = Pipe(p.id, p.from_node, p.to_node, p.has_valve, False, p.name)
        STATE["version"] += 1
        row["status"] = "restored"
        row["restored_at"] = now_iso()
        row["restored_version"] = STATE["version"]
        row["reopened_pipe_ids"] = actual
        return 200, {
            "version": STATE["version"],
            "event_id": event_id,
            "status": "restored",
            "fault_pipe_id": event.fault_pipe_id,
            "reopened_pipe_ids": actual,
            "recoverable_sink_ids": result.recoverable_sink_ids,
            "still_blocked_sink_ids": result.still_blocked_sink_ids,
        }

    return 404, {"detail": "Not Found"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静模式
        pass

    def _send(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        if self.path.startswith("/api/"):
            status, body = handle(self.path, {})
        else:
            status, body = 404, {"detail": "Not Found"}
        self._send(status, body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, {"detail": "invalid json"})
            return
        try:
            status, body = handle(self.path, payload)
        except KeyError as exc:
            status, body = 422, {"detail": f"missing object: {exc}"}
        self._send(status, body)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"stdlib memory backend listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
