#!/usr/bin/env python3
"""管网局部修订调用示例（仅依赖标准库）。

用法：
    python3 examples/revise_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

脚本完整演示（基于隔离流程的示例管网 sample_network.json）：
  1. 导入管网；
  2. 提交隔离事件 #1（关 P4 隔离故障管段 P6，U3 失供），版本变为 2；
  3. 局部修订（不清空隔离事件）：
     - 新增连接点 E、用水点 U9 与两根新管段 P9/P10（新阀默认开启）；
     - 更新节点名称；
     - 原子生效，版本 +1，返回全部变更及对象编号；
  4. 修订违约（均不写入，版本不变）：
     - 删除待修复事件引用的故障管段 P6 / 关阀管段 P4 / 失供用水点 U3；
     - 修改 P6 端点、P4 阀门属性、U3 类型；
     - 新增绕过隔离阀的旁通管 B->U3，使 P6 重新进水；
     - 新管段阀门置为关闭；用修订改阀位；版本过期；
  5. 隔离事件仍在，可正常复供。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_network.json")


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def expect_rejected(title: str, status: int, body: dict, *codes: str) -> None:
    got = {i["code"] for i in body.get("error", {}).get("issues", [])}
    assert status == 422 and body["error"]["code"] == "REVISION_INVALID", body
    for code in codes:
        assert code in got, f"{title}: 期望违约 {code}，实际 {got}\n{body}"
    print(f"--- {title}: 已拒绝（{sorted(got)}），未写入 ---")


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网（版本 1）
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网", status, body)
    assert status == 200, body

    # 2. 提交隔离：关 P4 隔离故障管段 P6（U3 失供），版本变为 2
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("2. 提交隔离事件 #1（关 P4 隔离 P6）", status, body)
    assert status == 200 and body["version"] == 2

    # 3. 合法局部修订：在与隔离区无关的支路末端接出新区
    #    S/A 侧仍有压；A->E->U9 的新管不影响 P6 两端的断水状态
    revision = {
        "version": 2,
        "add_nodes": [
            {"id": "E", "type": "junction", "name": "现场补测连接点"},
            {"id": "U9", "type": "sink", "name": "新建小区"},
        ],
        "add_pipes": [
            # 新管段阀门默认开启；不传 valve_closed
            {"id": "P9", "from": "A", "to": "E", "has_valve": True, "name": "现场补测管"},
            {"id": "P10", "from": "E", "to": "U9", "has_valve": False},
        ],
        "update_nodes": [
            {"id": "U1", "name": "幸福小区（测绘更名）"},
        ],
    }
    status, body = call("POST", "/api/network/revise", revision)
    show("3. 合法局部修订（增 2 节点 / 2 管段、改名 1 节点）", status, body)
    assert status == 200, body
    assert body["version"] == 3
    assert body["change_count"] == 5
    assert body["change_ids"][0] == {"op": "add_node", "node_id": "E", "pipe_id": None}
    # 事件未被清空
    status, events = call("GET", "/api/isolation/events")
    assert any(e["id"] == 1 and e["status"] == "pending" for e in events), events

    # 4a1. 删除待修复事件引用的故障管段 P6、关阀管段 P4、失供用水点 U3
    bad = {
        "version": 3,
        "delete_pipe_ids": ["P6", "P4"],
        "delete_node_ids": ["U3"],
    }
    status, body = call("POST", "/api/network/revise", bad)
    show("4a1. 删除受保护对象（预期 422，列出涉及对象与事件）", status, body)
    expect_rejected(
        "4a1",
        status,
        body,
        "PROTECTED_PIPE_DELETE",
        "PROTECTED_SINK_DELETE",
    )

    # 4a2. 改动受保护属性：故障/关阀管段端点、阀门属性；失供用水点类型
    bad = {
        "version": 3,
        "update_pipes": [
            {"id": "P6", "from": "B"},          # 故障管段端点不得改动
        ],
    }
    status, body = call("POST", "/api/network/revise", bad)
    show("4a2a. 改动故障管段端点（预期 422）", status, body)
    expect_rejected("4a2a", status, body, "PROTECTED_PIPE_ENDPOINT_CHANGE")

    bad = {
        "version": 3,
        "update_pipes": [{"id": "P4", "has_valve": False}],  # 关阀管段阀门属性不得改动
    }
    status, body = call("POST", "/api/network/revise", bad)
    show("4a2b. 改动关阀管段阀门属性（预期 422）", status, body)
    expect_rejected("4a2b", status, body, "PROTECTED_PIPE_VALVE_CHANGE")

    bad = {
        "version": 3,
        "update_nodes": [
            {"id": "U3", "type": "junction"},   # 失供用水点类型不得改动
        ],
    }
    status, body = call("POST", "/api/network/revise", bad)
    show("4a2c. 改动失供用水点类型（预期 422）", status, body)
    expect_rejected("4a2c", status, body, "PROTECTED_SINK_TYPE_CHANGE")

    # 4b. 新增旁通管绕过关闭的 P4，使故障管段 P6 的 U3 端重新进水
    bad = {
        "version": 3,
        "add_pipes": [{"id": "P99", "from": "B", "to": "U3", "has_valve": True}],
    }
    status, body = call("POST", "/api/network/revise", bad)
    show("4b. 新增旁通管导致重新进水（预期 422 PENDING_FAULT_REWATER）", status, body)
    expect_rejected("4b", status, body, "PENDING_FAULT_REWATER")

    # 4c. 新管段不接受 valve_closed 入参（新阀默认开启，关阀须走隔离提交）
    bad = {
        "version": 3,
        "add_nodes": [{"id": "Z", "type": "junction"}],
        "add_pipes": [
            {"id": "P77", "from": "A", "to": "Z", "has_valve": True,
             "valve_closed": True}
        ],
    }
    status, body = call("POST", "/api/network/revise", bad)
    show("4c. 新管段携带 valve_closed（预期 422 请求校验，字段被禁止）", status, body)
    assert status == 422, body
    print("--- 4c: 已拒绝，新管段只能默认开启，未写入 ---")

    # 4d. 用修订改变现有阀位（阀位只能由隔离/复供流程改变）
    bad = {"version": 3, "update_pipes": [{"id": "P1", "valve_closed": True}]}
    status, body = call("POST", "/api/network/revise", bad)
    show("4d. 修订改现有阀位（预期 422 VALVE_STATE_IMMUTABLE）", status, body)
    expect_rejected("4d", status, body, "VALVE_STATE_IMMUTABLE")

    # 4e. 候选管网结构非法（自环 + 悬空端点 + 重号）
    bad = {
        "version": 3,
        "add_pipes": [
            {"id": "P78", "from": "A", "to": "A"},     # 自环
            {"id": "P79", "from": "A", "to": "GHOST"},  # 悬空端点
        ],
    }
    status, body = call("POST", "/api/network/revise", bad)
    show("4e. 候选管网结构非法（预期 422）", status, body)
    expect_rejected("4e", status, body, "PIPE_SELF_LOOP", "PIPE_ENDPOINT_UNKNOWN")

    # 4f. 版本过期
    bad = {"version": 2, "update_nodes": [{"id": "B", "name": "二号点"}]}
    status, body = call("POST", "/api/network/revise", bad)
    show("4f. 版本过期（预期 409 VERSION_CONFLICT）", status, body)
    assert status == 409 and body["error"]["code"] == "VERSION_CONFLICT", body

    # 所有违约均未写入：版本仍为 3，待修复事件仍在
    status, net = call("GET", "/api/network")
    assert net["version"] == 3, net
    assert "P99" not in {p["id"] for p in net["pipes"]}

    # 5. 待修复事件照常复供（修订未触碰阀位：P4 仍关闭）
    status, body = call(
        "POST", "/api/isolation/events/1/restore/confirm", {"version": 3}
    )
    show("5. 修订后确认复供事件 #1（预期成功，P4 重开，版本 4）", status, body)
    assert status == 200 and body["version"] == 4, body
    assert body["reopened_pipe_ids"] == ["P4"]

    print("\n全部修订示例执行完成。")


if __name__ == "__main__":
    main()
