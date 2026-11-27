"""协议升版与设备绑定生命周期场景测试。

完整复刻业务叙事：
原配置 P-11、废弃项已占 P-11-v2，维护员在工作台发起版本 3；
新协议待验证时另一窗口提交设备 D 的首次绑定并与升版冲突；
重新打开注册详情核对绑定走向；升版确认支持响应丢失后的幂等取回；
另一次有效升级生成不同版本；注册确认只认实际生效配置；办结上报实际有效配置。
"""
from __future__ import annotations

import pytest

from app.store import store
from app.services.protocol_cfg import ProtocolCfgService
from app.services.protocol_lifecycle import (
    build_config_id,
    lifecycle_service,
    split_lineage,
)


@pytest.fixture(autouse=True)
def reset_scenario():
    """每个用例前把 P-11 协议族与辅助表恢复到叙事初始状态。"""
    protocol_rows = store.rows("protocol_cfg")
    for row in list(protocol_rows):
        if row.get("协议族") == "P-11" or str(row.get("配置编号", "")).startswith("P-11"):
            protocol_rows.remove(row)
    protocol_rows.append({
        "id": 9001, "status": "已生效", "pending": False, "abnormal": False,
        "配置编号": "P-11", "协议名称": "工业Modbus接入协议", "传输方式": "TCP",
        "数据格式": "二进制", "报文分隔符": "\r\n", "心跳间隔": "30",
        "超时秒数": "90", "配置版本": "v1",
        "协议族": "P-11", "版本序号": 1,
    })
    protocol_rows.append({
        "id": 9002, "status": "已废弃", "pending": False, "abnormal": False,
        "配置编号": "P-11-v2", "协议名称": "工业Modbus接入协议", "传输方式": "TCP",
        "数据格式": "二进制", "报文分隔符": "\r\n", "心跳间隔": "30",
        "超时秒数": "90", "配置版本": "v2",
        "协议族": "P-11", "版本序号": 2, "来源编号": "P-11",
        "废弃时间": "2026-10-01T10:00:00+08:00",
    })
    for table in ("maintainer_credential", "upgrade_ticket", "upgrade_confirm", "device_binding"):
        store.rows(table).clear()
    device_rows = store.rows("device_registry")
    for row in list(device_rows):
        if str(row.get("设备编号", "")).startswith("D-SCN"):
            device_rows.remove(row)
    yield


class TestLineageAndNumbering:
    def test_split_and_build_lineage(self):
        assert split_lineage("P-11") == ("P-11", 1)
        assert split_lineage("P-11-v2") == ("P-11", 2)
        assert build_config_id("P-11", 1) == "P-11"
        assert build_config_id("P-11", 3) == "P-11-v3"

    def test_legacy_upgrade_skips_occupied_v2_suffix(self):
        """同后缀编号已被废弃项占用时，再次升版不得重复 P-11-v2。"""
        result = ProtocolCfgService().version_upgrade("P-11", "v3-draft")
        assert result["ok"] is True
        assert result["new_version_id"] == "P-11-v3"
        ids = [row["配置编号"] for row in lifecycle_service.family_rows("P-11")]
        assert ids.count("P-11-v2") == 1
        assert "P-11-v3" in ids

    def test_deprecated_entry_keeps_original_id_and_lineage(self):
        ProtocolCfgService().version_upgrade("P-11", "v3-draft")
        deprecated = lifecycle_service.find_protocol("P-11-v2")
        assert deprecated is not None
        assert deprecated["status"] == "已废弃"
        assert deprecated["配置编号"] == "P-11-v2"
        assert deprecated["来源编号"] == "P-11"


class TestWorkbenchUpgradeFlow:
    def _configure(self):
        return lifecycle_service.configure_operator("maint-wang", "secret-token", "P-11")

    def test_apply_requires_credential(self):
        result = lifecycle_service.apply_upgrade("P-11", "v3", "maint-wang", "wrong", "T-1")
        assert result["ok"] is False
        assert "凭据" in result["message"]

    def test_full_upgrade_creates_v3_and_keeps_old_serving_until_confirm(self):
        self._configure()
        applied = lifecycle_service.apply_upgrade(
            "P-11", "v3", "maint-wang", "secret-token", "TICKET-V3",
        )
        assert applied["ok"] is True
        assert applied["ticket"]["新配置编号"] == "P-11-v3"
        # 申请阶段旧版本仍在服务，新版本待验证，注册页仍能定位到生效项。
        assert lifecycle_service.find_protocol("P-11")["status"] == "已生效"
        assert lifecycle_service.find_protocol("P-11-v3")["status"] == "待验证"
        assert lifecycle_service.active_protocol("P-11")["配置编号"] == "P-11"

        confirmed = lifecycle_service.confirm_upgrade(
            "TICKET-V3", "maint-wang", "secret-token",
        )
        assert confirmed["ok"] is True
        assert lifecycle_service.find_protocol("P-11")["status"] == "已废弃"
        assert lifecycle_service.find_protocol("P-11-v3")["status"] == "已生效"
        assert lifecycle_service.active_protocol("P-11")["配置编号"] == "P-11-v3"
        # 旧版本保留原编号与历史归属。
        old = lifecycle_service.find_protocol("P-11")
        assert old["替代编号"] == "P-11-v3"

    def test_confirm_is_idempotent_after_lost_response(self):
        """响应丢失后回原页面再点确认，取回同一笔办理结果，不重复切换。"""
        self._configure()
        lifecycle_service.apply_upgrade("P-11", "v3", "maint-wang", "secret-token", "T-LOST")
        first = lifecycle_service.confirm_upgrade("T-LOST", "maint-wang", "secret-token")
        second = lifecycle_service.confirm_upgrade("T-LOST", "maint-wang", "secret-token")
        assert first["ok"] is True and second["ok"] is True
        assert second.get("idempotent") is True
        assert first["confirmation"]["id"] == second["confirmation"]["id"]
        confirms = store.rows("upgrade_confirm")
        assert [row["票据"] for row in confirms].count("T-LOST") == 1

    def test_apply_is_idempotent_with_same_ticket(self):
        self._configure()
        first = lifecycle_service.apply_upgrade(
            "P-11", "v3", "maint-wang", "secret-token", "T-DUP",
        )
        second = lifecycle_service.apply_upgrade(
            "P-11", "v3", "maint-wang", "secret-token", "T-DUP",
        )
        assert second.get("idempotent") is True
        assert first["ticket"]["id"] == second["ticket"]["id"]
        assert len([r for r in store.rows("upgrade_ticket") if r["票据"] == "T-DUP"]) == 1

    def test_second_valid_upgrade_generates_distinct_version(self):
        """另一次有效升级必须区分版本身份：v3 办结后再升版得到 v4。"""
        self._configure()
        lifecycle_service.apply_upgrade("P-11", "v3", "maint-wang", "secret-token", "T-3")
        lifecycle_service.confirm_upgrade("T-3", "maint-wang", "secret-token")
        second = lifecycle_service.apply_upgrade(
            "P-11-v3", "v4", "maint-wang", "secret-token", "T-4",
        )
        assert second["ok"] is True
        assert second["ticket"]["新配置编号"] == "P-11-v4"
        assert second["ticket"]["新配置编号"] != "P-11-v3"

    def test_cannot_apply_upgrade_from_deprecated_entry(self):
        self._configure()
        result = lifecycle_service.apply_upgrade(
            "P-11-v2", "vX", "maint-wang", "secret-token", "T-X",
        )
        assert result["ok"] is False
        assert "生效版本" in result["message"]

    def test_upgrade_history_keeps_old_id_and_confirmation_separately(self):
        self._configure()
        lifecycle_service.apply_upgrade("P-11", "v3", "maint-wang", "secret-token", "T-H")
        lifecycle_service.confirm_upgrade("T-H", "maint-wang", "secret-token")
        history = lifecycle_service.upgrade_history("P-11-v2")
        assert history["applications"][0]["原配置编号"] == "P-11"
        assert history["confirmations"][0]["票据"] == "T-H"
        assert history["confirmations"][0]["新配置编号"] == "P-11-v3"


class TestDeviceBindingConflict:
    def _start_upgrade(self):
        lifecycle_service.configure_operator("maint-wang", "secret-token", "P-11")
        return lifecycle_service.apply_upgrade(
            "P-11", "v3", "maint-wang", "secret-token", "T-CONFLICT",
        )

    def test_binding_during_pending_upgrade_serves_active_and_holds_application(self):
        self._start_upgrade()
        result = lifecycle_service.bind_device("D-SCN-1", "温湿度传感器", "P-11-v3")
        assert result["ok"] is True
        # 沿仍在服务的 P-11 完成注册，而不是冒用废弃项，也不是绑定待验证版本。
        assert result["entry"]["绑定协议编号"] == "P-11"
        assert result["binding"]["绑定状态"] == "待激活"
        assert result["binding"]["用户选择编号"] == "P-11-v3"
        assert result["binding"]["等待票据"] == "T-CONFLICT"

    def test_binding_explicit_deprecated_id_never_uses_deprecated(self):
        self._start_upgrade()
        result = lifecycle_service.bind_device("D-SCN-2", "温湿度传感器", "P-11-v2")
        assert result["ok"] is True
        assert result["binding"]["生效版本编号"] == "P-11"
        assert result["entry"]["绑定协议编号"] == "P-11"

    def test_detail_distinguishes_original_selected_and_pending(self):
        self._start_upgrade()
        lifecycle_service.bind_device("D-SCN-3", "温湿度传感器", "P-11-v3")
        detail = lifecycle_service.device_binding_detail("D-SCN-3")
        assert detail["original_active"]["配置编号"] == "P-11"
        assert detail["selected_upgrade"]["配置编号"] == "P-11-v3"
        assert detail["pending_binding"]["绑定状态"] == "待激活"
        statuses = {row["配置编号"]: row["status"] for row in detail["family_versions"]}
        assert statuses["P-11"] == "已生效"
        assert statuses["P-11-v2"] == "已废弃"
        assert statuses["P-11-v3"] == "待验证"

    def test_confirm_binding_blocked_until_config_effective(self):
        self._start_upgrade()
        lifecycle_service.bind_device("D-SCN-4", "温湿度传感器", "P-11-v3")
        blocked = lifecycle_service.confirm_device_binding("D-SCN-4")
        assert blocked["ok"] is False
        assert "未实际生效" in blocked["message"]

        lifecycle_service.confirm_upgrade("T-CONFLICT", "maint-wang", "secret-token")
        # 升版办结后挂起申请自动对接到新生效版本，此时确认才放行。
        confirmed = lifecycle_service.confirm_device_binding("D-SCN-4")
        assert confirmed["ok"] is True
        assert confirmed["binding"]["目标配置编号"] == "P-11-v3"

    def test_report_uses_actual_effective_config_after_completion(self):
        self._start_upgrade()
        lifecycle_service.bind_device("D-SCN-5", "温湿度传感器", "P-11-v3")
        # 未确认绑定前不允许上报。
        assert lifecycle_service.report_device_protocol("D-SCN-5")["ok"] is False
        lifecycle_service.confirm_upgrade("T-CONFLICT", "maint-wang", "secret-token")
        lifecycle_service.confirm_device_binding("D-SCN-5")
        report = lifecycle_service.report_device_protocol("D-SCN-5")
        assert report["ok"] is True
        assert report["report"]["上报协议编号"] == "P-11-v3"

    def test_binding_rejected_when_no_effective_version_exists(self):
        """协议族无生效版本时，待验证配置不能用来确认注册，废弃项也不能冒用。"""
        # 发起升版制造待验证的 P-11-v3，再把 P-11 一并置废弃，构造无生效版本的协议族。
        self._start_upgrade()
        lifecycle_service.find_protocol("P-11")["status"] = "已废弃"
        result = lifecycle_service.bind_device("D-SCN-6", "温湿度传感器", "P-11-v3")
        assert result["ok"] is False
        assert "生效" in result["message"]

    def test_duplicate_device_registration_rejected(self):
        lifecycle_service.bind_device("D-SCN-7", "温湿度传感器", "P-11")
        again = lifecycle_service.bind_device("D-SCN-7", "温湿度传感器", "P-11")
        assert again["ok"] is False
