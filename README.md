# 社区矛盾调解协作

该项目服务于网格员和社区管理工作，负责社区矛盾调解协作相关信息的规范化处理与留痕。

运行环境：Python 3.11，仅依赖标准库。代码位于 `src` 目录，测试位于 `tests` 目录。

## 功能概览

将当事人、议题、证据与会谈纪要组织为**案件**，覆盖调解全流程：

- 立案、登记双方当事人、分派调解员、开始调解
- 证据提交（自动去重）、核验、撤回（原文与提交时间始终保留）
- 会谈纪要记录与编辑（乐观锁版本控制，冲突拒绝写入）
- 提出调解方案、双方确认（任一拒绝则回到调解中）、暂缓 / 恢复、结案
- 隐私字段授权共享：未经当事人授权，另一方看不到隐私内容
- 案件时间线、调解员工作台（下一步动作 + 逾期原因）、结果查询

全部状态持久化于 SQLite，**重启后共享授权与会谈纪要版本保持一致**。

## 案件状态机

```
INTAKE ──分派调解员──▶ ASSIGNED ──开始调解──▶ IN_MEDIATION ──提出方案──▶ PROPOSAL_REVIEW
                                                                                │
              双方均确认 ◀───────────────────────┬──────────── 任一方拒绝 ────────┘
                  │                              ▼
                  ▼                        回到 IN_MEDIATION
               RESOLVED ──结案──▶ CLOSED（终态）

ASSIGNED / IN_MEDIATION / PROPOSAL_REVIEW ──暂缓──▶ SUSPENDED ──恢复──▶ 暂缓前状态
IN_MEDIATION / SUSPENDED ──结案（须说明原因）──▶ CLOSED
```

状态只能沿上述路径变化，非法流转抛出 `InvalidStateTransition`。

## 快速开始

```python
from src import MediationService

svc = MediationService("mediation.db")

# 立案 + 双方 + 分派
case = svc.create_case("公共露台使用纠纷", "两户居民就露台使用时间产生争议")
pa = svc.add_party(case["id"], "张桂兰", contact="13800000001")
pb = svc.add_party(case["id"], "李国强", contact="13800000002")
svc.assign_mediator(case["id"], "med-1")
svc.start_mediation(case["id"], "med-1")

# 证据：private_fields 仅提交方与调解员可见
ev = svc.submit_evidence(case["id"], pa["id"], "聊天记录", "微信沟通记录",
                         private_fields={"phone": "13800000001"})
svc.verify_evidence(case["id"], ev["id"], "med-1")

# 授权共享后另一方才能看到隐私字段（可撤销）
svc.grant_share(case["id"], pa["id"])                 # 案件级
svc.grant_share(case["id"], pa["id"], evidence_id=ev["id"])  # 单条证据级

# 会谈纪要：编辑必须携带当前版本号，冲突抛 VersionConflictError
m = svc.record_minutes(case["id"], 1, "首次会谈纪要", "med-1")
svc.update_minutes(case["id"], m["id"], expected_version=1,
                   content="首次会谈（修订）", editor="med-1")

# 方案与双方确认
prop = svc.propose_settlement(case["id"], "单双周轮换使用露台", "med-1")
svc.confirm_proposal(case["id"], prop["id"], pa["id"], accept=True)
svc.confirm_proposal(case["id"], prop["id"], pb["id"], accept=True)  # 双方确认 -> RESOLVED
svc.close_case(case["id"], "med-1")

# 查询接口
svc.get_timeline(case["id"])          # 案件时间线
svc.get_next_actions(case["id"])      # 调解员下一步动作
svc.get_overdue_reasons(case["id"])   # 逾期原因（无逾期返回 []）
svc.get_case_result(case["id"])       # 结果查询：方案、确认记录、证据汇总
svc.view_evidence(case["id"], pb["id"])  # 按查看者身份返回（隐私字段按授权遮蔽）
```

## 关键设计

| 需求 | 实现 |
| --- | --- |
| 重复上传同一证据不产生副本 | 按 `案件 + 提交人 + 内容 SHA-256` 唯一约束去重，返回原记录且 `deduplicated=True`，不重复写时间线 |
| 撤回 / 待核验保留原文 | 撤回、核验只是 `status` 标记，`content` 与 `submitted_at` 永不修改 |
| 隐私字段未授权不展示 | `view_evidence` / `view_case` 按查看者身份过滤；另一方需持有有效 `share_grants`（案件级或证据级），撤销后立即恢复遮蔽 |
| 纪要版本冲突 | `update_minutes` 校验 `expected_version`，不一致抛 `VersionConflictError`；全部历史版本存于 `minute_versions` |
| 状态按规定路径变化 | `ALLOWED_TRANSITIONS` 状态机统一校验；暂缓记录 `previous_status`，恢复时原路返回 |
| 重启一致性 | 全部状态（含授权、纪要版本、时间线）存 SQLite，重开同一数据库文件即恢复 |

## 角色与权限

- **调解员**：开始/暂缓/恢复/结案、记录与编辑纪要、提出方案、核验证据、查看全部内容
- **当事人**：提交/撤回本方证据、对方案表态、授权/撤销本方隐私共享
- **系统/管理员**：立案、登记当事人、分派调解员

## 运行测试

```bash
python3 -m unittest discover -s tests -t . -v
```

## 目录结构

```
src/
  __init__.py   # 包入口与导出
  models.py     # 案件状态机、证据/方案状态、时限配置
  errors.py     # 领域异常（状态流转/权限/版本冲突等）
  storage.py    # SQLite 建表与连接
  service.py    # MediationService 领域服务
tests/
  test_mediation_service.py
```
