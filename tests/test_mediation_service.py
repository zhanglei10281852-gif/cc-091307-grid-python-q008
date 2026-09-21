"""调解协作服务测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.errors import (
    InvalidStateTransition,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
    VersionConflictError,
)
from src.models import CaseStatus, EvidenceStatus, ProposalStatus
from src.service import MediationService

MEDIATOR = "med-1"


class MediationServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.service = MediationService(self.db_path)

    def tearDown(self) -> None:
        self.service.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def make_case(self):
        """立案并登记双方、分派调解员，返回 (case_id, party_a, party_b)。"""
        case = self.service.create_case("公共露台使用纠纷", "两户居民就露台使用时间产生争议")
        pa = self.service.add_party(case["id"], "张桂兰", contact="13800000001")
        pb = self.service.add_party(case["id"], "李国强", contact="13800000002")
        self.service.assign_mediator(case["id"], MEDIATOR)
        return case["id"], pa["id"], pb["id"]

    def make_mediating_case(self):
        """进入调解中的案件。"""
        case_id, pa, pb = self.make_case()
        self.service.start_mediation(case_id, MEDIATOR)
        return case_id, pa, pb

    def future(self, **kwargs) -> datetime:
        return datetime.now(timezone.utc) + timedelta(**kwargs)

    # ------------------------------------------------------------------
    # 完整流程与时间线
    # ------------------------------------------------------------------
    def test_full_mediation_flow_and_timeline(self):
        case_id, pa, pb = self.make_case()
        self.service.start_mediation(case_id, MEDIATOR)
        self.service.add_issue(case_id, "露台使用时段划分", raised_by=pa)

        ev = self.service.submit_evidence(
            case_id, pa, "聊天记录截图", "2026-09-01 对方占用露台至深夜"
        )
        self.assertEqual(ev["status"], EvidenceStatus.PENDING_VERIFICATION.value)
        self.service.verify_evidence(case_id, ev["id"], MEDIATOR)

        m = self.service.record_minutes(case_id, 1, "首次会谈：双方陈述诉求", MEDIATOR)
        self.service.update_minutes(case_id, m["id"], 1, "首次会谈（修订）：补充时间约定", MEDIATOR)

        prop = self.service.propose_settlement(case_id, "单双周轮换使用露台", MEDIATOR)
        self.service.confirm_proposal(case_id, prop["id"], pa, accept=True)
        final = self.service.confirm_proposal(case_id, prop["id"], pb, accept=True)
        self.assertEqual(final["status"], ProposalStatus.CONFIRMED.value)
        self.assertEqual(self.service.get_case(case_id)["status"], CaseStatus.RESOLVED.value)

        self.service.close_case(case_id, MEDIATOR)
        closed = self.service.get_case(case_id)
        self.assertEqual(closed["status"], CaseStatus.CLOSED.value)
        self.assertIsNotNone(closed["closed_at"])

        types = [e["event_type"] for e in self.service.get_timeline(case_id)]
        expected_order = [
            "CASE_CREATED", "PARTY_ADDED", "PARTY_ADDED", "MEDIATOR_ASSIGNED",
            "MEDIATION_STARTED", "ISSUE_RAISED", "EVIDENCE_SUBMITTED",
            "EVIDENCE_VERIFIED", "SESSION_RECORDED", "MINUTES_UPDATED",
            "PROPOSAL_SUBMITTED", "PROPOSAL_CONFIRMED", "PROPOSAL_CONFIRMED",
            "CASE_RESOLVED", "CASE_CLOSED",
        ]
        self.assertEqual(types, expected_order)

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------
    def test_invalid_transitions_rejected(self):
        case = self.service.create_case("噪音纠纷")
        pa = self.service.add_party(case["id"], "甲方")
        self.service.add_party(case["id"], "乙方")
        case_id = case["id"]

        # 未分派调解员时，任何人都不能以调解员身份操作
        with self.assertRaises(PermissionDeniedError):
            self.service.close_case(case_id, MEDIATOR, reason="x")
        with self.assertRaises(PermissionDeniedError):
            self.service.suspend_case(case_id, "当事人外出", MEDIATOR)
        self.service.assign_mediator(case_id, MEDIATOR)
        with self.assertRaises(PermissionDeniedError):
            self.service.start_mediation(case_id, "med-2")
        # 已分派但未开始调解：不能结案、不能提方案
        with self.assertRaises(InvalidStateTransition):
            self.service.close_case(case_id, MEDIATOR, reason="x")
        with self.assertRaises(InvalidStateTransition):
            self.service.propose_settlement(case_id, "方案", MEDIATOR)
        # 未暂缓不能恢复
        with self.assertRaises(InvalidStateTransition):
            self.service.resume_case(case_id, MEDIATOR)
        # 结案是终态
        self.service.start_mediation(case_id, MEDIATOR)
        self.service.close_case(case_id, MEDIATOR, reason="双方自行和解")
        with self.assertRaises(InvalidStateTransition):
            self.service.start_mediation(case_id, MEDIATOR)
        with self.assertRaises(InvalidStateTransition):
            self.service.submit_evidence(case_id, pa, "补充", "新证据")

    def test_close_without_agreement_requires_reason(self):
        case_id, _, _ = self.make_mediating_case()
        with self.assertRaises(ValidationError):
            self.service.close_case(case_id, MEDIATOR)
        closed = self.service.close_case(case_id, MEDIATOR, reason="一方拒绝继续调解")
        self.assertEqual(closed["close_reason"], "一方拒绝继续调解")

    def test_suspend_and_resume_returns_to_previous_status(self):
        case_id, pa, _ = self.make_mediating_case()
        self.service.suspend_case(case_id, "当事人出差", MEDIATOR)
        case = self.service.get_case(case_id)
        self.assertEqual(case["status"], CaseStatus.SUSPENDED.value)
        self.assertEqual(case["previous_status"], CaseStatus.IN_MEDIATION.value)
        self.service.resume_case(case_id, MEDIATOR)
        self.assertEqual(self.service.get_case(case_id)["status"], CaseStatus.IN_MEDIATION.value)

        # 从方案确认阶段暂缓，恢复后仍回到方案确认阶段
        prop = self.service.propose_settlement(case_id, "错峰使用", MEDIATOR)
        self.service.suspend_case(case_id, "等待物业提供资料", MEDIATOR)
        self.service.resume_case(case_id, MEDIATOR)
        self.assertEqual(
            self.service.get_case(case_id)["status"], CaseStatus.PROPOSAL_REVIEW.value
        )
        # 暂缓不影响后续确认
        self.service.confirm_proposal(case_id, prop["id"], pa, accept=True)

    # ------------------------------------------------------------------
    # 证据：去重 / 撤回保留 / 权限
    # ------------------------------------------------------------------
    def test_duplicate_evidence_is_deduplicated(self):
        case_id, pa, _ = self.make_mediating_case()
        e1 = self.service.submit_evidence(case_id, pa, "照片", "露台堆物照片-9月1日")
        e2 = self.service.submit_evidence(case_id, pa, "照片", "露台堆物照片-9月1日")
        self.assertEqual(e1["id"], e2["id"])
        self.assertFalse(e1["deduplicated"])
        self.assertTrue(e2["deduplicated"])
        self.assertEqual(len(self.service.list_evidence(case_id)), 1)
        submitted = [e for e in self.service.get_timeline(case_id)
                     if e["event_type"] == "EVIDENCE_SUBMITTED"]
        self.assertEqual(len(submitted), 1)

        # 内容不同则是新证据
        e3 = self.service.submit_evidence(case_id, pa, "照片", "露台堆物照片-9月2日")
        self.assertNotEqual(e3["id"], e1["id"])
        self.assertEqual(len(self.service.list_evidence(case_id)), 2)

    def test_withdrawn_evidence_keeps_original(self):
        case_id, pa, pb = self.make_mediating_case()
        ev = self.service.submit_evidence(case_id, pa, "录音", "9月1日现场录音")
        # 非提交方不能撤回
        with self.assertRaises(PermissionDeniedError):
            self.service.withdraw_evidence(case_id, ev["id"], pb)
        withdrawn = self.service.withdraw_evidence(case_id, ev["id"], pa)
        self.assertEqual(withdrawn["status"], EvidenceStatus.WITHDRAWN.value)
        # 原文与提交时间保留
        again = self.service.get_evidence(case_id, ev["id"])
        self.assertEqual(again["content"], "9月1日现场录音")
        self.assertEqual(again["submitted_at"], ev["submitted_at"])
        # 已撤回证据不能再核验
        with self.assertRaises(ValidationError):
            self.service.verify_evidence(case_id, ev["id"], MEDIATOR)

    def test_evidence_permissions(self):
        case_id, pa, pb = self.make_mediating_case()
        ev = self.service.submit_evidence(case_id, pa, "照片", "现场照片")
        # 当事人不能核验证据
        with self.assertRaises(PermissionDeniedError):
            self.service.verify_evidence(case_id, ev["id"], pa)
        # 其他案件的当事人/无关人不能查看
        with self.assertRaises(PermissionDeniedError):
            self.service.view_evidence(case_id, "outsider")

    # ------------------------------------------------------------------
    # 隐私字段与授权共享
    # ------------------------------------------------------------------
    def test_private_fields_require_authorization(self):
        case_id, pa, pb = self.make_mediating_case()
        self.service.submit_evidence(
            case_id, pa, "聊天记录", "微信沟通记录",
            private_fields={"phone": "13800000001", "address": "3栋502"},
        )
        # 提交方与调解员可见
        owner_view = self.service.view_evidence(case_id, pa)
        self.assertEqual(owner_view[0]["private_fields"]["phone"], "13800000001")
        med_view = self.service.view_evidence(case_id, MEDIATOR)
        self.assertEqual(med_view[0]["private_fields"]["address"], "3栋502")
        # 另一方未授权不可见
        other_view = self.service.view_evidence(case_id, pb)
        self.assertIsNone(other_view[0]["private_fields"])
        self.assertTrue(other_view[0]["private_fields_redacted"])
        # 但公开内容仍可见
        self.assertEqual(other_view[0]["content"], "微信沟通记录")

    def test_case_level_and_evidence_level_grants(self):
        case_id, pa, pb = self.make_mediating_case()
        e1 = self.service.submit_evidence(
            case_id, pa, "证据一", "内容一", private_fields={"k1": "v1"}
        )
        e2 = self.service.submit_evidence(
            case_id, pa, "证据二", "内容二", private_fields={"k2": "v2"}
        )
        # 单条证据授权：只放开该条
        self.service.grant_share(case_id, pa, evidence_id=e1["id"])
        view = {e["id"]: e for e in self.service.view_evidence(case_id, pb)}
        self.assertEqual(view[e1["id"]]["private_fields"], {"k1": "v1"})
        self.assertIsNone(view[e2["id"]]["private_fields"])

        # 重复授权幂等
        self.service.grant_share(case_id, pa, evidence_id=e1["id"])
        self.assertEqual(len(self.service.list_share_grants(case_id)), 1)

        # 案件级授权：全部放开，包括当事人联系方式
        self.service.grant_share(case_id, pa)
        view = {e["id"]: e for e in self.service.view_evidence(case_id, pb)}
        self.assertEqual(view[e2["id"]]["private_fields"], {"k2": "v2"})
        case_view = self.service.view_case(case_id, pb)
        contacts = {p["id"]: p["contact"] for p in case_view["parties"]}
        self.assertEqual(contacts[pa], "13800000001")

    def test_revoke_share_restores_privacy(self):
        case_id, pa, pb = self.make_mediating_case()
        self.service.submit_evidence(
            case_id, pa, "聊天记录", "内容", private_fields={"phone": "13800000001"}
        )
        grant = self.service.grant_share(case_id, pa)
        self.assertEqual(
            self.service.view_evidence(case_id, pb)[0]["private_fields"],
            {"phone": "13800000001"},
        )
        # 被授权方不能撤销别人的授权
        with self.assertRaises(PermissionDeniedError):
            self.service.revoke_share(case_id, grant["id"], pb)
        self.service.revoke_share(case_id, grant["id"], pa)
        view = self.service.view_evidence(case_id, pb)
        self.assertIsNone(view[0]["private_fields"])
        self.assertEqual(self.service.list_share_grants(case_id), [])
        self.assertEqual(len(self.service.list_share_grants(case_id, include_revoked=True)), 1)

    def test_view_case_contact_privacy(self):
        case_id, pa, pb = self.make_mediating_case()
        # 当事人看对方联系方式被遮蔽
        view_b = self.service.view_case(case_id, pb)
        contacts = {p["id"]: p["contact"] for p in view_b["parties"]}
        self.assertIsNone(contacts[pa])
        self.assertEqual(contacts[pb], "13800000002")
        # 调解员全可见
        view_m = self.service.view_case(case_id, MEDIATOR)
        self.assertEqual(
            {p["id"]: p["contact"] for p in view_m["parties"]}[pa], "13800000001"
        )
        # 无关人员无权查看
        with self.assertRaises(PermissionDeniedError):
            self.service.view_case(case_id, "outsider")

    # ------------------------------------------------------------------
    # 会谈纪要版本
    # ------------------------------------------------------------------
    def test_minutes_version_conflict_and_history(self):
        case_id, _, _ = self.make_mediating_case()
        m = self.service.record_minutes(case_id, 1, "首次会谈纪要", MEDIATOR)
        self.assertEqual(m["version"], 1)

        m2 = self.service.update_minutes(case_id, m["id"], 1, "修订：补充双方陈述", MEDIATOR)
        self.assertEqual(m2["version"], 2)

        # 基于过期版本的并发写入被拒绝，且不产生任何副作用
        with self.assertRaises(VersionConflictError) as ctx:
            self.service.update_minutes(case_id, m["id"], 1, "过期版本写入", MEDIATOR)
        self.assertEqual(ctx.exception.current, 2)
        self.assertEqual(self.service.get_minute(case_id, m["id"])["content"], "修订：补充双方陈述")

        history = self.service.get_minutes_history(case_id, m["id"])
        self.assertEqual([h["version"] for h in history], [1, 2])
        self.assertEqual(history[0]["content"], "首次会谈纪要")
        self.assertEqual(history[1]["content"], "修订：补充双方陈述")

        # 当事人不能记录/修改纪要
        with self.assertRaises(PermissionDeniedError):
            self.service.record_minutes(case_id, 2, "内容", "someone")

    # ------------------------------------------------------------------
    # 方案确认
    # ------------------------------------------------------------------
    def test_both_parties_must_confirm(self):
        case_id, pa, pb = self.make_mediating_case()
        prop = self.service.propose_settlement(case_id, "单双周轮换", MEDIATOR)
        self.service.confirm_proposal(case_id, prop["id"], pa, accept=True)
        # 只有一方确认时案件仍在确认阶段
        self.assertEqual(
            self.service.get_case(case_id)["status"], CaseStatus.PROPOSAL_REVIEW.value
        )
        # 同一方不能重复表态
        with self.assertRaises(ValidationError):
            self.service.confirm_proposal(case_id, prop["id"], pa, accept=True)
        self.service.confirm_proposal(case_id, prop["id"], pb, accept=True)
        self.assertEqual(self.service.get_case(case_id)["status"], CaseStatus.RESOLVED.value)

    def test_proposal_rejection_returns_to_mediation(self):
        case_id, pa, pb = self.make_mediating_case()
        prop = self.service.propose_settlement(case_id, "方案一", MEDIATOR)
        rejected = self.service.confirm_proposal(case_id, prop["id"], pb, accept=False, note="时段不合理")
        self.assertEqual(rejected["status"], ProposalStatus.REJECTED.value)
        self.assertEqual(
            self.service.get_case(case_id)["status"], CaseStatus.IN_MEDIATION.value
        )
        # 可以重新提出方案
        prop2 = self.service.propose_settlement(case_id, "方案二", MEDIATOR)
        self.assertEqual(prop2["status"], ProposalStatus.PENDING.value)

    # ------------------------------------------------------------------
    # 工作台：下一步动作与逾期原因
    # ------------------------------------------------------------------
    def test_next_actions_by_status(self):
        case = self.service.create_case("漏水纠纷")
        case_id = case["id"]
        actions = [a["action"] for a in self.service.get_next_actions(case_id)]
        self.assertEqual(actions, ["add_party"])

        self.service.add_party(case_id, "甲方")
        self.service.add_party(case_id, "乙方")
        actions = [a["action"] for a in self.service.get_next_actions(case_id)]
        self.assertEqual(actions, ["assign_mediator"])

        self.service.assign_mediator(case_id, MEDIATOR)
        actions = [a["action"] for a in self.service.get_next_actions(case_id)]
        self.assertEqual(actions, ["start_mediation"])

        self.service.start_mediation(case_id, MEDIATOR)
        pa = self.service.get_case(case_id)["parties"][0]["id"]
        self.service.submit_evidence(case_id, pa, "照片", "漏水照片")
        actions = [a["action"] for a in self.service.get_next_actions(case_id)]
        self.assertIn("verify_evidence", actions)
        self.assertIn("propose_settlement", actions)

        prop = self.service.propose_settlement(case_id, "维修费分摊", MEDIATOR)
        actions = self.service.get_next_actions(case_id)
        self.assertEqual(actions[0]["action"], "await_confirmation")
        self.assertIn("甲方", actions[0]["detail"])

        for p in self.service.get_case(case_id)["parties"]:
            self.service.confirm_proposal(case_id, prop["id"], p["id"], accept=True)
        actions = [a["action"] for a in self.service.get_next_actions(case_id)]
        self.assertEqual(actions, ["close_case"])

        self.service.close_case(case_id, MEDIATOR)
        self.assertEqual(self.service.get_next_actions(case_id), [])

    def test_overdue_reasons(self):
        case = self.service.create_case("占道纠纷")
        case_id = case["id"]
        # 立案未分派
        reasons = self.service.get_overdue_reasons(case_id, now=self.future(hours=49))
        self.assertTrue(any("未分派调解员" in r for r in reasons))

        pa = self.service.add_party(case_id, "甲方")
        self.service.add_party(case_id, "乙方")
        self.service.assign_mediator(case_id, MEDIATOR)
        # 分派后未开始调解
        reasons = self.service.get_overdue_reasons(case_id, now=self.future(days=8))
        self.assertTrue(any("未开始调解" in r for r in reasons))

        self.service.start_mediation(case_id, MEDIATOR)
        self.service.submit_evidence(case_id, pa["id"], "照片", "占道照片")
        # 证据待核验超期
        reasons = self.service.get_overdue_reasons(case_id, now=self.future(days=6))
        self.assertTrue(any("待核验" in r for r in reasons))

        # 方案确认超期，提示未表态方
        prop = self.service.propose_settlement(case_id, "限期清理", MEDIATOR)
        self.service.confirm_proposal(case_id, prop["id"], pa["id"], accept=True)
        reasons = self.service.get_overdue_reasons(case_id, now=self.future(days=8))
        self.assertTrue(any("未获双方确认" in r and "乙方" in r for r in reasons))

        # 暂缓超期
        self.service.suspend_case(case_id, "等待鉴定", MEDIATOR)
        reasons = self.service.get_overdue_reasons(case_id, now=self.future(days=31))
        self.assertTrue(any("暂缓" in r for r in reasons))

        # 正常推进无逾期
        fresh_id, _, _ = self.make_mediating_case()
        self.assertEqual(self.service.get_overdue_reasons(fresh_id), [])

    # ------------------------------------------------------------------
    # 结果查询
    # ------------------------------------------------------------------
    def test_case_result_query(self):
        case_id, pa, pb = self.make_mediating_case()
        self.service.add_issue(case_id, "使用时段", raised_by=pa)
        ev = self.service.submit_evidence(case_id, pa, "照片", "现场照片")
        self.service.verify_evidence(case_id, ev["id"], MEDIATOR)
        self.service.submit_evidence(case_id, pb, "说明", "情况说明")
        prop = self.service.propose_settlement(case_id, "单双周轮换使用", MEDIATOR)
        self.service.confirm_proposal(case_id, prop["id"], pa, accept=True)
        self.service.confirm_proposal(case_id, prop["id"], pb, accept=True)
        self.service.close_case(case_id, MEDIATOR)

        result = self.service.get_case_result(case_id)
        self.assertEqual(result["status"], CaseStatus.CLOSED.value)
        self.assertEqual(result["agreement"]["content"], "单双周轮换使用")
        self.assertEqual(len(result["agreement"]["confirmations"]), 2)
        self.assertEqual(result["evidence_summary"]["total"], 2)
        self.assertEqual(result["evidence_summary"]["verified"], 1)
        self.assertEqual(result["evidence_summary"]["pending_verification"], 1)
        self.assertEqual(len(result["issues"]), 1)
        self.assertEqual(result["close_reason"], "双方已确认调解方案")

    # ------------------------------------------------------------------
    # 重启一致性
    # ------------------------------------------------------------------
    def test_restart_preserves_grants_and_minute_versions(self):
        case_id, pa, pb = self.make_mediating_case()
        self.service.submit_evidence(
            case_id, pa, "聊天记录", "内容", private_fields={"phone": "13800000001"}
        )
        self.service.grant_share(case_id, pa)
        m = self.service.record_minutes(case_id, 1, "首次会谈", MEDIATOR)
        self.service.update_minutes(case_id, m["id"], 1, "首次会谈（修订）", MEDIATOR)
        self.service.suspend_case(case_id, "当事人请假", MEDIATOR)
        timeline_len = len(self.service.get_timeline(case_id))
        self.service.close()

        # 重新打开同一数据库：共享授权、纪要版本、案件状态、时间线全部保持
        svc2 = MediationService(self.db_path)
        try:
            grants = svc2.list_share_grants(case_id)
            self.assertEqual(len(grants), 1)
            self.assertTrue(grants[0]["active"])
            view = svc2.view_evidence(case_id, pb)
            self.assertEqual(view[0]["private_fields"], {"phone": "13800000001"})

            minute = svc2.get_minute(case_id, m["id"])
            self.assertEqual(minute["version"], 2)
            self.assertEqual(minute["content"], "首次会谈（修订）")
            self.assertEqual(len(svc2.get_minutes_history(case_id, m["id"])), 2)

            case = svc2.get_case(case_id)
            self.assertEqual(case["status"], CaseStatus.SUSPENDED.value)
            self.assertEqual(case["previous_status"], CaseStatus.IN_MEDIATION.value)
            self.assertEqual(len(svc2.get_timeline(case_id)), timeline_len)

            # 重启后版本冲突检测仍然有效
            with self.assertRaises(VersionConflictError):
                svc2.update_minutes(case_id, m["id"], 1, "过期写入", MEDIATOR)
        finally:
            svc2.close()
        # 恢复 self.service 供 tearDown 使用
        self.service = MediationService(self.db_path)

    # ------------------------------------------------------------------
    # 其他校验
    # ------------------------------------------------------------------
    def test_not_found_errors(self):
        with self.assertRaises(NotFoundError):
            self.service.get_case("case_missing")
        case_id, pa, _ = self.make_mediating_case()
        with self.assertRaises(NotFoundError):
            self.service.get_evidence(case_id, "ev_missing")
        with self.assertRaises(NotFoundError):
            self.service.submit_evidence(case_id, "party_missing", "t", "c")

    def test_assign_requires_two_parties(self):
        case = self.service.create_case("宠物纠纷")
        self.service.add_party(case["id"], "甲方")
        with self.assertRaises(ValidationError):
            self.service.assign_mediator(case["id"], MEDIATOR)

    def test_single_pending_proposal(self):
        case_id, _, _ = self.make_mediating_case()
        self.service.propose_settlement(case_id, "方案一", MEDIATOR)
        # 已有待确认方案，重复提出被拒绝
        with self.assertRaises(ValidationError):
            self.service.propose_settlement(case_id, "方案二", MEDIATOR)


if __name__ == "__main__":
    unittest.main()
