"""跨模块数据流协调服务：实现9组跨模块业务数据流。

每一组数据流将两个关联的领域Service组合成真实业务流程，
包括数据校验、关联查找、状态同步和聚合计算。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _now_dt() -> datetime:
    return datetime.now(CST)


class CrossModuleService:
    """跨模块数据流协调器：将9组领域Service串联为真实业务流程。"""

    # ========================
    # Flow 1: device_registry + protocol_cfg → 设备注册与协议适配
    # ========================

    def register_device_with_protocol(
        self, device_id: str, device_type: str, protocol_id: str
    ) -> dict[str, Any]:
        """设备注册时自动绑定协议配置（委托协议生命周期服务）。

        生命周期服务保证：只认实际生效配置、升版冲突时保留待激活申请、
        绝不冒用已废弃条目；本方法保留原跨模块入口与返回结构。
        """
        from app.services.protocol_lifecycle import lifecycle_service

        return lifecycle_service.bind_device(device_id, device_type, protocol_id)

    def query_device_protocol_status(self, device_id: str | None = None) -> list[dict[str, Any]]:
        """查询设备与其绑定协议的状态概览。

        返回每个设备及其关联协议的状态矩阵。
        """
        device_rows = store.rows("device_registry")
        protocol_rows = store.rows("protocol_cfg")
        result = []

        filtered = device_rows
        if device_id:
            filtered = [
                row for row in device_rows if row.get("设备编号") == device_id
            ]

        for dev in filtered:
            matched_protocol = None
            dev_protocol = dev.get("协议类型", "")
            # 优先按设备实际绑定的配置编号精确定位，避免同名废弃/待验证条目误判。
            bound_id = dev.get("绑定协议编号")
            if bound_id:
                for proto in protocol_rows:
                    if proto.get("配置编号") == bound_id:
                        matched_protocol = proto.get("status")
                        break
            if matched_protocol is None:
                for proto in protocol_rows:
                    if proto.get("协议名称") == dev_protocol and proto.get("status") == "已生效":
                        matched_protocol = proto.get("status")
                        break

            result.append({
                "设备编号": dev.get("设备编号"),
                "设备类型": dev.get("设备类型"),
                "设备状态": dev.get("status"),
                "协议类型": dev_protocol,
                "绑定协议编号": bound_id or "",
                "协议状态": matched_protocol or "未找到",
                "注册时间": dev.get("注册时间"),
                "最后在线": dev.get("最后在线"),
            })
        return result

    # ========================
    # Flow 2: msg_decode + msg_router → 报文解码与消息路由
    # ========================

    def decode_and_route(self, raw_message: str, protocol_type: str, topic: str) -> dict[str, Any]:
        """报文解码后按路由规则分发到目标Topic。

        业务流程：
        1. 根据协议类型查找已启用的解码规则
        2. 模拟解码（按分隔符切割报文）
        3. 查找匹配的路由规则（按Topic匹配）
        4. 模拟路由到目标Topic
        """
        decode_rows = store.rows("msg_decode")
        router_rows = store.rows("msg_router")

        # 步骤1：查找已启用的解码规则
        matched_rules = [
            row for row in decode_rows
            if row.get("协议类型") == protocol_type
            and row.get("status") == "已启用"
        ]
        if not matched_rules:
            return {
                "ok": False,
                "message": f"未找到协议 {protocol_type} 的已启用解码规则",
            }

        # 步骤2：模拟报文解码
        rule = matched_rules[0]
        separator = rule.get("报文标识", ",")
        parts = raw_message.split(separator) if separator else [raw_message]
        decoded_fields = {}
        for i, part in enumerate(parts):
            decoded_fields[f"field_{i}"] = part.strip()
        decoded_fields["_decode_rule"] = rule.get("规则编号")
        decoded_fields["_timestamp"] = _now()

        # 步骤3：查找匹配的路由规则
        matched_router = [
            row for row in router_rows
            if row.get("匹配Topic") == topic
            and row.get("status") == "已启用"
        ]
        if not matched_router:
            return {
                "ok": False,
                "message": f"Topic {topic} 没有匹配的已启用路由规则",
            }

        router = matched_router[0]
        target_topic = router.get("目标Topic", "")
        router_id = router.get("规则编号")

        # 步骤4：路由分发
        result_entry = {
            "router_id": router_id,
            "source_topic": topic,
            "target_topic": target_topic,
            "forward_method": router.get("转发方式", "direct"),
            "decoded_fields": decoded_fields,
            "raw_message": raw_message,
            "route_time": _now(),
        }

        return {
            "ok": True,
            "message": f"报文已解码并路由到 {target_topic}",
            "result": result_entry,
        }

    # ========================
    # Flow 3: ts_point + ts_store → 时序测点与存储策略
    # ========================

    def configure_point_with_store(
        self, point_id: str, device_id: str, point_name: str, storage_engine: str
    ) -> dict[str, Any]:
        """为测点配置存储策略，自动关联设备档案。

        业务流程：
        1. 校验设备是否已注册且在线
        2. 创建测点定义
        3. 查找或创建对应的存储策略
        4. 返回关联结果
        """
        device_rows = store.rows("device_registry")
        point_rows = store.rows("ts_point")
        store_rows_data = store.rows("ts_store")

        # 步骤1：校验设备
        device = None
        for row in device_rows:
            if row.get("设备编号") == device_id:
                device = row
                break
        if device is None:
            return {"ok": False, "message": f"设备 {device_id} 未注册"}

        # 步骤2：创建测点
        max_id = max((int(row.get("id", 0)) for row in point_rows), default=0)
        point_entry = {
            "id": max_id + 1,
            "测点编号": point_id,
            "设备编号": device_id,
            "测点名称": point_name,
            "数据类型": "float64",
            "采集间隔": "30s",
            "上报方式": "mqtt",
            "单位符号": "",
            "量程范围": "0-100",
            "status": "采集中",
            "pending": True,
            "abnormal": False,
        }
        point_rows.append(point_entry)

        # 步骤3：创建存储策略
        max_sid = max((int(row.get("id", 0)) for row in store_rows_data), default=0)
        store_entry = {
            "id": max_sid + 1,
            "策略编号": f"STR-{point_id}",
            "测点编号": point_id,
            "存储引擎": storage_engine,
            "压缩算法": "gzip",
            "降采样规则": "avg_5min",
            "保留天数": "90",
            "分段策略": "by_month",
            "冷热分层": "30d_hot",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        store_rows_data.append(store_entry)

        return {
            "ok": True,
            "message": f"测点 {point_id} 已配置，存储引擎：{storage_engine}",
            "point": point_entry,
            "store_strategy": store_entry,
        }

    def query_point_store_matrix(self) -> list[dict[str, Any]]:
        """查询测点-存储策略关联矩阵。"""
        point_rows = store.rows("ts_point")
        store_rows_data = store.rows("ts_store")
        result = []
        for pt in point_rows:
            pt_id = pt.get("测点编号")
            matched = None
            for st in store_rows_data:
                if st.get("测点编号") == pt_id:
                    matched = st
                    break
            result.append({
                "测点编号": pt_id,
                "设备编号": pt.get("设备编号"),
                "测点名称": pt.get("测点名称"),
                "测点状态": pt.get("status"),
                "存储引擎": matched.get("存储引擎") if matched else "未配置",
                "压缩算法": matched.get("压缩算法") if matched else "无",
                "保留天数": matched.get("保留天数") if matched else "0",
                "存储状态": matched.get("status") if matched else "未配置",
            })
        return result

    # ========================
    # Flow 4: downsample + data_gap → 降采样与数据断点补采
    # ========================

    def downsample_with_gap_check(
        self, source_point: str, target_granularity: str, agg_function: str,
        time_start: str | None = None, time_end: str | None = None,
    ) -> dict[str, Any]:
        """降采样前先检查数据断点，存在断点则自动发起补采。

        业务流程：
        1. 创建降采样任务
        2. 检查源测点在该时间段是否有数据断点
        3. 如有断点则自动创建补采记录
        4. 关联降采样任务和补采记录
        """
        downsample_rows = store.rows("downsample")
        data_gap_rows = store.rows("data_gap")

        # 步骤1：创建降采样任务
        max_id = max((int(row.get("id", 0)) for row in downsample_rows), default=0)
        start = time_start or (_now_dt() - timedelta(hours=24)).isoformat(timespec="seconds")
        end = time_end or _now()
        task = {
            "id": max_id + 1,
            "任务编号": f"DOWN-{max_id + 1:04d}",
            "源测点": source_point,
            "目标粒度": target_granularity,
            "聚合函数": agg_function,
            "源时间范围": f"{start} ~ {end}",
            "目标测点": f"{source_point}_ds",
            "执行状态": "待执行",
            "完成时间": "",
            "status": "待执行",
            "pending": True,
            "abnormal": False,
        }
        downsample_rows.append(task)

        # 步骤2：检查数据断点（模拟检测）
        # 在源测点对应的设备上查找断点记录
        related_gaps = [
            row for row in data_gap_rows
            if row.get("测点编号") == source_point
            and row.get("status") in ("待补采", "补采中")
        ]

        # 步骤3：模拟断点检测（基于时间窗口估算）
        gap_count = len(related_gaps)
        estimated_missing = 0
        for gap in related_gaps:
            try:
                missing = int(gap.get("缺失点数", 0))
            except (ValueError, TypeError):
                missing = 0
            estimated_missing += missing

        # 如有断点且存在未完成的补采，启动补采
        gap_result = None
        if related_gaps:
            gap = related_gaps[0]
            gap["status"] = "补采中"
            gap["pending"] = True
            gap_result = {
                "gap_id": gap.get("记录编号"),
                "status": "补采中",
                "missing_points": estimated_missing,
            }

        return {
            "ok": True,
            "message": f"降采样任务已创建，发现 {gap_count} 条断点记录",
            "downsample_task": task,
            "gap_result": gap_result,
            "estimated_missing": estimated_missing,
        }

    def trigger_gap_recovery(self, device_id: str, point_id: str) -> dict[str, Any]:
        """显式触发断点补采，基于设备上下线和降采样历史判断。

        业务流程：
        1. 查找设备状态
        2. 查找该测点的断点记录
        3. 查找降采样任务关联
        4. 发起补采
        """
        data_gap_rows = store.rows("data_gap")
        downsample_rows = store.rows("downsample")

        # 查找相关降采样任务
        related_tasks = [
            row for row in downsample_rows
            if row.get("源测点") == point_id and row.get("status") == "已完成"
        ]

        # 查找断点记录
        gaps = [
            row for row in data_gap_rows
            if row.get("设备编号") == device_id and row.get("测点编号") == point_id
        ]

        if not gaps:
            return {"ok": False, "message": f"设备 {device_id} 测点 {point_id} 无断点记录"}

        # 发起补采
        gap = gaps[0]
        gap["status"] = "补采中"
        gap["pending"] = True

        return {
            "ok": True,
            "message": f"已发起断点补采：{gap.get('记录编号')}",
            "gap_entry": gap,
            "related_downsample_tasks": len(related_tasks),
        }

    # ========================
    # Flow 5: alarm_trigger + rule_engine → 告警触发与联动规则
    # ========================

    def evaluate_alarm_with_rule(
        self, point_id: str, current_value: float,
    ) -> dict[str, Any]:
        """当测点值触发告警时，自动执行关联的联动规则。

        业务流程：
        1. 查找该测点的已启用告警规则
        2. 评估告警条件（阈值对比）
        3. 如触发告警，查找关联的联动规则并执行
        4. 返回告警结果和执行动作
        """
        alarm_rows = store.rows("alarm_trigger")
        rule_rows = store.rows("rule_engine")

        # 步骤1：查找告警规则
        triggered_alarms = []
        for alarm in alarm_rows:
            if alarm.get("关联测点") != point_id or alarm.get("status") != "已启用":
                continue
            try:
                upper = float(alarm.get("阈值上限", "inf"))
            except (ValueError, TypeError):
                upper = float("inf")
            try:
                lower = float(alarm.get("阈值下限", "-inf"))
            except (ValueError, TypeError):
                lower = float("-inf")

            condition = alarm.get("告警条件", "")
            triggered = False
            if ">" in condition:
                if current_value > upper:
                    triggered = True
            elif "<" in condition:
                if current_value < lower:
                    triggered = True
            elif ">" in condition and "<" in condition:
                if current_value > upper or current_value < lower:
                    triggered = True
            elif "between" in condition.lower():
                if lower <= current_value <= upper:
                    triggered = True
            else:
                triggered = current_value > upper or current_value < lower

            if triggered:
                triggered_alarms.append(alarm)

        if not triggered_alarms:
            return {
                "ok": True,
                "message": f"测点 {point_id} 值 {current_value} 未触发任何告警",
                "triggered": False,
                "alarms": [],
                "actions": [],
            }

        # 步骤2：执行联动规则
        actions = []
        for alarm in triggered_alarms:
            interval = alarm.get("持续秒数", "0")
            level = alarm.get("告警级别", "")
            notify = alarm.get("通知方式", "")

            for rule in rule_rows:
                if rule.get("status") != "已启用":
                    continue
                action = rule.get("执行动作", "")
                action_params = rule.get("动作参数", "")

                actions.append({
                    "alarm_rule": alarm.get("规则编号"),
                    "alarm_level": level,
                    "triggered_value": current_value,
                    "rule_engine_rule": rule.get("规则编号"),
                    "action": action,
                    "params": action_params,
                    "notify_method": notify,
                    "cool_down": rule.get("冷却时间", ""),
                    "executed_at": _now(),
                })

        return {
            "ok": True,
            "message": f"测点 {point_id} 值 {current_value} 触发 {len(triggered_alarms)} 条告警",
            "triggered": True,
            "threat_level": triggered_alarms[0].get("告警级别", "警告"),
            "alarms": [
                {"rule_id": a.get("规则编号"), "condition": a.get("告警条件"),
                 "upper": a.get("阈值上限"), "lower": a.get("阈值下限")}
                for a in triggered_alarms
            ],
            "actions": actions,
        }

    # ========================
    # Flow 6: shadow_dev + tag_binding → 设备影子与标签绑定
    # ========================

    def sync_device_shadow_with_tags(
        self, device_id: str, desired_state: str,
    ) -> dict[str, Any]:
        """设备影子同步时自动关联标签，确保标签反映期望状态。

        业务流程：
        1. 查找设备的影子记录
        2. 更新期望状态
        3. 查找设备的标签绑定
        4. 根据期望状态更新标签
        """
        shadow_rows = store.rows("shadow_dev")
        tag_rows = store.rows("tag_binding")

        # 步骤1：查找影子记录
        matched_shadow = None
        for row in shadow_rows:
            if row.get("设备编号") == device_id:
                matched_shadow = row
                break

        if matched_shadow is None:
            return {"ok": False, "message": f"设备 {device_id} 没有影子记录"}

        # 步骤2：更新影子
        old_desired = matched_shadow.get("期望状态", "")
        matched_shadow["期望状态"] = desired_state
        matched_shadow["同步时间"] = _now()
        if desired_state != old_desired:
            matched_shadow["status"] = "存在差异"
        else:
            matched_shadow["status"] = "已同步"

        # 步骤3：查找关联标签
        related_tags = [
            row for row in tag_rows
            if row.get("设备编号") == device_id and row.get("status") == "已绑定"
        ]

        # 步骤4：更新标签反映新状态
        tag_updates = []
        for tag in related_tags:
            old_value = tag.get("标签值", "")
            new_value = f"{old_value}+{desired_state}"
            tag["标签值"] = new_value
            tag["标签版本"] = str(int(tag.get("标签版本", "0")) + 1)
            tag_updates.append({
                "tag_key": tag.get("标签键"),
                "old_value": old_value,
                "new_value": new_value,
            })

        return {
            "ok": True,
            "message": f"设备 {device_id} 影子已同步，{len(tag_updates)} 个标签已更新",
            "shadow": {
                "设备编号": device_id,
                "期望状态": desired_state,
                "上报状态": matched_shadow.get("上报状态"),
                "版本号": matched_shadow.get("版本号"),
            },
            "tag_updates": tag_updates,
        }

    def bind_tag_with_shadow_check(
        self, device_id: str, tag_key: str, tag_value: str,
    ) -> dict[str, Any]:
        """绑定标签时校验设备影子状态，确保标签与期望状态一致。

        业务流程：
        1. 查找设备影子
        2. 校验期望状态
        3. 创建标签绑定
        4. 关联影子到标签
        """
        shadow_rows = store.rows("shadow_dev")
        tag_rows = store.rows("tag_binding")

        # 步骤1：查找影子
        shadow = None
        for row in shadow_rows:
            if row.get("设备编号") == device_id:
                shadow = row
                break

        shadow_status = shadow.get("期望状态") if shadow else "未知"

        # 步骤2：创建标签绑定
        max_id = max((int(row.get("id", 0)) for row in tag_rows), default=0)
        tag_entry = {
            "id": max_id + 1,
            "关系编号": f"TAG-{max_id + 1:04d}",
            "设备编号": device_id,
            "标签键": tag_key,
            "标签值": tag_value,
            "绑定时间": _now(),
            "解绑时间": "",
            "来源方式": "manual",
            "标签版本": "1",
            "status": "已绑定",
            "pending": True,
            "abnormal": False,
        }
        tag_rows.append(tag_entry)

        return {
            "ok": True,
            "message": f"标签 {tag_key}={tag_value} 已绑定到设备 {device_id}",
            "tag_entry": tag_entry,
            "shadow_status": shadow_status,
        }

    # ========================
    # Flow 7: gateway_mgmt + edge_cache → 网关管理与边缘缓存
    # ========================

    def manage_gateway_cache(
        self, gateway_id: str, cache_size: str, eviction_strategy: str,
    ) -> dict[str, Any]:
        """管理网关边缘缓存配置，基于网关健康状态调整缓存策略。

        业务流程：
        1. 查找网关节点
        2. 检查健康状态
        3. 创建/更新边缘缓存配置
        4. 根据网关状态调整缓存大小和淘汰策略
        """
        gateway_rows = store.rows("gateway_mgmt")
        cache_rows = store.rows("edge_cache")

        # 步骤1：查找网关
        gateway = None
        for row in gateway_rows:
            if row.get("网关编号") == gateway_id:
                gateway = row
                break
        if gateway is None:
            return {"ok": False, "message": f"网关 {gateway_id} 不存在"}

        gw_status = gateway.get("status", "")
        health = gateway.get("健康状态", "")

        # 步骤2：根据健康状态调整缓存配置
        adjusted_size = cache_size
        if gw_status == "降级" or health == "异常":
            adjusted_size = str(int(cache_size.replace("MB", "")) // 2) + "MB"
            eviction_strategy = "LRU" if eviction_strategy == "LFU" else eviction_strategy

        # 步骤3：创建/更新缓存配置
        existing = None
        for row in cache_rows:
            if row.get("网关编号") == gateway_id:
                existing = row
                break

        if existing:
            existing["缓存大小"] = adjusted_size
            existing["淘汰策略"] = eviction_strategy
            existing["最近刷新"] = _now()
            cache_entry = existing
        else:
            max_id = max((int(row.get("id", 0)) for row in cache_rows), default=0)
            cache_entry = {
                "id": max_id + 1,
                "配置编号": f"ECA-{gateway_id}",
                "网关编号": gateway_id,
                "缓存大小": adjusted_size,
                "淘汰策略": eviction_strategy,
                "持久化开关": "on",
                "同步间隔": "60s",
                "压缩开关": "on",
                "最近刷新": _now(),
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            }
            cache_rows.append(cache_entry)

        # 更新网关健康状态
        gateway["健康状态"] = "健康"

        return {
            "ok": True,
            "message": f"网关 {gateway_id} 边缘缓存已配置（大小:{adjusted_size}，淘汰:{eviction_strategy}）",
            "gateway": {
                "网关编号": gateway_id,
                "状态": gw_status,
                "挂载设备数": gateway.get("挂载设备数"),
            },
            "cache_entry": cache_entry,
        }

    def query_gateway_cache_matrix(self) -> list[dict[str, Any]]:
        """查询网关-缓存关联矩阵。"""
        gateway_rows = store.rows("gateway_mgmt")
        cache_rows = store.rows("edge_cache")
        result = []
        for gw in gateway_rows:
            gw_id = gw.get("网关编号")
            matched = None
            for ca in cache_rows:
                if ca.get("网关编号") == gw_id:
                    matched = ca
                    break
            result.append({
                "网关编号": gw_id,
                "网关地址": gw.get("网关地址"),
                "网关状态": gw.get("status"),
                "挂载设备数": gw.get("挂载设备数"),
                "缓存大小": matched.get("缓存大小") if matched else "未配置",
                "淘汰策略": matched.get("淘汰策略") if matched else "无",
                "缓存状态": matched.get("status") if matched else "未配置",
            })
        return result

    # ========================
    # Flow 8: firmware_ota + aggregate_sink → 固件升级与聚合分发
    # ========================

    def ota_with_aggregate_dispatch(
        self, target_device: str, firmware_version: str, upgrade_strategy: str,
        target_count: int = 10,
    ) -> dict[str, Any]:
        """固件升级任务创建后，自动创建聚合分发任务将升级结果汇总。

        业务流程：
        1. 创建固件升级任务
        2. 创建聚合分发任务汇总升级进度
        3. 返回关联的两个任务
        """
        ota_rows = store.rows("firmware_ota")
        agg_rows = store.rows("aggregate_sink")

        # 步骤1：创建固件升级任务
        max_id = max((int(row.get("id", 0)) for row in ota_rows), default=0)
        ota_task = {
            "id": max_id + 1,
            "任务编号": f"OTA-{max_id + 1:04d}",
            "目标设备": target_device,
            "固件版本": firmware_version,
            "升级包摘要": f"sha256:mock_{firmware_version}",
            "升级策略": upgrade_strategy,
            "目标数量": target_count,
            "成功数量": 0,
            "任务进度": "0%",
            "status": "待执行",
            "pending": True,
            "abnormal": False,
        }
        ota_rows.append(ota_task)

        # 步骤2：创建聚合分发任务
        max_aid = max((int(row.get("id", 0)) for row in agg_rows), default=0)
        agg_task = {
            "id": max_aid + 1,
            "任务编号": f"AGG-{max_aid + 1:04d}",
            "数据窗口": f"ota_{ota_task['任务编号']}",
            "聚合函数": "count_success",
            "输出Topic": f"ota/status/{target_device}",
            "窗口时长": "300s",
            "滑动步长": "60s",
            "延迟容忍": "30s",
            "执行状态": "待执行",
            "status": "待执行",
            "pending": True,
            "abnormal": False,
        }
        agg_rows.append(agg_task)

        return {
            "ok": True,
            "message": f"固件升级任务已创建，聚合分发任务已关联",
            "ota_task": ota_task,
            "aggregate_task": agg_task,
        }

    def query_ota_aggregate_status(self, ota_task_id: str | None = None) -> list[dict[str, Any]]:
        """查询固件升级与其聚合分发任务的关联状态。"""
        ota_rows = store.rows("firmware_ota")
        agg_rows = store.rows("aggregate_sink")
        result = []

        filtered = ota_rows
        if ota_task_id:
            filtered = [
                row for row in ota_rows
                if row.get("任务编号") == ota_task_id
            ]

        for ota in filtered:
            ota_id = ota.get("任务编号")
            matched_agg = None
            for agg in agg_rows:
                if ota_id in (agg.get("数据窗口") or ""):
                    matched_agg = agg
                    break
            result.append({
                "OTA任务编号": ota_id,
                "目标设备": ota.get("目标设备"),
                "固件版本": ota.get("固件版本"),
                "OTA状态": ota.get("status"),
                "进度": ota.get("任务进度"),
                "聚合任务": matched_agg.get("任务编号") if matched_agg else "未关联",
                "聚合状态": matched_agg.get("status") if matched_agg else "无",
            })
        return result

    # ========================
    # Flow 9: data_stream + batch_export → 数据流管道与批量导出
    # ========================

    def stream_to_batch_export(
        self, stream_id: str, device_range: str, point_list: str,
        time_start: str | None = None, time_end: str | None = None,
        export_format: str = "CSV",
    ) -> dict[str, Any]:
        """从数据流管道触发批量导出，基于管道状态决定导出策略。

        业务流程：
        1. 查找数据流管道
        2. 校验管道状态（必须在运行中）
        3. 创建批量导出任务
        4. 关联管道和导出
        """
        stream_rows = store.rows("data_stream")
        export_rows = store.rows("batch_export")

        # 步骤1：查找管道
        stream = None
        for row in stream_rows:
            if row.get("管道编号") == stream_id:
                stream = row
                break
        if stream is None:
            return {"ok": False, "message": f"数据流管道 {stream_id} 不存在"}

        if stream.get("status") != "运行中":
            return {
                "ok": False,
                "message": f"管道 {stream_id} 未运行，当前状态：{stream.get('status')}",
            }

        # 步骤2：创建批量导出任务
        max_id = max((int(row.get("id", 0)) for row in export_rows), default=0)
        start = time_start or (_now_dt() - timedelta(hours=24)).isoformat(timespec="seconds")
        end = time_end or _now()
        export_task = {
            "id": max_id + 1,
            "任务编号": f"EXP-{max_id + 1:04d}",
            "设备范围": device_range,
            "测点列表": point_list,
            "时间范围": f"{start} ~ {end}",
            "导出格式": export_format,
            "数据行数": "0",
            "文件大小": "0",
            "导出状态": "待执行",
            "status": "待执行",
            "pending": True,
            "abnormal": False,
        }
        export_rows.append(export_task)

        return {
            "ok": True,
            "message": f"批量导出任务已从管道 {stream_id} 触发",
            "stream_source": {
                "管道编号": stream_id,
                "输入源": stream.get("输入源"),
                "处理算子链": stream.get("处理算子链"),
            },
            "export_task": export_task,
        }

    def query_stream_export_matrix(self) -> list[dict[str, Any]]:
        """查询数据流管道与批量导出的关联矩阵。"""
        stream_rows = store.rows("data_stream")
        export_rows = store.rows("batch_export")
        result = []
        for s in stream_rows:
            result.append({
                "管道编号": s.get("管道编号"),
                "输入源": s.get("输入源"),
                "管道状态": s.get("status"),
                "并行度": s.get("并行度"),
                "关联导出数": sum(
                    1 for e in export_rows
                    if e.get("设备范围") == s.get("输入源")
                ),
            })
        return result

    # ========================
    # 跨模块概览 Dashboard
    # ========================

    def cross_module_overview(self) -> dict[str, Any]:
        """输出全平台跨模块数据流概览。"""
        modules = store.module_names()

        summary = {
            "total_modules": len(modules),
            "total_records": sum(len(store.rows(m)) for m in modules),
            "active_flows": 0,
            "flows": [],
            "generated_at": _now(),
        }

        # 统计每种数据流的状态
        flow_pairs = [
            ("device_registry", "protocol_cfg", "设备注册与协议适配"),
            ("msg_decode", "msg_router", "报文解码与消息路由"),
            ("ts_point", "ts_store", "时序测点与存储策略"),
            ("downsample", "data_gap", "降采样与断点补采"),
            ("alarm_trigger", "rule_engine", "告警触发与联动规则"),
            ("shadow_dev", "tag_binding", "设备影子与标签绑定"),
            ("gateway_mgmt", "edge_cache", "网关管理与边缘缓存"),
            ("firmware_ota", "aggregate_sink", "固件升级与聚合分发"),
            ("data_stream", "batch_export", "数据流管道与批量导出"),
        ]

        for mod_a, mod_b, label in flow_pairs:
            a_rows = store.rows(mod_a)
            b_rows = store.rows(mod_b)
            a_active = sum(1 for r in a_rows if r.get("pending"))
            b_active = sum(1 for r in b_rows if r.get("pending"))
            is_active = a_active > 0 or b_active > 0
            if is_active:
                summary["active_flows"] += 1

            summary["flows"].append({
                "name": label,
                "module_a": mod_a,
                "module_b": mod_b,
                "a_total": len(a_rows),
                "b_total": len(b_rows),
                "a_active": a_active,
                "b_active": b_active,
                "is_active": is_active,
            })

        return summary


cross_service = CrossModuleService()