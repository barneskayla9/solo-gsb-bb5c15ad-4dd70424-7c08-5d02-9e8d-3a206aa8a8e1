"""HTTP 请求 / 响应模型。

节点类型：
- source  水源
- sink    用水点
- junction 连接点

编号（id）为字符串，便于保留 "N01" 这类业务编号；排序时按字符串字典序。
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

NodeType = Literal["source", "sink", "junction"]


class NodeIn(BaseModel):
    id: str = Field(min_length=1, description="节点编号，唯一")
    type: NodeType
    name: str | None = Field(default=None, description="可选名称")


class PipeIn(BaseModel):
    id: str = Field(min_length=1, description="管段编号，唯一")
    # JSON 字段为 from/to（from 是 Python 关键字），Python 属性名为 from_node/to_node
    from_node: str = Field(min_length=1, validation_alias="from")
    to_node: str = Field(min_length=1, validation_alias="to")
    has_valve: bool = Field(default=False, description="该管段是否设有阀门")
    # 阀门初始状态：默认开启（未关闭）。非阀门管段传 true 会在导入时被拒绝。
    valve_closed: bool = False
    name: str | None = None

    model_config = {"populate_by_name": True, "extra": "forbid"}


class ImportIn(BaseModel):
    nodes: list[NodeIn]
    pipes: list[PipeIn]


class PlanIn(BaseModel):
    """故障隔离预演 / 提交入参。"""

    fault_pipe_id: str = Field(min_length=1, description="故障管段编号")
    close_pipe_ids: list[str] = Field(
        default_factory=list, description="拟关闭的阀门管段编号列表"
    )
    version: int = Field(ge=0, description="读取管网时的版本号，空网为 0")


class RestoreIn(BaseModel):
    """复供预演 / 确认入参：按隔离事件编号 + 当前管网版本操作。"""

    version: int = Field(ge=0, description="读取管网时的版本号")


# ------------------------------------------------------- 局部修订（增改删）


class AddNodeIn(NodeIn):
    """新增节点：与整网导入节点同构。"""


class AddPipeIn(PipeIn):
    """新增管段：阀门默认开启。

    继承整网管段结构，但显式移除 valve_closed 入参 —— 新管段阀门只能默认开启，
    关阀必须走隔离提交流程（传入 valve_closed 会得到请求体校验错误 422）。
    """

    valve_closed: None = Field(
        default=None,
        exclude=True,
        description="禁止字段：新管段阀门默认开启，关阀请走隔离提交",
    )


class UpdateNodeIn(BaseModel):
    id: str = Field(min_length=1, description="待更新节点编号")
    # 缺省 = 保持原值；不使用 Optional 默认，避免“未给出”与“显式置空名称”混淆
    type: NodeType | None = Field(default=None, description="节点类型；缺省保持原值")
    name: str | None = Field(default=None, description="名称；显式 null 清空，缺省保持原值")

    model_config = {"extra": "forbid"}

    def fields_provided(self) -> set[str]:
        return set(self.model_fields_set)


class UpdatePipeIn(BaseModel):
    id: str = Field(min_length=1, description="待更新管段编号")
    from_: str | None = Field(default=None, validation_alias="from")
    to_: str | None = Field(default=None, validation_alias="to")
    has_valve: bool | None = Field(default=None, description="是否有阀；缺省保持原值")
    valve_closed: bool | None = Field(
        default=None,
        description="禁止字段：阀位只能由隔离提交/复供确认改变，给出即判违约",
    )
    name: str | None = Field(default=None, description="名称；显式 null 清空，缺省保持原值")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    def fields_provided(self) -> set[str]:
        return set(self.model_fields_set)


class RevisionIn(BaseModel):
    """局部修订入参：基准版本 + 增/改/删变更列表（每项须带对象编号）。"""

    version: int = Field(ge=0, description="基准管网版本号（读取管网时获得）")
    add_nodes: list[AddNodeIn] = Field(default_factory=list)
    add_pipes: list[AddPipeIn] = Field(default_factory=list)
    update_nodes: list[UpdateNodeIn] = Field(default_factory=list)
    update_pipes: list[UpdatePipeIn] = Field(default_factory=list)
    delete_node_ids: list[str] = Field(default_factory=list)
    delete_pipe_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 响应模型


class NodeOut(BaseModel):
    id: str
    type: NodeType
    name: str | None = None


class PipeOut(BaseModel):
    id: str
    # 出参 JSON 键名为 from/to；Python 属性使用 from_/to_
    from_: str = Field(serialization_alias="from")
    to_: str = Field(serialization_alias="to")
    has_valve: bool
    valve_closed: bool | None = None
    name: str | None = None


class ImportOut(BaseModel):
    version: int
    node_count: int
    pipe_count: int


class ChangeOut(BaseModel):
    op: Literal[
        "add_node",
        "add_pipe",
        "update_node",
        "update_pipe",
        "delete_node",
        "delete_pipe",
    ]
    node_id: str | None = None
    pipe_id: str | None = None


class RevisionOut(BaseModel):
    version: int = Field(description="修订后的新版本号")
    revision_no: int = Field(description="修订流水号（按当前管网累计，整网导入后归零）")
    change_ids: list[ChangeOut] = Field(description="本次变更及对象编号列表")
    change_count: int
    node_count: int
    pipe_count: int


class NetworkOut(BaseModel):
    version: int
    updated_at: datetime | None = None
    nodes: list[NodeOut]
    pipes: list[PipeOut]


class IssueOut(BaseModel):
    code: str
    message: str
    pipe_id: str | None = None
    # 复供阻断原因可能引用具体待修复事件与相关管段（可选，保持向后兼容）
    event_id: int | None = None
    related_pipe_id: str | None = None
    # 节点类问题（修订）可能引用节点编号（可选，保持向后兼容）
    node_id: str | None = None


class PreviewOut(BaseModel):
    version: int
    version_match: bool
    fault_pipe_id: str
    close_pipe_ids: list[str]
    isolatable: bool
    affected_sink_ids: list[str]
    issues: list[IssueOut]


class SubmitOut(BaseModel):
    version: int
    fault_pipe_id: str
    closed_pipe_ids: list[str]
    affected_sink_ids: list[str]
    event_id: int


class RestorePreviewOut(BaseModel):
    version: int
    version_match: bool
    event_id: int
    status: Literal["pending", "restored"]
    fault_pipe_id: str
    closed_pipe_ids: list[str]
    safe: bool = Field(description="重开后其他待修复故障管段两端是否仍均不可从水源到达")
    reopened_pipe_ids: list[str] = Field(description="确认复供时将重开的阀门")
    recoverable_sink_ids: list[str] = Field(description="预计恢复供水的用水点")
    still_blocked_sink_ids: list[str] = Field(description="本次仍无法恢复的原失供用水点")
    remaining_closed_pipe_ids: list[str] = Field(description="重开后仍关闭的阀门（其他待修复事件需要）")
    remaining_fault_pipe_ids: list[str] = Field(description="仍需保持断水的其他待修复故障管段")
    issues: list[IssueOut]


class RestoreConfirmOut(BaseModel):
    version: int
    event_id: int
    status: Literal["pending", "restored"]
    fault_pipe_id: str
    reopened_pipe_ids: list[str]
    recoverable_sink_ids: list[str]
    still_blocked_sink_ids: list[str]


class EventOut(BaseModel):
    id: int
    version_before: int
    version_after: int
    fault_pipe_id: str
    closed_pipe_ids: list[str]
    affected_sink_ids: list[str]
    status: Literal["pending", "restored"] = "pending"
    restored_at: datetime | None = None
    restored_version: int | None = None
    reopened_pipe_ids: list[str] = []
    created_at: datetime
