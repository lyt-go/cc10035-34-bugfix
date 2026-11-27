"""协议升版与设备绑定的生命周期协调服务。

承载工作台「待升版协议版本切换」的完整领域流程：

- 维护员切换凭据的配置与校验（无凭据不允许发起升版）；
- 协议升版申请（幂等票据）、升版确认（同一笔票据重复确认取回同一结果）；
- 协议族血缘：同后缀编号不再重复生成，废弃条目保留原编号与历史归属；
- 设备首次绑定：只认实际生效的配置，升版冲突时保留待激活申请，不冒用废弃项；
- 注册详情区分原有效版本、用户选择的升级与尚未成功的绑定；
- 办结后的使用上报，上报解析到的必须是当前实际有效的配置。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

CST = timezone(timedelta(hours=8))

PROTOCOL_MODULE = "protocol_cfg"
DEVICE_MODULE = "device_registry"

# 协议版本血缘后缀，例如 P-11-v2；根配置不带后缀（视作 v1）。
_VERSION_SUFFIX = re.compile(r"^(?P<root>.+)-v(?P<seq>\d+)$")


def _now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def split_lineage(config_id: str) -> tuple[str, int]:
    """拆分配置编号的协议族根编号与版本序号。

    ``P-11`` -> ``("P-11", 1)``；``P-11-v2`` -> ``("P-11", 2)``。
    """
    match = _VERSION_SUFFIX.match(config_id.strip())
    if not match:
        return config_id.strip(), 1
    return match.group("root"), int(match.group("seq"))


def build_config_id(root_id: str, seq: int) -> str:
    """按版本序号拼配置编号：根版本保持原编号，其后依次为 -v2、-v3…"""
    return root_id if seq <= 1 else f"{root_id}-v{seq}"


class ProtocolLifecycleService:
    """协议升版 / 设备绑定生命周期协调器（内存仓储，全部状态可查询）。"""

    # ---------------- 基础查询 ----------------

    def _protocol_rows(self) -> list[dict[str, Any]]:
        return store.rows(PROTOCOL_MODULE)

    def _device_rows(self) -> list[dict[str, Any]]:
        return store.rows(DEVICE_MODULE)

    def _table(self, name: str) -> list[dict[str, Any]]:
        return store.rows(name)

    def _next_id(self, table_name: str) -> int:
        return max((int(row.get("id", 0)) for row in self._table(table_name)), default=0) + 1

    def find_protocol(self, config_id: str) -> dict[str, Any] | None:
        """按配置编号精确定位单条协议（废弃项也能查到原编号）。"""
        for row in self._protocol_rows():
            if row.get("配置编号") == config_id:
                return row
        return None

    def family_rows(self, root_id: str) -> list[dict[str, Any]]:
        """返回同一协议族（同一根编号）下的全部版本，按版本序号排序。"""
        members = [
            row for row in self._protocol_rows()
            if split_lineage(str(row.get("配置编号", "")))[0] == root_id
        ]
        return sorted(members, key=lambda row: split_lineage(str(row.get("配置编号", "")))[1])

    def active_protocol(self, root_id: str) -> dict[str, Any] | None:
        """定位协议族当前实际生效（且未废弃）的版本，注册页据此准确定位。"""
        for row in self.family_rows(root_id):
            if row.get("status") == "已生效":
                return row
        return None

    def next_version_seq(self, root_id: str) -> int:
        """计算协议族下一个可用版本序号，避免同后缀编号重复。"""
        seqs = [
            split_lineage(str(row.get("配置编号", "")))[1]
            for row in self.family_rows(root_id)
        ]
        return (max(seqs, default=1) + 1)

    # ---------------- 维护员切换凭据 ----------------

    def configure_operator(
        self, operator: str, credential: str, scope_root: str = "",
    ) -> dict[str, Any]:
        """给维护员配置协议版本切换凭据。"""
        operator = (operator or "").strip()
        credential = (credential or "").strip()
        if not operator or not credential:
            return {"ok": False, "message": "维护员与切换凭据均不能为空"}

        table = self._table("maintainer_credential")
        for row in table:
            if row.get("维护员") == operator:
                row["切换凭据"] = credential
                row["授权协议族"] = scope_root
                row["更新时间"] = _now()
                return {
                    "ok": True,
                    "message": f"维护员 {operator} 的切换凭据已更新",
                    "credential": row,
                }

        entry = {
            "id": self._next_id("maintainer_credential"),
            "维护员": operator,
            "切换凭据": credential,
            "授权协议族": scope_root,
            "更新时间": _now(),
        }
        table.append(entry)
        return {
            "ok": True,
            "message": f"维护员 {operator} 已配置版本切换凭据",
            "credential": entry,
        }

    def _verify_operator(self, operator: str, credential: str, root_id: str) -> str:
        """校验维护员切换凭据，返回错误信息；空串表示通过。"""
        operator = (operator or "").strip()
        credential = (credential or "").strip()
        if not operator or not credential:
            return "发起版本切换必须提供维护员与切换凭据"
        for row in self._table("maintainer_credential"):
            if row.get("维护员") != operator:
                continue
            if row.get("切换凭据") != credential:
                return f"维护员 {operator} 的切换凭据不正确"
            scope = str(row.get("授权协议族") or "")
            if scope and scope != root_id:
                return f"维护员 {operator} 未获得协议族 {root_id} 的切换授权"
            return ""
        return f"维护员 {operator} 尚未配置版本切换凭据"

    # ---------------- 协议升版：申请 + 确认（幂等） ----------------

    def apply_upgrade(
        self,
        config_id: str,
        new_version: str,
        operator: str,
        credential: str,
        ticket: str = "",
    ) -> dict[str, Any]:
        """发起协议升版申请：创建待验证新版本，旧生效版本继续在服务。

        同一 ``ticket`` 重复发起时直接取回原申请，不会重复建单或重复编号。
        """
        config_id = (config_id or "").strip()
        ticket = (ticket or "").strip()
        tickets = self._table("upgrade_ticket")

        # 幂等：响应丢失后回到原页面再次提交，取回同一笔申请。
        if ticket:
            for existing in tickets:
                if existing.get("票据") == ticket:
                    return {
                        "ok": True,
                        "idempotent": True,
                        "message": "升版申请已存在，取回原办理单",
                        "ticket": existing,
                        "new_entry": self.find_protocol(str(existing.get("新配置编号"))),
                    }

        target = self.find_protocol(config_id)
        if target is None:
            return {"ok": False, "message": f"协议配置 {config_id} 不存在"}

        root_id, current_seq = split_lineage(config_id)
        error = self._verify_operator(operator, credential, root_id)
        if error:
            return {"ok": False, "message": error}

        # 申请阶段只允许基于「仍在服务」的生效版本发起；已废弃条目不能再升版。
        active = self.active_protocol(root_id)
        if active is None:
            return {"ok": False, "message": f"协议族 {root_id} 当前没有生效版本，无法发起升版"}
        if target.get("配置编号") != active.get("配置编号"):
            return {
                "ok": False,
                "message": (
                    f"仅可对当前生效版本 {active.get('配置编号')} 发起升版，"
                    f"{config_id} 状态为 {target.get('status')}"
                ),
            }

        # 同一协议族同时只允许一笔待确认的升版，防止并发窗口重复切换。
        for existing in tickets:
            if existing.get("协议族") == root_id and existing.get("状态") == "待确认":
                return {
                    "ok": False,
                    "message": (
                        f"协议族 {root_id} 已存在待确认升版 {existing.get('票据')}，"
                        "请先确认或废弃该申请"
                    ),
                }

        new_seq = self.next_version_seq(root_id)
        new_config_id = build_config_id(root_id, new_seq)
        # 编号唯一性兜底：即使血缘计算之外出现同编号也不允许重复。
        if self.find_protocol(new_config_id) is not None:
            return {"ok": False, "message": f"配置编号 {new_config_id} 已存在，版本身份冲突"}

        new_entry = {
            "id": self._next_id(PROTOCOL_MODULE),
            "配置编号": new_config_id,
            "协议名称": target.get("协议名称"),
            "传输方式": target.get("传输方式"),
            "数据格式": target.get("数据格式"),
            "报文分隔符": target.get("报文分隔符"),
            "心跳间隔": target.get("心跳间隔"),
            "超时秒数": target.get("超时秒数"),
            "配置版本": new_version,
            "status": "待验证",
            "pending": True,
            "abnormal": False,
            # 版本血缘与历史归属：废弃后仍可据此查到原编号与来源。
            "协议族": root_id,
            "版本序号": new_seq,
            "来源编号": config_id,
            "创建时间": _now(),
        }
        self._protocol_rows().append(new_entry)

        if not ticket:
            ticket = f"UPG-{root_id}-{new_seq}-{self._next_id('upgrade_ticket'):04d}"
        record = {
            "id": self._next_id("upgrade_ticket"),
            "票据": ticket,
            "协议族": root_id,
            "原配置编号": config_id,
            "原版本序号": current_seq,
            "新配置编号": new_config_id,
            "新版本序号": new_seq,
            "配置版本": new_version,
            "维护员": operator,
            "状态": "待确认",
            "申请时间": _now(),
        }
        tickets.append(record)
        return {
            "ok": True,
            "idempotent": False,
            "message": f"升版申请 {ticket} 已创建，新版本 {new_config_id} 待验证",
            "ticket": record,
            "new_entry": new_entry,
        }

    def confirm_upgrade(self, ticket: str, operator: str, credential: str) -> dict[str, Any]:
        """确认升版办结：新版本标记生效，旧版本废弃并保留历史归属。

        同一票据重复确认（如首次响应丢失后回原页面再点确认）幂等返回同一结果。
        """
        ticket = (ticket or "").strip()
        record = next(
            (row for row in self._table("upgrade_ticket") if row.get("票据") == ticket),
            None,
        )
        if record is None:
            return {"ok": False, "message": f"升版票据 {ticket} 不存在"}

        error = self._verify_operator(operator, credential, str(record.get("协议族")))
        if error:
            return {"ok": False, "message": error}

        confirms = self._table("upgrade_confirm")

        # 幂等取回：同一笔办理只产生一次版本切换与一条确认记录。
        if record.get("状态") == "已办结":
            prior = next(
                (row for row in confirms if row.get("票据") == ticket), None,
            )
            return {
                "ok": True,
                "idempotent": True,
                "message": f"升版 {ticket} 已办结，取回原确认结果",
                "ticket": record,
                "confirmation": prior,
                "active_entry": self.find_protocol(str(record.get("新配置编号"))),
            }

        new_entry = self.find_protocol(str(record.get("新配置编号")))
        old_entry = self.find_protocol(str(record.get("原配置编号")))
        if new_entry is None or old_entry is None:
            return {"ok": False, "message": "升版关联的协议条目缺失，无法确认"}

        # 版本切换：新版本生效，旧版本废弃（保留编号与血缘，不删除）。
        new_entry["status"] = "已生效"
        new_entry["pending"] = False
        new_entry["生效时间"] = _now()
        old_entry["status"] = "已废弃"
        old_entry["pending"] = False
        old_entry["废弃时间"] = _now()
        old_entry["替代编号"] = new_entry["配置编号"]

        confirmation = {
            "id": self._next_id("upgrade_confirm"),
            "票据": ticket,
            "协议族": record.get("协议族"),
            "原配置编号": old_entry["配置编号"],
            "新配置编号": new_entry["配置编号"],
            "维护员": operator,
            "确认时间": _now(),
            "办理结果": "已生效",
        }
        confirms.append(confirmation)
        record["状态"] = "已办结"
        record["确认时间"] = confirmation["确认时间"]

        # 升版办结后，挂起在该协议族、等待新版本的绑定申请自动对接到新生效版本。
        for binding in self._table("device_binding"):
            if (
                binding.get("协议族") == record.get("协议族")
                and binding.get("绑定状态") == "待激活"
                and binding.get("等待票据") == ticket
            ):
                binding["目标配置编号"] = new_entry["配置编号"]

        return {
            "ok": True,
            "idempotent": False,
            "message": (
                f"升版 {ticket} 已办结：{old_entry['配置编号']} 废弃，"
                f"{new_entry['配置编号']} 生效"
            ),
            "ticket": record,
            "confirmation": confirmation,
            "active_entry": new_entry,
        }

    def upgrade_history(self, config_id: str = "") -> dict[str, Any]:
        """查询升版申请与确认记录；可按协议族或任一版本编号过滤。"""
        tickets = self._table("upgrade_ticket")
        confirms = self._table("upgrade_confirm")
        if config_id:
            root_id, _ = split_lineage(config_id)
            tickets = [row for row in tickets if row.get("协议族") == root_id]
            ticket_ids = {row.get("票据") for row in tickets}
            confirms = [row for row in confirms if row.get("票据") in ticket_ids]
        return {
            "ok": True,
            "applications": tickets,
            "confirmations": confirms,
        }

    # ---------------- 设备首次绑定（注册） ----------------

    def bind_device(
        self, device_id: str, device_type: str, protocol_id: str,
    ) -> dict[str, Any]:
        """设备注册首次绑定协议：只认实际生效配置，升版冲突时挂起申请。

        - 精确编号命中已生效版本：沿仍在服务的版本完成绑定；
        - 命中待验证升级（或其协议族有进行中的升版）：保留待激活申请等待新版本，
          设备沿当前生效版本先注册，绝不冒用废弃项；
        - 只命中废弃项且无生效/待激活版本：拒绝注册。
        """
        device_id = (device_id or "").strip()
        protocol_id = (protocol_id or "").strip()
        if not device_id or not protocol_id:
            return {"ok": False, "message": "设备编号与协议配置编号均不能为空"}

        device_rows = self._device_rows()
        if any(row.get("设备编号") == device_id for row in device_rows):
            existing = next(row for row in device_rows if row.get("设备编号") == device_id)
            return {
                "ok": False,
                "message": f"设备 {device_id} 已注册，当前状态：{existing.get('status')}",
            }

        chosen = self.find_protocol(protocol_id)
        if chosen is None:
            return {"ok": False, "message": f"协议配置 {protocol_id} 不存在"}

        root_id, _ = split_lineage(protocol_id)
        family = self.family_rows(root_id)
        active = self.active_protocol(root_id)
        pending_ticket = next(
            (
                row for row in self._table("upgrade_ticket")
                if row.get("协议族") == root_id and row.get("状态") == "待确认"
            ),
            None,
        )

        # 废弃项不能用于绑定；若协议族还有生效版本则改走生效版本，否则拒绝。
        if chosen.get("status") == "已废弃":
            if active is not None:
                chosen = active
                protocol_id = str(active.get("配置编号"))
            else:
                return {
                    "ok": False,
                    "message": (
                        f"协议配置 {protocol_id} 已废弃且协议族 {root_id} 无生效版本，"
                        "不能冒用废弃项注册"
                    ),
                }

        waiting_for_upgrade = chosen.get("status") == "待验证" or pending_ticket is not None
        if waiting_for_upgrade and active is None:
            return {
                "ok": False,
                "message": (
                    f"协议族 {root_id} 仅有待验证版本、暂无生效配置，"
                    "请在新版本生效后再确认注册"
                ),
            }

        # 注册始终沿「仍在服务」的生效版本落地，保证设备可立即接入。
        serving = active if waiting_for_upgrade else chosen
        entry = {
            "id": self._next_id(DEVICE_MODULE),
            "设备编号": device_id,
            "设备类型": device_type,
            "协议类型": serving.get("协议名称", ""),
            "认证方式": serving.get("传输方式", ""),
            "所属网关": "",
            "固件版本": "v1.0.0",
            "注册时间": _now(),
            "最后在线": _now(),
            # 绑定追踪字段：实际生效配置、用户选择的升级、绑定确认状态。
            "绑定协议编号": serving.get("配置编号"),
            "绑定确认": "待确认",
            "上报协议编号": "",
            "status": "待激活",
            "pending": True,
            "abnormal": False,
        }
        device_rows.append(entry)

        binding = {
            "id": self._next_id("device_binding"),
            "设备编号": device_id,
            "协议族": root_id,
            "用户选择编号": protocol_id,
            "生效版本编号": serving.get("配置编号"),
            "目标配置编号": (
                str(pending_ticket.get("新配置编号"))
                if waiting_for_upgrade and pending_ticket
                else serving.get("配置编号")
            ),
            "等待票据": pending_ticket.get("票据") if waiting_for_upgrade and pending_ticket else "",
            "绑定状态": "待激活" if waiting_for_upgrade else "已绑定",
            "绑定时间": _now(),
        }
        self._table("device_binding").append(binding)

        if waiting_for_upgrade:
            message = (
                f"设备 {device_id} 已沿生效版本 {serving.get('配置编号')} 注册，"
                f"选择的升级 {protocol_id} 待激活，绑定申请挂起等待新版本"
            )
        else:
            message = f"设备 {device_id} 已注册并绑定生效协议 {serving.get('配置编号')}"
        return {
            "ok": True,
            "message": message,
            "entry": entry,
            "binding": binding,
            "family_versions": [
                {
                    "配置编号": row.get("配置编号"),
                    "配置版本": row.get("配置版本"),
                    "status": row.get("status"),
                }
                for row in family
            ],
        }

    def confirm_device_binding(self, device_id: str) -> dict[str, Any]:
        """确认设备绑定：只有实际使用的配置处于已生效状态才允许确认。"""
        device = next(
            (row for row in self._device_rows() if row.get("设备编号") == device_id),
            None,
        )
        if device is None:
            return {"ok": False, "message": f"设备 {device_id} 未注册"}

        binding = next(
            (
                row for row in self._table("device_binding")
                if row.get("设备编号") == device_id
            ),
            None,
        )
        if binding is None:
            return {"ok": False, "message": f"设备 {device_id} 无绑定申请记录"}

        target_id = str(binding.get("目标配置编号"))
        target = self.find_protocol(target_id)
        if target is None or target.get("status") != "已生效":
            return {
                "ok": False,
                "message": (
                    f"绑定目标 {target_id} "
                    f"当前状态：{target.get('status') if target else '不存在'}，"
                    "配置未实际生效，不能确认绑定"
                ),
            }

        binding["绑定状态"] = "已绑定"
        binding["生效版本编号"] = target_id
        binding["确认时间"] = _now()
        binding["等待票据"] = ""
        device["绑定协议编号"] = target_id
        device["绑定确认"] = "已确认"
        return {
            "ok": True,
            "message": f"设备 {device_id} 绑定确认完成，实际生效配置 {target_id}",
            "binding": binding,
        }

    def device_binding_detail(self, device_id: str) -> dict[str, Any]:
        """注册详情：区分原有效版本、用户选择的升级、尚未成功的绑定。"""
        device = next(
            (row for row in self._device_rows() if row.get("设备编号") == device_id),
            None,
        )
        if device is None:
            return {"ok": False, "message": f"设备 {device_id} 未注册"}

        binding = next(
            (
                row for row in self._table("device_binding")
                if row.get("设备编号") == device_id
            ),
            None,
        )
        root_id = str(binding.get("协议族")) if binding else ""
        active = self.active_protocol(root_id) if root_id else None

        return {
            "ok": True,
            "device": {
                "设备编号": device.get("设备编号"),
                "设备类型": device.get("设备类型"),
                "status": device.get("status"),
                "绑定协议编号": device.get("绑定协议编号"),
                "绑定确认": device.get("绑定确认"),
                "上报协议编号": device.get("上报协议编号"),
            },
            "original_active": {
                "配置编号": binding.get("生效版本编号") if binding else device.get("绑定协议编号"),
                "说明": "注册时仍在服务的原有效版本",
            },
            "selected_upgrade": {
                "配置编号": binding.get("用户选择编号") if binding else "",
                "等待票据": binding.get("等待票据") if binding else "",
                "说明": "用户在注册页选择的升级版本",
            },
            "pending_binding": {
                "目标配置编号": binding.get("目标配置编号") if binding else "",
                "绑定状态": binding.get("绑定状态") if binding else "",
                "说明": "尚未成功确认的绑定申请",
            },
            "current_active": {
                "配置编号": active.get("配置编号") if active else "",
                "status": active.get("status") if active else "",
            },
            "family_versions": [
                {
                    "配置编号": row.get("配置编号"),
                    "配置版本": row.get("配置版本"),
                    "status": row.get("status"),
                    "来源编号": row.get("来源编号", ""),
                    "替代编号": row.get("替代编号", ""),
                }
                for row in (self.family_rows(root_id) if root_id else [])
            ],
        }

    def report_device_protocol(self, device_id: str) -> dict[str, Any]:
        """办结后上报：解析并记录设备实际有效的配置，废弃版本不会被上报。"""
        device = next(
            (row for row in self._device_rows() if row.get("设备编号") == device_id),
            None,
        )
        if device is None:
            return {"ok": False, "message": f"设备 {device_id} 未注册"}
        if device.get("绑定确认") != "已确认":
            return {
                "ok": False,
                "message": "绑定尚未确认，配置未实际生效，不能上报使用情况",
            }

        binding = next(
            (
                row for row in self._table("device_binding")
                if row.get("设备编号") == device_id
            ),
            None,
        )
        root_id = str(binding.get("协议族")) if binding else ""
        active = self.active_protocol(root_id) if root_id else None
        if active is None:
            return {"ok": False, "message": f"协议族 {root_id} 无生效配置，无法上报"}

        device["上报协议编号"] = active.get("配置编号")
        device["最后在线"] = _now()
        return {
            "ok": True,
            "message": f"设备 {device_id} 已上报实际有效配置 {active.get('配置编号')}",
            "report": {
                "设备编号": device_id,
                "上报协议编号": active.get("配置编号"),
                "配置版本": active.get("配置版本"),
                "上报时间": device["最后在线"],
            },
        }


lifecycle_service = ProtocolLifecycleService()
