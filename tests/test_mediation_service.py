"""调解协作服务的端到端测试（标准库 unittest，可直接运行）。

运行方式：python -m unittest discover -s tests -v
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src import (
    AuthorizationError,
    CaseStatus,
    EvidenceStatus,
    EventType,
    GrantStatus,
    MediationService,
    NotFoundError,
    ProposalStatus,
    Resolution,
    StateTransitionError,
    ValidationError,
    VersionConflictError,
)


class _Clock:
    """可控时钟，用于逾期逻辑测试。"""
    def __init__(self):
        self.t = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kwargs):
        self.t += timedelta(**kwargs)


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.svc = MediationService(":memory:", now_fn=self.clock)
        self.alice = self.svc.register_party("张阿姨", "13800000001")
        self.bob = self.svc.register_party("李师傅", "13800000002")
        self.case = self.svc.create_case(
            "楼道公共空间堆物争议", "张阿姨反映李师傅在楼道堆放杂物",
            "公共空间使用", self.alice.id, self.bob.id)
        self.mediator = "mediator-001"

    def tearDown(self):
        self.svc.close()

    def _to_assigned(self):
        case = self.svc.get_case(self.case.id)
        if case.status == CaseStatus.PENDING_ASSIGNMENT:
            self.svc.assign_mediator(self.case.id, self.mediator)
        return self.svc.get_case(self.case.id)

    def _to_proposal_pending(self):
        self._to_assigned()
        return self.svc.propose_solution(self.case.id, "李师傅三日内清理楼道",
                                         self.mediator)

    def _to_agreed(self):
        proposal = self._to_proposal_pending()
        self.svc.confirm_proposal(self.case.id, proposal.id, self.alice.id)
        self.svc.confirm_proposal(self.case.id, proposal.id, self.bob.id)
        return proposal


class TestStateMachine(BaseCase):
    def test_full_happy_path_to_close(self):
        self.assertEqual(self.case.status, CaseStatus.PENDING_ASSIGNMENT)

        case = self._to_assigned()
        self.assertEqual(case.status, CaseStatus.ASSIGNED)
        self.assertEqual(case.mediator_id, self.mediator)

        proposal = self.svc.propose_solution(self.case.id, "三日内清理",
                                             self.mediator)
        self.assertEqual(self.svc.get_case(self.case.id).status,
                         CaseStatus.PROPOSAL_PENDING)

        self.svc.confirm_proposal(self.case.id, proposal.id, self.alice.id)
        # 仅一方确认，状态不变
        self.assertEqual(self.svc.get_case(self.case.id).status,
                         CaseStatus.PROPOSAL_PENDING)

        self.svc.confirm_proposal(self.case.id, proposal.id, self.bob.id)
        case = self.svc.get_case(self.case.id)
        self.assertEqual(case.status, CaseStatus.AGREED)

        self.svc.close_case(self.case.id, self.mediator, "双方已履行")
        case = self.svc.get_case(self.case.id)
        self.assertEqual(case.status, CaseStatus.CLOSED)
        self.assertEqual(case.resolution, Resolution.AGREEMENT)
        self.assertIsNotNone(case.closed_at)

    def test_illegal_transitions_rejected(self):
        # 未分派不能提方案
        with self.assertRaises(StateTransitionError):
            self.svc.propose_solution(self.case.id, "方案", self.mediator)
        # 未分派不能暂缓
        with self.assertRaises(StateTransitionError):
            self.svc.suspend_case(self.case.id, self.alice.id)
        # 分派后不能重复分派
        self._to_assigned()
        with self.assertRaises(StateTransitionError):
            self.svc.assign_mediator(self.case.id, "mediator-002")
        # 调解中不能直接结案
        with self.assertRaises(StateTransitionError):
            self.svc.close_case(self.case.id, self.mediator)

    def test_closed_is_terminal(self):
        proposal = self._to_agreed()
        self.svc.close_case(self.case.id, self.mediator)
        with self.assertRaises(StateTransitionError):
            self.svc.suspend_case(self.case.id, self.mediator)
        with self.assertRaises(StateTransitionError):
            self.svc.propose_solution(self.case.id, "新方案", self.mediator)
        with self.assertRaises(StateTransitionError):
            self.svc.submit_evidence(self.case.id, self.alice.id, "照片")
        with self.assertRaises(StateTransitionError):
            self.svc.close_case(self.case.id, self.mediator)

    def test_reject_returns_to_assigned_and_can_repropose(self):
        proposal = self._to_proposal_pending()
        self.svc.confirm_proposal(self.case.id, proposal.id, self.alice.id)
        self.svc.reject_proposal(self.case.id, proposal.id, self.bob.id,
                                 "期限太短")
        case = self.svc.get_case(self.case.id)
        self.assertEqual(case.status, CaseStatus.ASSIGNED)
        rejected = self.svc.get_case_result(self.case.id)["latest_proposal"]
        self.assertEqual(rejected["status"], ProposalStatus.REJECTED.value)

        # 已拒绝的方案不能再确认
        with self.assertRaises(StateTransitionError):
            self.svc.confirm_proposal(self.case.id, proposal.id, self.bob.id)
        # 可以提出新方案并走完流程
        proposal2 = self.svc.propose_solution(self.case.id, "七日内清理",
                                              self.mediator)
        self.svc.confirm_proposal(self.case.id, proposal2.id, self.alice.id)
        self.svc.confirm_proposal(self.case.id, proposal2.id, self.bob.id)
        self.assertEqual(self.svc.get_case(self.case.id).status,
                         CaseStatus.AGREED)

    def test_confirmed_party_cannot_switch_to_reject(self):
        proposal = self._to_proposal_pending()
        self.svc.confirm_proposal(self.case.id, proposal.id, self.alice.id)
        with self.assertRaises(ValidationError):
            self.svc.reject_proposal(self.case.id, proposal.id, self.alice.id)

    def test_suspend_and_resume_restores_previous_status(self):
        # 从“已分派”暂缓 → 恢复到“已分派”
        self._to_assigned()
        self.svc.suspend_case(self.case.id, self.alice.id, "张阿姨出差一周")
        case = self.svc.get_case(self.case.id)
        self.assertEqual(case.status, CaseStatus.SUSPENDED)
        self.assertEqual(case.status_before_suspend, CaseStatus.ASSIGNED)
        self.svc.resume_case(self.case.id, self.mediator)
        self.assertEqual(self.svc.get_case(self.case.id).status,
                         CaseStatus.ASSIGNED)

        # 从“方案待确认”暂缓 → 恢复到“方案待确认”
        self._to_proposal_pending()
        self.svc.suspend_case(self.case.id, self.mediator, "节假日暂停")
        self.assertEqual(self.svc.get_case(self.case.id).status_before_suspend,
                         CaseStatus.PROPOSAL_PENDING)
        self.svc.resume_case(self.case.id, self.mediator)
        self.assertEqual(self.svc.get_case(self.case.id).status,
                         CaseStatus.PROPOSAL_PENDING)

    def test_actions_blocked_while_suspended(self):
        self._to_assigned()
        self.svc.suspend_case(self.case.id, self.mediator)
        with self.assertRaises(StateTransitionError):
            self.svc.propose_solution(self.case.id, "方案", self.mediator)
        with self.assertRaises(StateTransitionError):
            self.svc.suspend_case(self.case.id, self.mediator)

    def test_close_from_suspended_is_terminated(self):
        self._to_assigned()
        self.svc.suspend_case(self.case.id, self.bob.id, "双方拒绝继续")
        self.svc.close_case(self.case.id, self.mediator, "调解终止")
        case = self.svc.get_case(self.case.id)
        self.assertEqual(case.status, CaseStatus.CLOSED)
        self.assertEqual(case.resolution, Resolution.TERMINATED)

    def test_permission_checks(self):
        self._to_assigned()
        # 非本案调解员不能提方案
        with self.assertRaises(AuthorizationError):
            self.svc.propose_solution(self.case.id, "方案", "mediator-999")
        # 非当事人不能确认方案
        proposal = self.svc.propose_solution(self.case.id, "方案", self.mediator)
        with self.assertRaises(AuthorizationError):
            self.svc.confirm_proposal(self.case.id, proposal.id, "outsider")
        # 当事人不能结案、不能恢复暂缓
        with self.assertRaises(AuthorizationError):
            self.svc.close_case(self.case.id, self.alice.id)
        self.svc.suspend_case(self.case.id, self.alice.id)
        with self.assertRaises(AuthorizationError):
            self.svc.resume_case(self.case.id, self.bob.id)

    def test_create_case_validation(self):
        with self.assertRaises(ValidationError):
            self.svc.create_case("", "desc", "cat", self.alice.id, self.bob.id)
        with self.assertRaises(ValidationError):
            self.svc.create_case("t", "d", "c", self.alice.id, self.alice.id)
        with self.assertRaises(NotFoundError):
            self.svc.create_case("t", "d", "c", self.alice.id, "ghost")


class TestEvidence(BaseCase):
    def setUp(self):
        super().setUp()
        self._to_assigned()

    def test_duplicate_upload_creates_no_copy(self):
        ev1, created1 = self.svc.submit_evidence(
            self.case.id, self.alice.id, "楼道照片",
            {"拍摄位置": "3栋2单元"})
        ev2, created2 = self.svc.submit_evidence(
            self.case.id, self.alice.id, "楼道照片",
            {"拍摄位置": "3栋2单元"})
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(ev1.id, ev2.id)
        listed = self.svc.list_evidence(self.case.id, self.mediator)
        self.assertEqual(len(listed), 1)
        # 重复提交在时间线留痕但不产生新证据
        types = [e.event_type for e in self.svc.get_timeline(self.case.id)]
        self.assertIn(EventType.EVIDENCE_RESUBMITTED, types)

    def test_different_content_creates_new_record(self):
        self.svc.submit_evidence(self.case.id, self.alice.id, "照片一")
        self.svc.submit_evidence(self.case.id, self.alice.id, "照片二")
        # 不同提交者的相同内容也算不同证据
        self.svc.submit_evidence(self.case.id, self.bob.id, "照片一")
        self.assertEqual(len(self.svc.list_evidence(self.case.id, self.mediator)), 3)

    def test_withdraw_preserves_original_and_timestamp(self):
        ev, _ = self.svc.submit_evidence(self.case.id, self.alice.id,
                                         "原始聊天记录", {"电话": "13800000001"})
        original_content = ev.content
        original_time = ev.submitted_at
        self.clock.advance(days=1)
        withdrawn = self.svc.withdraw_evidence(self.case.id, ev.id,
                                               self.alice.id, "误传")
        self.assertEqual(withdrawn.status, EvidenceStatus.WITHDRAWN)
        self.assertEqual(withdrawn.content, original_content)
        self.assertEqual(withdrawn.submitted_at, original_time)
        # 撤回后记录仍在列表中（留痕），原文可溯
        listed = self.svc.list_evidence(self.case.id, self.mediator)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].status, EvidenceStatus.WITHDRAWN)
        self.assertEqual(listed[0].content, original_content)
        # 撤回是终态
        with self.assertRaises(StateTransitionError):
            self.svc.verify_evidence(self.case.id, ev.id, self.mediator)

    def test_pending_verification_flow(self):
        ev, _ = self.svc.submit_evidence(self.case.id, self.bob.id, "缴费单据")
        ev = self.svc.mark_evidence_pending_verification(
            self.case.id, ev.id, self.mediator, "单据模糊")
        self.assertEqual(ev.status, EvidenceStatus.PENDING_VERIFICATION)
        ev = self.svc.verify_evidence(self.case.id, ev.id, self.mediator)
        self.assertEqual(ev.status, EvidenceStatus.VERIFIED)
        # 提交时间始终保留
        self.assertTrue(ev.submitted_at)

    def test_evidence_status_requires_mediator(self):
        ev, _ = self.svc.submit_evidence(self.case.id, self.alice.id, "照片")
        with self.assertRaises(AuthorizationError):
            self.svc.mark_evidence_pending_verification(
                self.case.id, ev.id, self.bob.id)
        with self.assertRaises(AuthorizationError):
            self.svc.verify_evidence(self.case.id, ev.id, self.alice.id)

    def test_withdraw_permission(self):
        ev, _ = self.svc.submit_evidence(self.case.id, self.alice.id, "照片")
        # 另一方当事人不能撤回他人证据
        with self.assertRaises(AuthorizationError):
            self.svc.withdraw_evidence(self.case.id, ev.id, self.bob.id)
        # 调解员可以撤回
        self.svc.withdraw_evidence(self.case.id, ev.id, self.mediator)

    def test_submit_evidence_validation(self):
        with self.assertRaises(ValidationError):
            self.svc.submit_evidence(self.case.id, self.alice.id, "  ")
        with self.assertRaises(AuthorizationError):
            self.svc.submit_evidence(self.case.id, "outsider", "照片")


class TestPrivacyAndSharing(BaseCase):
    def setUp(self):
        super().setUp()
        self._to_assigned()
        self.ev, _ = self.svc.submit_evidence(
            self.case.id, self.alice.id, "楼道照片",
            {"手机号": "13800000001", "门牌号": "302"})

    def test_other_party_sees_redacted_by_default(self):
        view = self.svc.get_evidence(self.case.id, self.ev.id, self.bob.id)
        self.assertTrue(view.private_fields_redacted)
        self.assertEqual(view.private_fields, {})
        # 公开内容仍可见
        self.assertEqual(view.content, "楼道照片")

    def test_submitter_and_mediator_see_full_fields(self):
        view_a = self.svc.get_evidence(self.case.id, self.ev.id, self.alice.id)
        self.assertEqual(view_a.private_fields["手机号"], "13800000001")
        view_m = self.svc.get_evidence(self.case.id, self.ev.id, self.mediator)
        self.assertFalse(view_m.private_fields_redacted)

    def test_one_way_grant_is_not_enough(self):
        # 仅甲方授权给乙方，未经“双方”授权，乙方仍不可见
        self.svc.grant_sharing(self.case.id, self.alice.id, self.bob.id)
        view = self.svc.get_evidence(self.case.id, self.ev.id, self.bob.id)
        self.assertTrue(view.private_fields_redacted)

    def test_mutual_grants_unlock_private_fields(self):
        self.svc.grant_sharing(self.case.id, self.alice.id, self.bob.id)
        self.svc.grant_sharing(self.case.id, self.bob.id, self.alice.id)
        view = self.svc.get_evidence(self.case.id, self.ev.id, self.bob.id)
        self.assertFalse(view.private_fields_redacted)
        self.assertEqual(view.private_fields["门牌号"], "302")

    def test_revoke_hides_fields_again(self):
        g1 = self.svc.grant_sharing(self.case.id, self.alice.id, self.bob.id)
        self.svc.grant_sharing(self.case.id, self.bob.id, self.alice.id)
        self.assertTrue(self.svc.can_view_private_fields(
            self.case.id, self.ev.id, self.bob.id))
        self.svc.revoke_sharing(self.case.id, g1.id, self.alice.id)
        self.assertFalse(self.svc.can_view_private_fields(
            self.case.id, self.ev.id, self.bob.id))

    def test_expired_grant_not_effective(self):
        expires = self.clock.t + timedelta(hours=1)
        self.svc.grant_sharing(self.case.id, self.alice.id, self.bob.id,
                               expires_at=expires)
        self.svc.grant_sharing(self.case.id, self.bob.id, self.alice.id,
                               expires_at=expires)
        self.assertTrue(self.svc.can_view_private_fields(
            self.case.id, self.ev.id, self.bob.id))
        self.clock.advance(hours=2)
        self.assertFalse(self.svc.can_view_private_fields(
            self.case.id, self.ev.id, self.bob.id))

    def test_evidence_scoped_grant_only_covers_that_evidence(self):
        ev2, _ = self.svc.submit_evidence(self.case.id, self.alice.id,
                                          "第二份证据", {"门牌号": "302"})
        self.svc.grant_sharing(self.case.id, self.alice.id, self.bob.id,
                               evidence_id=self.ev.id)
        self.svc.grant_sharing(self.case.id, self.bob.id, self.alice.id,
                               evidence_id=self.ev.id)
        self.assertTrue(self.svc.can_view_private_fields(
            self.case.id, self.ev.id, self.bob.id))
        self.assertFalse(self.svc.can_view_private_fields(
            self.case.id, ev2.id, self.bob.id))

    def test_grant_validation_and_idempotency(self):
        with self.assertRaises(ValidationError):
            self.svc.grant_sharing(self.case.id, self.alice.id, self.alice.id)
        with self.assertRaises(AuthorizationError):
            self.svc.grant_sharing(self.case.id, self.alice.id, "outsider")
        g1 = self.svc.grant_sharing(self.case.id, self.alice.id, self.bob.id)
        g2 = self.svc.grant_sharing(self.case.id, self.alice.id, self.bob.id)
        self.assertEqual(g1.id, g2.id)  # 幂等，不重复授权

    def test_revoke_twice_rejected(self):
        grant = self.svc.grant_sharing(self.case.id, self.alice.id, self.bob.id)
        self.svc.revoke_sharing(self.case.id, grant.id, self.alice.id)
        with self.assertRaises(StateTransitionError):
            self.svc.revoke_sharing(self.case.id, grant.id, self.alice.id)

    def test_unrelated_user_cannot_view_evidence(self):
        with self.assertRaises(AuthorizationError):
            self.svc.get_evidence(self.case.id, self.ev.id, "outsider")
        with self.assertRaises(AuthorizationError):
            self.svc.list_evidence(self.case.id, "outsider")


class TestMinutes(BaseCase):
    def setUp(self):
        super().setUp()
        self._to_assigned()

    def test_create_and_update_increments_version(self):
        minute = self.svc.create_minute(self.case.id, "第一次会谈",
                                        "双方陈述诉求", self.mediator)
        self.assertEqual(minute.version, 1)
        updated = self.svc.update_minute(self.case.id, minute.id,
                                         "双方陈述诉求并确认争议焦点",
                                         base_version=1,
                                         edited_by=self.mediator)
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.content, "双方陈述诉求并确认争议焦点")

    def test_stale_version_raises_conflict(self):
        minute = self.svc.create_minute(self.case.id, "会谈", "v1", self.mediator)
        self.svc.update_minute(self.case.id, minute.id, "v2",
                               base_version=1, edited_by=self.mediator)
        # 另一方仍基于旧版本编辑 → 冲突
        with self.assertRaises(VersionConflictError) as ctx:
            self.svc.update_minute(self.case.id, minute.id, "覆盖",
                                   base_version=1, edited_by=self.alice.id)
        self.assertEqual(ctx.exception.expected_version, 1)
        self.assertEqual(ctx.exception.actual_version, 2)
        # 内容未被覆盖
        self.assertEqual(self.svc.get_minute(self.case.id, minute.id).content,
                         "v2")

    def test_history_preserves_all_versions(self):
        minute = self.svc.create_minute(self.case.id, "会谈", "第一版",
                                        self.mediator)
        self.svc.update_minute(self.case.id, minute.id, "第二版",
                               base_version=1, edited_by=self.alice.id)
        self.svc.update_minute(self.case.id, minute.id, "第三版",
                               base_version=2, edited_by=self.mediator)
        history = self.svc.minute_history(self.case.id, minute.id)
        self.assertEqual([h.version for h in history], [1, 2, 3])
        self.assertEqual([h.content for h in history],
                         ["第一版", "第二版", "第三版"])

    def test_minute_access_and_closed_case(self):
        with self.assertRaises(AuthorizationError):
            self.svc.create_minute(self.case.id, "t", "c", "outsider")
        minute = self.svc.create_minute(self.case.id, "会谈", "内容",
                                        self.mediator)
        self._to_agreed()
        self.svc.close_case(self.case.id, self.mediator)
        with self.assertRaises(StateTransitionError):
            self.svc.update_minute(self.case.id, minute.id, "改动",
                                   base_version=1, edited_by=self.mediator)


class TestTimelineAndResult(BaseCase):
    def test_timeline_records_key_events_in_order(self):
        self._to_assigned()
        self.svc.add_issue(self.case.id, "楼道堆物", "占用消防通道",
                           self.alice.id)
        self.svc.submit_evidence(self.case.id, self.alice.id, "照片")
        self.svc.create_minute(self.case.id, "首次会谈", "记录", self.mediator)
        proposal = self.svc.propose_solution(self.case.id, "三日内清理",
                                             self.mediator)
        self.svc.confirm_proposal(self.case.id, proposal.id, self.alice.id)
        self.svc.confirm_proposal(self.case.id, proposal.id, self.bob.id)
        self.svc.close_case(self.case.id, self.mediator)

        events = self.svc.get_timeline(self.case.id)
        types = [e.event_type for e in events]
        self.assertEqual(types, [
            EventType.CASE_CREATED,
            EventType.MEDIATOR_ASSIGNED,
            EventType.ISSUE_ADDED,
            EventType.EVIDENCE_SUBMITTED,
            EventType.MINUTE_CREATED,
            EventType.PROPOSAL_SUBMITTED,
            EventType.PROPOSAL_CONFIRMED,
            EventType.PROPOSAL_CONFIRMED,
            EventType.PROPOSAL_AGREED,
            EventType.CASE_CLOSED,
        ])

    def test_timeline_access_control(self):
        with self.assertRaises(AuthorizationError):
            self.svc.get_timeline(self.case.id, viewer_id="outsider")
        # 当事人与调解员可见
        self._to_assigned()
        self.assertTrue(self.svc.get_timeline(self.case.id, self.alice.id))
        self.assertTrue(self.svc.get_timeline(self.case.id, self.mediator))

    def test_case_result_after_close(self):
        self._to_assigned()
        self.svc.add_issue(self.case.id, "堆物", "desc", self.alice.id)
        ev1, _ = self.svc.submit_evidence(self.case.id, self.alice.id, "照片")
        self.svc.submit_evidence(self.case.id, self.bob.id, "说明")
        self.svc.withdraw_evidence(self.case.id, ev1.id, self.alice.id)
        proposal = self._to_proposal_pending()
        self.svc.confirm_proposal(self.case.id, proposal.id, self.alice.id)
        self.svc.confirm_proposal(self.case.id, proposal.id, self.bob.id)
        self.svc.close_case(self.case.id, self.mediator, "履行完毕")

        result = self.svc.get_case_result(self.case.id, viewer_id=self.bob.id)
        self.assertEqual(result["status"], CaseStatus.CLOSED.value)
        self.assertEqual(result["resolution"], Resolution.AGREEMENT.value)
        self.assertEqual(result["close_reason"], "履行完毕")
        self.assertEqual(result["evidence_summary"]["total"], 2)
        self.assertEqual(result["evidence_summary"]
                         [EvidenceStatus.WITHDRAWN.value], 1)
        self.assertEqual(result["evidence_summary"]
                         [EvidenceStatus.SUBMITTED.value], 1)
        self.assertEqual(result["latest_proposal"]["status"],
                         ProposalStatus.AGREED.value)
        self.assertEqual(len(result["issues"]), 1)
        # 结果中不泄露隐私字段
        self.assertNotIn("private_fields", str(result))

    def test_result_access_control(self):
        with self.assertRaises(AuthorizationError):
            self.svc.get_case_result(self.case.id, viewer_id="outsider")


class TestNextActionsAndOverdue(BaseCase):
    def test_next_actions_follow_status(self):
        info = self.svc.get_next_actions(self.case.id)
        self.assertEqual(info["next_actions"][0]["action"], "assign_mediator")
        self.assertFalse(info["overdue"])

        self._to_assigned()
        info = self.svc.get_next_actions(self.case.id)
        actions = {a["action"] for a in info["next_actions"]}
        self.assertIn("propose_solution", actions)

        proposal = self.svc.propose_solution(self.case.id, "方案", self.mediator)
        self.svc.confirm_proposal(self.case.id, proposal.id, self.alice.id)
        info = self.svc.get_next_actions(self.case.id)
        self.assertTrue(any("李师傅" in a["description"]
                            for a in info["next_actions"]))

        self.svc.confirm_proposal(self.case.id, proposal.id, self.bob.id)
        info = self.svc.get_next_actions(self.case.id)
        self.assertEqual(info["next_actions"][0]["action"], "close_case")

    def test_overdue_reasons_after_sla(self):
        # 待分派超过 2 天
        self.clock.advance(days=3)
        info = self.svc.get_next_actions(self.case.id)
        self.assertTrue(info["overdue"])
        self.assertIn("尚未分派调解员", info["overdue_reasons"][0])

        # 方案待确认超过 5 天，指出未确认方
        self._to_assigned()
        proposal = self.svc.propose_solution(self.case.id, "方案", self.mediator)
        self.svc.confirm_proposal(self.case.id, proposal.id, self.alice.id)
        self.clock.advance(days=6)
        info = self.svc.get_next_actions(self.case.id)
        self.assertTrue(info["overdue"])
        self.assertIn("李师傅", info["overdue_reasons"][0])
        self.assertNotIn("张阿姨", info["overdue_reasons"][0])

    def test_not_overdue_within_sla(self):
        self.clock.advance(days=1)
        info = self.svc.get_next_actions(self.case.id)
        self.assertFalse(info["overdue"])
        self.assertEqual(info["overdue_reasons"], [])

    def test_suspension_overdue(self):
        self._to_assigned()
        self.svc.suspend_case(self.case.id, self.mediator, "暂停")
        self.clock.advance(days=31)
        info = self.svc.get_next_actions(self.case.id)
        self.assertTrue(info["overdue"])
        self.assertIn("暂缓", info["overdue_reasons"][0])

    def test_mediator_dashboard(self):
        self._to_assigned()
        other = self.svc.create_case("噪音争议", "desc", "邻里",
                                     self.alice.id, self.bob.id)
        self.svc.assign_mediator(other.id, self.mediator)
        dashboard = self.svc.mediator_dashboard(self.mediator)
        self.assertEqual(len(dashboard), 2)
        self.assertTrue(all("next_actions" in d for d in dashboard))
        # 结案后不再出现在工作台
        self.svc.suspend_case(other.id, self.mediator)
        self.svc.close_case(other.id, self.mediator)
        dashboard = self.svc.mediator_dashboard(self.mediator)
        self.assertEqual(len(dashboard), 1)


class TestPersistenceAcrossRestart(unittest.TestCase):
    """重启后共享授权与会谈版本保持一致。"""

    def test_state_survives_restart(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "mediation.db")

            svc1 = MediationService(db_path, now_fn=clock)
            alice = svc1.register_party("张阿姨", "13800000001")
            bob = svc1.register_party("李师傅", "13800000002")
            case = svc1.create_case("堆物争议", "desc", "公共空间使用",
                                    alice.id, bob.id)
            svc1.assign_mediator(case.id, "mediator-001")
            ev, _ = svc1.submit_evidence(case.id, alice.id, "照片",
                                         {"手机号": "13800000001"})
            svc1.mark_evidence_pending_verification(case.id, ev.id,
                                                    "mediator-001")
            minute = svc1.create_minute(case.id, "会谈", "第一版",
                                        "mediator-001")
            svc1.update_minute(case.id, minute.id, "第二版",
                               base_version=1, edited_by="mediator-001")
            svc1.grant_sharing(case.id, alice.id, bob.id)
            svc1.grant_sharing(case.id, bob.id, alice.id)
            svc1.close()

            # 模拟重启：同一数据库文件重新实例化
            svc2 = MediationService(db_path, now_fn=clock)
            try:
                # 授权仍然有效：双方互相授权 → 隐私字段可见
                self.assertTrue(svc2.can_view_private_fields(
                    case.id, ev.id, bob.id))
                view = svc2.get_evidence(case.id, ev.id, bob.id)
                self.assertEqual(view.private_fields["手机号"], "13800000001")
                # 证据状态保留
                self.assertEqual(view.status,
                                 EvidenceStatus.PENDING_VERIFICATION)
                # 纪要版本保留，基于旧版本编辑仍报冲突
                restored = svc2.get_minute(case.id, minute.id)
                self.assertEqual(restored.version, 2)
                self.assertEqual(restored.content, "第二版")
                with self.assertRaises(VersionConflictError):
                    svc2.update_minute(case.id, minute.id, "覆盖",
                                       base_version=1, edited_by="mediator-001")
                svc2.update_minute(case.id, minute.id, "第三版",
                                   base_version=2, edited_by="mediator-001")
                self.assertEqual(
                    len(svc2.minute_history(case.id, minute.id)), 3)
                # 案件状态与时间线保留
                self.assertEqual(svc2.get_case(case.id).status,
                                 CaseStatus.ASSIGNED)
                self.assertTrue(svc2.get_timeline(case.id))
                # 去重在重启后依然生效
                _, created = svc2.submit_evidence(case.id, alice.id, "照片",
                                                  {"手机号": "13800000001"})
                self.assertFalse(created)
            finally:
                svc2.close()


if __name__ == "__main__":
    unittest.main()
