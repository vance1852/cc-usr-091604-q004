# 裁判指派平台

城市篮球联赛裁判指派服务。报名截止后发现裁判与参赛队存在培训/亲属关系、
临时指派导致同一裁判连续跨城赶场——本平台围绕这两个痛点设计：

- 维护裁判档案：**等级、可执法项目、执法区域、不可用时段、主动申报的利益冲突**；
- 按**比赛级别、距离、连续工作时长、近期场次**生成候选名单；
- 主任**锁定候选 → 确认指派**，裁判**接受/拒绝均须填写理由**，全程保留时间线；
- 新增冲突申报后**未开赛场次自动重新评估**，**已结束比赛的历史指派不可覆盖**；
- 提供**批量指派、替补接管、冲突解释、按日期导出**接口，并保证**不重复占用同一裁判**
  （含跨城赶场缓冲与当日连续执法上限）。

## 运行

```bash
python3 -m unittest discover -s tests -v   # 41 个测试
python3 examples/demo.py                    # 端到端演示
```

纯标准库实现（Python 3.11+），无需安装依赖。

## 目录

- `app/models.py`：领域模型与枚举（裁判等级、比赛级别、指派状态机、候选报告）
- `app/service.py`：`AssignmentService` 全部业务接口与规则引擎
- `app/repository.py`：线程安全存储（事务锁 + 比赛版本号乐观锁）
- `app/timeutil.py`：UTC 归一化、跨天时段展开、大圆距离
- `app/errors.py`：带稳定 `code` 的领域异常，可直接映射为接口错误响应
- `app/testing.py`：可注入时钟等测试辅助
- `app/officials.py`：早期起点的兼容导出
- `tests/`：候选、指派流程、冲突、并发、时区、导出六组测试

## 接口一览（`AssignmentService`）

| 接口 | 说明 |
| --- | --- |
| `add_referee / add_match / add_unavailability / add_daily_unavailability` | 维护裁判档案、比赛与不可用时段（支持跨天） |
| `declare_conflict / deactivate_conflict` | 申报/撤销利益冲突；申报即重估未开赛场次 |
| `generate_candidates(match_id)` | 候选名单 + 排除原因；空结果区分 `NO_QUALIFIED_CANDIDATES` 与 `ALL_EXCLUDED_BY_CONFLICT` |
| `explain(match_id, referee_id)` | 冲突/资格解释：每条原因带 `code`、`category`（qualification/availability/conflict）与中文说明 |
| `lock_candidates → confirm(expected_version=...)` | 锁定候选、按版本号确认；并发确认只有一方成功 |
| `respond(accept, reason)` | 裁判接受/拒绝，理由必填，写入时间线 |
| `revoke / reinstate` | 撤销未开赛指派（理由必填）；冲突解除后恢复 |
| `substitute(assignment_id, reason)` | 替补接管：原指派转 `SUPERSEDED`，新指派记录 `TAKEOVER` |
| `batch_assign(match_ids)` | 批量指派：按开赛时间排序逐场确认，同批内自动避开已占用裁判 |
| `timeline(assignment_id)` | 指派完整时间线（全局序号 + 时间 + 操作者 + 理由） |
| `export_by_date(day, tz, fmt="json"/"csv")` | 按指定时区的日期导出，含已结束比赛的历史指派 |

## 规则说明

**硬性排除**（进入 `excluded`，附 `code` 与 `category`）：

- `LEVEL_TOO_LOW`：裁判等级低于比赛级别要求（小组赛≥国家二级 … 决赛=国际级）；
- `EVENT_NOT_COVERED` / `REGION_NOT_COVERED`：项目、区域未覆盖；
- `UNAVAILABLE`：与申报的不可用时段重叠（绝对 UTC 区间，天然支持跨天）；
- `ALREADY_BOOKED`：同时段已有指派，禁止重复占用；
- `INSUFFICIENT_TRAVEL_GAP`：两场比赛间隔不足（同城 30 分钟；跨城按距离
  `max(120, 1.5×公里数)` 分钟），避免连续跨城赶场；
- `DAILY_LIMIT_EXCEEDED`：当日连续执法（间隔≤30 分钟串联）超过 240 分钟；
- `CONFLICT_OF_INTEREST`：与参赛队存在已申报的培训/亲属/任职/经济利益关系。

**打分排序**（越小越优先）：`距离km×1.0 + 近14天场次×25 + 当日已排分钟×0.5`，
打分明细随候选返回，便于向主任解释。

**状态机**：`LOCKED → CONFIRMED → ACCEPTED/DECLINED`，另有 `REVOKED`（撤销）、
`SUPERSEDED`（被替补）、`FLAGGED_CONFLICT`（冲突待复核）。
已结束比赛的指派一律拒绝修改（`MATCH_FINISHED`），历史不可覆盖。

## 可追溯性与并发

- 每次变更向指派时间线追加事件（全局单调序号、注入时钟时间戳、操作者、理由）；
- 复合操作在仓储事务内串行执行；`confirm` 携带锁定时的比赛版本号，
  并发确认/锁定后变更（含新冲突申报）都会使版本失效（`VERSION_CONFLICT`）；
- 测试覆盖：并发确认只成功一次、跨天时段、跨时区重叠、撤销后档期释放、
  已结束比赛历史不被改写（见 `tests/`）。
