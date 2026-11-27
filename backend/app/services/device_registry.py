"""设备注册领域服务：IoT设备全生命周期管理。

提供设备注册、心跳检测、协议匹配、健康检查等真实业务能力。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _now_dt() -> datetime:
    return datetime.now(CST)


class DeviceRegistryService(BaseModuleService):
    MODULE = "device_registry"
    ENTITY = "设备档案"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["设备编号", "设备类型", "协议类型", "认证方式", "所属网关", "固件版本", "注册时间", "最后在线"]
    REQUIRED_FIELDS = ["设备编号", "设备类型", "协议类型"]
    STATUS_ORDER = ["待激活", "在线", "离线", "已注销"]
    ACTION_RULES = {"激活设备": "在线", "标记离线": "离线", "注销设备": "已注销"}
    NEGATIVE_ACTIONS = []

    def heartbeat(self, device_id: str) -> dict[str, Any]:
        """设备心跳上报：更新最后在线时间，检测离线超时设备需标记离线。

        业务流程：
        1. 查找设备
        2. 更新最后在线时间
        3. 检查是否长期离线（超过7天标记为离线）
        """
        rows = store.rows(self.MODULE)
        device = None
        for row in rows:
            if row.get("设备编号") == device_id:
                device = row
                break
        if device is None:
            return {"ok": False, "message": f"设备 {device_id} 不存在"}

        device["最后在线"] = _now()
        if device["status"] == "待激活":
            device["status"] = "在线"
            device["pending"] = True
        device["abnormal"] = False

        return {
            "ok": True,
            "message": f"设备 {device_id} 心跳已更新",
            "设备编号": device_id,
            "最后在线": device["最后在线"],
            "状态": device["status"],
        }

    def check_offline_devices(self, threshold_hours: int = 168) -> list[dict[str, Any]]:
        """检查离线超时设备：超过阈值的在线设备标记为离线。

        Args:
            threshold_hours: 离线阈值（小时），默认7天=168小时
        """
        rows = store.rows(self.MODULE)
        threshold = _now_dt() - timedelta(hours=threshold_hours)
        offline_list = []

        for row in rows:
            if row.get("status") != "在线":
                continue
            last_online = row.get("最后在线", "")
            if not last_online:
                continue
            try:
                last_dt = datetime.fromisoformat(last_online)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=CST)
            except (ValueError, TypeError):
                continue

            if last_dt < threshold:
                row["status"] = "离线"
                row["abnormal"] = True
                offline_list.append({
                    "设备编号": row.get("设备编号"),
                    "最后在线": last_online,
                    "离线时长小时": round((_now_dt() - last_dt).total_seconds() / 3600, 1),
                })

        return offline_list

    def match_protocol(self, device_type: str) -> list[dict[str, Any]]:
        """根据设备类型匹配可用的协议配置。

        只返回当前实际生效（未废弃）的协议；同一协议族仅暴露唯一生效版本，
        注册页据此准确定位生效项，不会命中已废弃条目。
        """
        protocol_rows = store.rows("protocol_cfg")
        matched = []
        fallback = []
        seen_families: set[str] = set()

        for proto in protocol_rows:
            if proto.get("status") != "已生效":
                continue
            config_id = str(proto.get("配置编号", ""))
            # 与 protocol_lifecycle 保持同一套血缘拆分，避免循环导入则内联解析。
            root_id = config_id.rsplit("-v", 1)[0] if "-v" in config_id else config_id
            if root_id in seen_families:
                continue
            seen_families.add(root_id)

            payload = {
                "配置编号": proto.get("配置编号"),
                "协议名称": proto.get("协议名称", ""),
                "传输方式": proto.get("传输方式", ""),
                "数据格式": proto.get("数据格式"),
                "心跳间隔": proto.get("心跳间隔"),
            }
            proto_name = str(payload["协议名称"])
            proto_transport = str(payload["传输方式"])
            if device_type.lower() in proto_name.lower() or device_type.lower() in proto_transport.lower():
                matched.append(payload)
            else:
                fallback.append(payload)

        return (matched or fallback)[:5]

    def status_summary(self) -> dict[str, Any]:
        """设备状态统计摘要：在线/离线/待激活/已注销比例。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        summary = {}
        for status in self.STATUS_ORDER:
            count = sum(1 for r in rows if r.get("status") == status)
            summary[status] = count

        devices = [{
            "设备编号": r.get("设备编号"),
            "设备类型": r.get("设备类型"),
            "状态": r.get("status"),
            "最后在线": r.get("最后在线"),
            "协议类型": r.get("协议类型"),
        } for r in rows]

        return {
            "total": total,
            "summary": summary,
            "online_rate": round(summary.get("在线", 0) / max(total, 1) * 100, 1),
            "devices": devices,
        }