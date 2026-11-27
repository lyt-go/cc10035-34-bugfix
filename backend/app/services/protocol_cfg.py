"""协议配置领域服务：IoT协议定义与生命周期管理。

提供协议版本管理、校验、设备协议统计等真实业务能力。
工作台的版本切换流程由 ``protocol_lifecycle`` 承载（含凭据、幂等与血缘），
本服务保留通用资源动作与向后兼容的升版入口，并修复同后缀编号重复问题。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.protocol_lifecycle import (
    build_config_id,
    lifecycle_service,
    split_lineage,
)
from app.store import store

CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


class ProtocolCfgService(BaseModuleService):
    MODULE = "protocol_cfg"
    ENTITY = "协议配置"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["配置编号", "协议名称", "传输方式", "数据格式", "报文分隔符", "心跳间隔", "超时秒数", "配置版本"]
    REQUIRED_FIELDS = ["配置编号", "协议名称", "传输方式"]
    STATUS_ORDER = ["草稿", "待验证", "已生效", "已废弃"]
    ACTION_RULES = {"提交验证": "待验证", "标记生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """通用动作流转；标记生效时维护协议族内唯一生效版本与历史归属。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if target == "已生效":
            root_id, _ = split_lineage(str(entry.get("配置编号", "")))
            # 同一协议族只能有一个生效版本：旧生效版本转入废弃并记录替代关系。
            for row in lifecycle_service.family_rows(root_id):
                if row.get("status") == "已生效" and row.get("id") != entry.get("id"):
                    row["status"] = "已废弃"
                    row["pending"] = False
                    row["废弃时间"] = _now()
                    row["替代编号"] = entry.get("配置编号")
            entry["生效时间"] = _now()
        elif target == "已废弃":
            entry["废弃时间"] = _now()

        entry["status"] = target
        entry["pending"] = target != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS
        return entry, f"{self.ENTITY}已{action}"

    def version_upgrade(self, config_id: str, new_version: str) -> dict[str, Any]:
        """协议版本升级（向后兼容入口）：按血缘生成下一版本编号，不再重复 -v2。

        工作台完整版本切换流程（凭据校验、待验证不中断服务、幂等确认）请使用
        ``ProtocolLifecycleService.apply_upgrade / confirm_upgrade``。
        """
        rows = store.rows(self.MODULE)
        target = lifecycle_service.find_protocol(config_id)
        if target is None:
            return {"ok": False, "message": f"协议配置 {config_id} 不存在"}

        root_id, _ = split_lineage(config_id)
        new_seq = lifecycle_service.next_version_seq(root_id)
        new_config_id = build_config_id(root_id, new_seq)
        if lifecycle_service.find_protocol(new_config_id) is not None:
            return {"ok": False, "message": f"配置编号 {new_config_id} 已存在，版本身份冲突"}

        # 标记旧版本为废弃（保留原编号与血缘，不物理删除）
        target["status"] = "已废弃"
        target["pending"] = False
        target["废弃时间"] = _now()
        target["替代编号"] = new_config_id

        new_entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "配置编号": new_config_id,
            "协议名称": target.get("协议名称"),
            "传输方式": target.get("传输方式"),
            "数据格式": target.get("数据格式"),
            "报文分隔符": target.get("报文分隔符"),
            "心跳间隔": target.get("心跳间隔"),
            "超时秒数": target.get("超时秒数"),
            "配置版本": new_version,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "协议族": root_id,
            "版本序号": new_seq,
            "来源编号": config_id,
            "创建时间": _now(),
        }
        rows.append(new_entry)

        return {
            "ok": True,
            "message": f"协议 {config_id} 已废弃，新版本 {new_config_id}（{new_version}）已创建",
            "old_version": config_id,
            "new_version_id": new_config_id,
            "new_entry": new_entry,
        }

    def validate_protocol(self, config_id: str) -> dict[str, Any]:
        """协议校验：检查心跳间隔和超时参数是否合理。

        校验规则：
        - 心跳间隔 > 0
        - 超时秒数 > 心跳间隔（确保超时检测合理）
        - 报文分隔符非空
        """
        target = lifecycle_service.find_protocol(config_id)
        if target is None:
            return {"ok": False, "message": f"协议配置 {config_id} 不存在"}

        errors = []
        try:
            heartbeat = float(target.get("心跳间隔", "0"))
        except (ValueError, TypeError):
            heartbeat = 0

        try:
            timeout = float(target.get("超时秒数", "0"))
        except (ValueError, TypeError):
            timeout = 0

        if heartbeat <= 0:
            errors.append("心跳间隔必须大于0")
        if timeout <= heartbeat:
            errors.append(f"超时秒数({timeout})应大于心跳间隔({heartbeat})")
        if not str(target.get("报文分隔符", "")).strip():
            errors.append("报文分隔符不能为空")

        if errors:
            return {"ok": False, "message": "; ".join(errors), "errors": errors}

        return {
            "ok": True,
            "message": f"协议 {config_id} 校验通过",
            "heartbeat_interval": heartbeat,
            "timeout": timeout,
        }

    def device_protocol_distribution(self) -> list[dict[str, Any]]:
        """统计每种协议类型被多少设备使用。"""
        device_rows = store.rows("device_registry")
        dist = {}
        for dev in device_rows:
            proto = dev.get("协议类型", "未知")
            dist[proto] = dist.get(proto, 0) + 1

        return sorted(
            [{"协议类型": k, "设备数": v} for k, v in dist.items()],
            key=lambda x: x["设备数"], reverse=True,
        )
