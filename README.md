# 城市篮球联赛裁判指派服务

在报名截止后仍可安全完成裁判指派：维护裁判等级、可执法项目、执法区域、不可用时段与主动申报的
利益冲突，按**比赛级别、距离、连续工作时长、近期场次**生成候选名单；支持锁定、并发确认、
裁判接受/拒绝、批量指派、替补接管、冲突解释与按日期导出。新增冲突申报会自动重评估所有
**未开赛**场次，而已开赛/已结束比赛的历史指派永不被覆盖。

仅依赖 Python 3.11 标准库（`zoneinfo` 提供时区，`wsgiref` 提供 HTTP）。

## 运行

```bash
# 测试
python -m unittest discover -s tests -v

# 启动 HTTP 服务（默认 127.0.0.1:8000）
python -m app.api
```

## 模块

| 文件 | 职责 |
| --- | --- |
| `app/models.py` | 领域模型：`Official` / `Team` / `Match` / `Unavailability` / `ConflictDeclaration` / `Assignment` 与状态机、不可变事件时间线 |
| `app/geo.py` | 城市坐标与 haversine 球面距离 |
| `app/policy.py` | 转场速度、休息、连续工时与近期场次阈值 |
| `app/eligibility.py` | 资格与排班规则引擎，产出 `candidates / excluded / disqualified` 三类结果 |
| `app/service.py` | 线程安全的应用服务与乐观并发控制（业务边界） |
| `app/store.py` | 内存存储 + 全局锁 |
| `app/api.py` | WSGI/JSON HTTP 接口 |
| `app/timeutils.py` | 带时区的时间解析与当地日期换算 |

## 指派状态机

```
candidate（候选名单，不落库）
   │ lock_candidate
   ▼
locked ──confirm──▶ confirmed ──respond(accept=true)──▶ accepted
   │                   │
   │                   └──respond(accept=false)────────▶ declined   （释放占用，等待替补）
   │
   ├──cancel──────────────────────────────────────────▶ cancelled
   │
   └──被替补接管 / 被新增冲突重评估作废 ───────────────▶ replaced / superseded
```

只有 `locked / confirmed / accepted` 会**占用**裁判，`declined / cancelled / replaced / superseded`
立即释放，因此同一裁判不会被重复占用。每次状态流转都向 `timeline` 追加一条带时间戳、操作者、
动作与理由的不可变事件。

## 候选结果如何区分“没人合格”和“被规则排除”

`GET /api/matches/{id}/candidates` 返回三段：

- `candidates`：通过全部硬性资格 **和** 排班/冲突规则的裁判（按距离→近期场次→等级排序）；
- `excluded`：硬性资格合格，但因利益冲突 / 不可用 / 时间占用 / 跨城转场不足 /
  连续工时超限 / 近期场次超限被排除，每条都带机器可读 `code` 与中文解释；
- `disqualified`：项目不符、等级不足、区域不覆盖、已停用等**硬性不合格**。

`summary.outcome` 取三值之一：

- `candidates_available`：存在合格候选；
- `all_excluded_by_rules`：没有合格候选，但裁判池里有人够硬资格、是被规则挡住的；
- `no_qualified_pool`：连硬资格都没人满足。

## 关键规则

- **比赛级别 ↔ 裁判等级**：职业需国家级、甲级需国家一级、乙级需国家二级、业余/青少年需国家三级及以上。
- **执法项目/区域**：不具备该项目、或执法区域（常驻城市 + 覆盖城市）不含比赛城市即硬性不合格。
- **跨城转场**：相邻两场间隔必须 ≥ 路程时间（haversine 距离 ÷ 平均时速）+ 基本休息时间，否则 `travel_gap`。
- **连续工作时长**：相邻两场间隔不超过 `block_join_minutes` 归为同一段连续工作，超过
  `max_consecutive_hours` 即 `consecutive_hours`。
- **近期场次**：滚动 `recent_window_hours` 窗口内含本场超过 `max_recent_games` 即 `recent_load`。
- **利益冲突 / 不可用**：主动申报，命中即排除；新增申报立即重评估未开赛场次。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| POST | `/api/teams` `/api/officials` `/api/matches` | 建档案/比赛（时间为 ISO8601，可带偏移量或用 `tz` 本地化） |
| POST | `/api/officials/{id}/unavailability` | 登记不可用时段（触发未开赛重评估） |
| POST | `/api/officials/{id}/conflicts` | 申报利益冲突（触发未开赛重评估） |
| GET | `/api/matches/{id}/candidates?limit=` | 候选名单 + 排除/不合格解释 |
| GET | `/api/matches/{id}/explain?official_id=` | 单裁判的利益冲突 / 排班冲突解释 |
| POST | `/api/assignments/lock` | 主任锁定候选 |
| POST | `/api/assignments/{id}/confirm` | 确认，可带 `expected_version` 做乐观并发 |
| POST | `/api/assignments/{id}/respond` | 裁判 `accept` + 必填 `reason` |
| POST | `/api/assignments/{id}/cancel` | 撤销（必填原因，仅限未开赛） |
| POST | `/api/assignments/batch` | 批量指派，原子提交（任一不满足整批回滚） |
| POST | `/api/matches/{id}/takeover` | 替补接管，旧指派转 `replaced` 并保留时间线 |
| POST | `/api/matches/{id}/start` `/finish` | 标记开赛/完赛（之后进入历史保护） |
| GET | `/api/assignments/{id}/timeline` | 完整状态时间线 |
| GET | `/api/export?date=YYYY-MM-DD&tz=Asia/Shanghai` | 按指定时区当地日期导出 |

错误响应统一为 `{ "error_code", "message", "reasons" }`：`not_qualified`（硬资格不足，HTTP 409）、
`excluded_by_rules`（被冲突/排班规则排除，409）、`state_conflict`（并发版本或非法状态流转，409）、
`historical_protected`（已开赛/已结束，409）、`not_found`（404）。

## 可追溯性与测试

`tests/` 覆盖：

- 资格/冲突/转场/连续工时/近期场次规则，以及“无人合格 vs 全员被规则排除”的区分；
- 锁定→确认→接受/拒绝（理由）→时间线；
- 新增冲突后未开赛场次重评估作废、已开赛/已结束历史指派受保护；
- 批量原子提交与批内防重复占用、替补接管；
- **并发确认**（多线程带相同 `expected_version` 同时确认，恰好一个成功）与并发锁定不重复占用；
- **跨午夜与跨时区**（上海 23:30 跨天、东京时区比赛）按当地日期导出；
- 撤销保护，以及全部接口的 WSGI 端到端 JSON 契约。
