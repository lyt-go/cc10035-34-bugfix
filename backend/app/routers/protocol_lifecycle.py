"""协议升版与设备绑定生命周期接口。

承载工作台「待升版协议版本切换」与注册页「首次绑定」的全部专用端点：
维护员凭据、幂等升版申请/确认、协议族生效项解析、绑定确认与使用上报。
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.protocol_lifecycle import lifecycle_service, split_lineage

router = APIRouter(prefix="/api/protocol-lifecycle", tags=["协议升版与设备绑定"])


class OperatorPayload(BaseModel):
    operator: str = Field(..., description="维护员账号")
    credential: str = Field(..., description="版本切换凭据")
    scope_root: str = Field(default="", description="授权协议族根编号，空表示不限制")


class UpgradeApplyPayload(BaseModel):
    config_id: str = Field(..., description="发起升版的生效配置编号")
    new_version: str = Field(..., description="新版本号")
    operator: str
    credential: str
    ticket: str = Field(default="", description="客户端幂等票据，空则服务端生成")


class UpgradeConfirmPayload(BaseModel):
    ticket: str
    operator: str
    credential: str


class BindPayload(BaseModel):
    device_id: str
    device_type: str
    protocol_id: str = Field(..., description="注册页选择的协议编号（可为待验证升级编号）")


class DevicePayload(BaseModel):
    device_id: str


@router.post("/operators/credential")
def configure_operator(payload: OperatorPayload):
    """给维护员配置协议版本切换凭据。"""
    return lifecycle_service.configure_operator(
        payload.operator, payload.credential, payload.scope_root,
    )


@router.get("/families/{config_id}/active")
def resolve_active(config_id: str):
    """注册页按协议族（或任一版本编号）准确定位当前生效项。"""
    root_id, _ = split_lineage(config_id)
    active = lifecycle_service.active_protocol(root_id)
    family = lifecycle_service.family_rows(root_id)
    return {
        "ok": True,
        "协议族": root_id,
        "active": active,
        "family_versions": [
            {
                "配置编号": row.get("配置编号"),
                "配置版本": row.get("配置版本"),
                "status": row.get("status"),
                "来源编号": row.get("来源编号", ""),
                "替代编号": row.get("替代编号", ""),
            }
            for row in family
        ],
    }


@router.post("/upgrades/apply")
def apply_upgrade(payload: UpgradeApplyPayload):
    """工作台发起版本切换申请（幂等：同票据取回同一笔申请）。"""
    return lifecycle_service.apply_upgrade(
        payload.config_id,
        payload.new_version,
        payload.operator,
        payload.credential,
        payload.ticket,
    )


@router.post("/upgrades/confirm")
def confirm_upgrade(payload: UpgradeConfirmPayload):
    """确认升版办结（幂等：响应丢失后重复确认取回同一笔办理结果）。"""
    return lifecycle_service.confirm_upgrade(
        payload.ticket, payload.operator, payload.credential,
    )


@router.get("/upgrades/history")
def upgrade_history(config_id: str = ""):
    """查询升版申请与确认记录（旧版本编号、本次升版确认分别可查）。"""
    return lifecycle_service.upgrade_history(config_id)


@router.post("/bindings")
def bind_device(payload: BindPayload):
    """设备注册首次绑定：生效项直接绑定，升版冲突时挂起待激活申请。"""
    return lifecycle_service.bind_device(
        payload.device_id, payload.device_type, payload.protocol_id,
    )


@router.post("/bindings/confirm")
def confirm_binding(payload: DevicePayload):
    """确认设备绑定：仅当实际使用的配置已生效才允许确认。"""
    return lifecycle_service.confirm_device_binding(payload.device_id)


@router.get("/bindings/{device_id}")
def binding_detail(device_id: str):
    """注册详情：原有效版本 / 用户选择的升级 / 尚未成功的绑定分别可区分。"""
    return lifecycle_service.device_binding_detail(device_id)


@router.post("/bindings/report")
def report_binding(payload: DevicePayload):
    """办结后上报设备实际有效的配置。"""
    return lifecycle_service.report_device_protocol(payload.device_id)
