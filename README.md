# 社区矛盾调解协作

该项目服务于网格员和社区管理工作，负责社区矛盾调解协作相关信息的规范化处理与留痕。

运行环境：Python 3.11（仅使用标准库，无外部依赖）。代码位于 `src` 目录，配置与数据文件应按部署环境提供。

## 功能概览

`MediationService`（`src/service.py`）把当事人、议题、证据和会谈纪要组织成案件，提供：

- **案件状态机**：`待分派 → 已分派 → 方案待确认 → 已达成一致 → 已结案`；
  `已分派 / 方案待确认` 可暂缓，恢复时回到暂缓前状态；暂缓中可终止结案。
  状态只能沿规定路径变化，非法流转抛出 `StateTransitionError`。
- **证据管理**：提交、待核验、已核验、撤回。撤回/核验只改状态，
  原文与提交时间永久保留；同一提交者重复上传同一内容按哈希去重，不产生副本。
- **隐私保护**：证据可携带隐私字段（手机号、门牌等）。提交人本人与调解员始终可见；
  另一方只有在**双方互相授权**（`grant_sharing` 双向均有效、未撤销、未过期）后可见，
  否则视图中隐私字段被隐藏。
- **会谈纪要**：乐观锁版本控制——编辑必须基于当前版本，过期版本抛出
  `VersionConflictError`；全部历史版本留痕可查。
- **查询接口**：案件时间线（全部关键动作留痕）、结果查询（进展/结案汇总，
  不含隐私字段）、调解员下一步动作与逾期原因（各状态处理时限可配置）、调解员工作台。
- **持久化**：SQLite 写穿存储。用同一数据库文件重新实例化服务即可在重启后
  恢复全部状态，共享授权与会谈版本保持一致。

## 快速开始

```python
from src import MediationService

svc = MediationService("mediation.db")          # 文件路径即可持久化；":memory:" 为纯内存
alice = svc.register_party("张阿姨", "13800000001")
bob = svc.register_party("李师傅", "13800000002")

case = svc.create_case("楼道堆物争议", "占用公共空间", "公共空间使用",
                       alice.id, bob.id)
svc.assign_mediator(case.id, "mediator-001")

svc.add_issue(case.id, "楼道堆物", "占用消防通道", alice.id)
ev, created = svc.submit_evidence(case.id, alice.id, "现场照片",
                                  private_fields={"门牌号": "302"})

# 双方互相授权后，另一方才能看到隐私字段
svc.grant_sharing(case.id, alice.id, bob.id)
svc.grant_sharing(case.id, bob.id, alice.id)

minute = svc.create_minute(case.id, "首次会谈", "双方陈述诉求", "mediator-001")
svc.update_minute(case.id, minute.id, "补充争议焦点",
                  base_version=minute.version, edited_by="mediator-001")

proposal = svc.propose_solution(case.id, "三日内清理完毕", "mediator-001")
svc.confirm_proposal(case.id, proposal.id, alice.id)
svc.confirm_proposal(case.id, proposal.id, bob.id)   # 双方确认 → 已达成一致
svc.close_case(case.id, "mediator-001", "已履行")

svc.get_timeline(case.id)                  # 案件时间线
svc.get_case_result(case.id)               # 结果查询
svc.get_next_actions(case.id)              # 下一步动作与逾期原因
svc.mediator_dashboard("mediator-001")     # 调解员工作台
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```
