#!/usr/bin/env python3
"""局部管网修订调用示例（仅依赖标准库）。

用法：
    python3 examples/revision_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

现场测绘发现管网资料有误时，操作员可基于基准版本对节点/管段做局部
增改删，而不清空隔离事件。脚本演示：

  导入 -> 提交隔离（关 P4 隔离 P6，U3 失供）->
  1. 尝试删除故障管段 P6       -> 422 PROTECTED_PIPE_DELETE_BY_EVENT
  2. 尝试改 P4 阀门属性        -> 422 PROTECTED_PIPE_VALVE_CHANGE
  3. 尝试改失供点 U3 类型      -> 422 PROTECTED_SINK_TYPE_CHANGE
  4. 新增绕过 P4 的进水支路    -> 422 REVISION_FAULT_REACHABLE（通水复核）
  5. 新增管段显式 valve_closed -> 422 REVISION_VALVE_CLOSED_FORBIDDEN
  6. 用过期版本修订            -> 409 VERSION_CONFLICT
  7. 合法修订：水源侧扩展 U9   -> 200，版本 +1，隔离事件保留
  8. 复供仍可正常进行
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


def expect(status, wanted, label):
    assert status == wanted, f"{label}：期望 HTTP {wanted}，实际 {status}"


def revision(changes: dict, version: int) -> tuple[int, dict]:
    return call("POST", "/api/network/revision", {"version": version, **changes})


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    status, body = call("POST", "/api/network/import", network)
    show("导入管网", status, body)
    expect(status, 200, "导入")

    # 提交隔离：关 P4 隔离 P6（U3 失供），版本 1 -> 2
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("提交隔离（关 P4，隔离 P6）", status, body)
    expect(status, 200, "隔离提交")

    # 1. 删除待修复事件的故障管段 -> 拒绝且不写入
    status, body = revision({"deleted_pipe_ids": ["P6"]}, 2)
    show("1. 删除故障管段 P6（预期 422 保护）", status, body)
    expect(status, 422, "删除故障管段")

    # 2. 修改关阀管段 P4 的阀门属性 -> 拒绝
    status, body = revision(
        {"updated_pipes": [{"id": "P4", "from": "B", "to": "D", "has_valve": False}]},
        2,
    )
    show("2. 去掉关阀管段 P4 的阀门（预期 422 保护）", status, body)
    expect(status, 422, "改阀门属性")

    # 3. 修改失供用水点 U3 的类型 -> 拒绝
    status, body = revision(
        {"updated_nodes": [{"id": "U3", "type": "junction"}]}, 2
    )
    show("3. 失供用水点 U3 改类型（预期 422 保护）", status, body)
    expect(status, 422, "改用水点类型")

    # 4. 新增绕过 P4 的进水支路 A-D：故障管段 P6 的 D 端重新可达 -> 通水复核拒绝
    status, body = revision(
        {"added_pipes": [{"id": "P9", "from": "A", "to": "D", "has_valve": False}]},
        2,
    )
    show("4. 新增绕过隔离阀的支路 A-D（预期 422 通水复核）", status, body)
    expect(status, 422, "破坏隔离的修订")
    assert body["error"]["code"] == "REVISION_FAULT_REACHABLE", body

    # 5. 新管段阀门显式要求关闭 -> 拒绝（新管段阀门默认开启）
    status, body = revision(
        {"added_pipes": [
            {"id": "P9", "from": "A", "to": "B", "has_valve": True, "valve_closed": True}
        ]},
        2,
    )
    show("5. 新管段显式 valve_closed=true（预期 422）", status, body)
    expect(status, 422, "新管段关闭")

    # 6. 过期版本 -> 409
    status, body = revision(
        {"updated_nodes": [{"id": "U1", "type": "sink", "name": "改名"}]}, 1
    )
    show("6. 基于过期版本修订（预期 409）", status, body)
    expect(status, 409, "版本过期")

    # 7. 合法修订：在水源侧 A 点扩展新用水点 U9，不触碰隔离区 -> 成功
    changes = {
        "added_nodes": [{"id": "U9", "type": "sink", "name": "测绘新增用水点"}],
        "added_pipes": [{"id": "P9", "from": "A", "to": "U9", "has_valve": True}],
        "updated_nodes": [{"id": "U1", "type": "sink", "name": "一区总表"}],
    }
    status, body = revision(changes, 2)
    show("7. 合法局部修订（水源侧扩展，预期 200，版本 3）", status, body)
    expect(status, 200, "合法修订")
    assert body["version"] == 3, body
    assert body["added_node_ids"] == ["U9"]
    assert body["added_pipe_ids"] == ["P9"]
    assert body["updated_node_ids"] == ["U1"]

    # 新管段阀门默认开启；隔离事件保留、P4 仍关
    status, snap = call("GET", "/api/network")
    p9 = next(p for p in snap["pipes"] if p["id"] == "P9")
    p4 = next(p for p in snap["pipes"] if p["id"] == "P4")
    assert p9["valve_closed"] is False and p4["valve_closed"] is True
    print("\n校验：新管段 P9 阀门默认开启；P4 仍由隔离事件关闭。")

    status, events = call("GET", "/api/isolation/events")
    assert events[0]["status"] == "pending", events
    print("校验：隔离事件未被清空，仍为 pending。")

    # 8. 修订后复供流程照常工作（版本 3 -> 4）
    status, body = call(
        "POST", "/api/isolation/events/1/restore/confirm", {"version": 3}
    )
    show("8. 修订后确认复供（预期 200，版本 4）", status, body)
    expect(status, 200, "复供")
    assert body["version"] == 4, body

    print("\n全部修订示例执行完成。")


if __name__ == "__main__":
    main()
