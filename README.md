# 跨时区会议排期 API

基于 **Python + FastAPI + 标准库 `zoneinfo` + SQLite** 的会议排期服务，提供两类能力：

1. **可用时段聚合**：每位参与者按**本地星期与钟面时刻**填写每周重复的可用区间并给出 IANA
   时区（如 `Asia/Shanghai`），服务按具体日期应用夏令时（DST）规则，统一换算到 UTC，
   用**扫描线**算法计算所有满足最低出席人数与会议时长的候选时段。
2. **会议预约**：`POST /api/v1/reservations` 把预约原子化写入 SQLite，自动检测全部参与者
   的半开区间时间冲突，并用 `request_id` 提供幂等保证。

- 服务访问地址：<http://localhost:8000>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：`GET /health`

## 一条命令启动

```bash
docker compose up --build
```

服务在 **8000 端口**启动并可直接访问 <http://localhost:8000>。首次启动时会**自动创建
SQLite 数据库与表结构**（无需手工初始化），数据库文件位于命名卷 `reservation-data`
（容器内路径 `/data/reservations.db`），容器重建后预约数据仍然保留。

若宿主机 8000 端口被占用：

```bash
HOST_PORT=9000 docker compose up --build   # 访问 http://localhost:9000
```

后台运行与停止：

```bash
docker compose up -d --build
docker compose logs -f
docker compose down            # 停止并移除容器（保留数据卷）
docker compose down -v         # 连同预约数据卷一起删除
```

## 配置

| 配置项 | 位置 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `APP_PORT` | 容器环境变量 | `8000` | 容器内 uvicorn 监听端口 |
| `HOST_PORT` | 宿主机环境变量 / `.env` | `8000` | 映射到宿主机的端口 |
| `RESERVATION_DB_PATH` | 环境变量 / `.env` | 容器内 `/data/reservations.db`；本地运行时为仓库 `data/reservations.db` | SQLite 预约数据库路径，目录不存在会自动创建；启动时幂等建表 |
| `TZ` | 容器环境变量 | `UTC` | 容器系统时区（不影响按 IANA 时区进行的换算） |

可复制 `.env.example` 为 `.env` 后按需调整。构建时镜像内会安装 `tzdata`（IANA 时区
数据库），Python 的 `tzdata` pip 包也作为兜底依赖安装。

## 语义说明（可用时段聚合）

- **搜索区间**：`[start_date 00:00 UTC, end_date + 1 天 00:00 UTC)`，起止日期均含（以 UTC 日期为界）。
  这意味着 UTC+14 时区里落在本地周一凌晨的窗口，其 UTC 时刻可能处于周日、从而被裁剪在区间之外。
- **跨午夜窗口**：`end_local <= start_local` 时视为跨午夜，自动拆成“当日起点 → 次日终点”的半开区间后换算 UTC。
- **DST 处理**（按每个具体日期分别计算）：
  - **不存在的本地时刻**（春季跳变被跳过的钟面时间，如美东 2026-03-08 02:30）→ 返回 `400` 错误；
  - **歧义时刻**（秋季回退出现两次的钟面时间）→ 统一选择 `fold=0`，即 UTC 偏移量较大、较早出现的那一次。
- **候选段**：出席者集合保持恒定的最大区间；时长必须 `>= meeting_minutes`。
- **排序**：出席人数**降序**，人数相同时按 UTC 开始时间**升序**（稳定排序）。
- 同一参与者互相重叠的窗口会先按人合并，扫描线中一人在任一时刻至多计一份出席。

## 会议预约语义（SQLite）

- **半开区间冲突**：预约区间为 `[start_utc, end_utc)`。已有预约满足
  `existing.start < new.end AND new.start < existing.end` 时才与新人重叠；因此
  **首尾相接的相邻区间不冲突**（13:00–14:00 与 14:00–15:00 可连续预订）。
- **原子性**：幂等检查 → 全部参与者冲突检查 → 预约行与全部占用行写入在**单个
  `BEGIN IMMEDIATE` 事务**中完成。任一参与者冲突则整体回滚、返回 `409`，**不会留下
  部分占用写入**；写事务在数据库级互斥，并发请求同样保证原子性。
- **幂等**：参与者 ID 先**去重并按字典序排序**，时刻统一归一化到 UTC（`Z` 与
  `+00:00`、以及等价偏移量视为相同），得到规范化请求：
  - 相同 `request_id` + 相同规范化请求重试 → 返回**原预约**（HTTP `200`，
    `status="replayed"`），不新增记录；
  - 相同 `request_id` 但起止时刻或参与者不同 → 返回 `409`（幂等键冲突）。
- 首次创建成功返回 HTTP `201`，`status="created"`。
- 时间戳必须携带时区偏移（如 `...Z` 或 `+09:00`），naive 时间返回 `422`；
  `end_utc` 必须严格晚于 `start_utc`。
- 数据库以 WAL 模式运行，支持并发读取；时间以 Unix epoch **微秒整数**存储，
  无浮点精度与时区歧义。

## 目录结构

```
app/
  scheduling.py     # 核心：本地时刻换算、跨午夜拆分、扫描线、排序
  reservations.py   # SQLite 预约存储：单事务冲突检查与写入、幂等、并发安全
  schemas.py        # Pydantic 请求/响应模型
  main.py           # FastAPI 路由（启动时自动建库）
tests/              # pytest 测试（DST、扫描线、预约冲突/幂等/并发/原子性、API）
Dockerfile
docker-compose.yml  # 命名卷 reservation-data 持久化 /data
.env.example
requirements.txt    # 运行依赖
requirements-dev.txt  # 测试依赖（含 pytest、httpx）
```

## 本地开发（不使用 Docker）

需要 Python 3.11+。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# 启动开发服务器（端口 8000；数据库默认落在 ./data/reservations.db）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 自定义数据库路径
RESERVATION_DB_PATH=/tmp/my-bookings.db uvicorn app.main:app --port 8000

# 运行测试
pytest -q
```

## API

### `POST /api/v1/availability/aggregate`

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `start_date` / `end_date` | `YYYY-MM-DD` | 搜索日期范围，均含（UTC 日期为界） |
| `meeting_minutes` | 整数 > 0 | 会议时长（分钟） |
| `min_attendees` | 整数 > 0 | 最低出席人数（不能超过参与者总数） |
| `participants[].id` | 字符串 | 参与者唯一标识 |
| `participants[].timezone` | 字符串 | IANA 时区名，如 `Europe/Berlin` |
| `participants[].availability[].weekday` | `1..7` 或名称 | `1`=周一 … `7`=周日；也接受 `Monday`/`mon` |
| `...start_local` / `end_local` | `HH:MM[:SS]` | 本地钟面时刻；`end <= start` 表示跨午夜 |

请求示例（东京周一 22:00 跨午夜到次日 02:00，纽约周一 09:00–12:00）：

```bash
curl -s -X POST http://localhost:8000/api/v1/availability/aggregate \
  -H 'Content-Type: application/json' \
  -d '{
    "start_date": "2026-03-09",
    "end_date": "2026-03-09",
    "meeting_minutes": 60,
    "min_attendees": 2,
    "participants": [
      {"id": "tokyo", "timezone": "Asia/Tokyo",
       "availability": [{"weekday": 1, "start_local": "22:00", "end_local": "02:00"}]},
      {"id": "ny", "timezone": "America/New_York",
       "availability": [{"weekday": 1, "start_local": "09:00", "end_local": "12:00"}]}
    ]
  }'
```

响应（共同可用段为 UTC 13:00–16:00；东京本地已跨到周二凌晨）：

```json
{
  "start_date": "2026-03-09",
  "end_date": "2026-03-09",
  "meeting_minutes": 60,
  "min_attendees": 2,
  "segments": [
    {
      "start_utc": "2026-03-09T13:00:00Z",
      "end_utc": "2026-03-09T16:00:00Z",
      "duration_minutes": 180,
      "attendees": ["ny", "tokyo"],
      "attendee_count": 2,
      "local_times": [
        {
          "participant_id": "ny",
          "timezone": "America/New_York",
          "start_local": "2026-03-09T09:00:00-04:00",
          "end_local": "2026-03-09T12:00:00-04:00",
          "start_weekday": "Monday",
          "end_weekday": "Monday"
        },
        {
          "participant_id": "tokyo",
          "timezone": "Asia/Tokyo",
          "start_local": "2026-03-09T22:00:00+09:00",
          "end_local": "2026-03-10T01:00:00+09:00",
          "start_weekday": "Monday",
          "end_weekday": "Tuesday"
        }
      ]
    }
  ]
}
```

错误：

- `400`：本地时刻因 DST 春跳变不存在，或 IANA 时区名未知；
- `422`：请求结构校验失败（日期倒置、人数非法、id 重复、weekday 越界等）。

### `POST /api/v1/reservations`

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `request_id` | 非空字符串 | 客户端幂等键；同键重试返回原预约，同键不同内容返回 409 |
| `start_utc` | ISO 8601（含时区偏移） | UTC 会议开始时刻，如 `2026-03-09T13:00:00Z` |
| `end_utc` | ISO 8601（含时区偏移） | 结束时刻，必须严格晚于开始 |
| `participant_ids` | 字符串数组（至少 1 个） | 参与者 ID；自动去重并按字典序排列 |

请求示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/reservations \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-20260309-001",
    "start_utc": "2026-03-09T13:00:00Z",
    "end_utc": "2026-03-09T14:00:00Z",
    "participant_ids": ["bob", "alice"]
  }'
```

成功（`201 created`）：

```json
{
  "id": "res_8f14c2a0e93c4e7f9b6d1a2c3e4f5061",
  "request_id": "req-20260309-001",
  "start_utc": "2026-03-09T13:00:00Z",
  "end_utc": "2026-03-09T14:00:00Z",
  "participant_ids": ["alice", "bob"],
  "participant_count": 2,
  "status": "created"
}
```

内容一致的重试（参与者顺序/重复不影响）返回 `200`，`status="replayed"`，
`id` 与原预约相同，且数据库不新增记录。

**时间冲突（`409`）**——任一名参与者与已有预约在半开区间内重叠，整体拒绝：

```json
{
  "detail": "reservation conflicts with one or more existing reservations",
  "type": "overlap",
  "conflicts": [
    {
      "reservation_id": "res_...",
      "request_id": "req-20260309-001",
      "start_utc": "2026-03-09T13:00:00Z",
      "end_utc": "2026-03-09T14:00:00Z",
      "participant_ids": ["bob"]
    }
  ]
}
```

`participant_ids` 列出该冲突预约中与本次请求真正重合的参与者（取交集）。
相邻区间（结束=对方开始）不冲突，返回 `201`。

**幂等键冲突（`409`）**——相同 `request_id` 但内容不同：

```json
{
  "detail": "request_id 'req-20260309-001' was already used with a different payload",
  "type": "idempotency_conflict",
  "request_id": "req-20260309-001",
  "existing_reservation": { "...": "原预约完整内容" }
}
```

错误码汇总：

- `201`：预约创建成功；`200`：幂等重放原预约；
- `409`：参与者时间冲突，或 `request_id` 被不同内容复用；
- `422`：结束不晚于开始、时间戳无时区偏移、参与者列表为空等结构校验失败。

## 测试覆盖要点

- **聚合**：春季跳变（不存在时刻报错、跨跳变窗口 UTC 时长缩短）、秋季回退（歧义时刻
  取 `fold=0`）、跨午夜拆分与求交、扫描线排序、重叠窗口合并、UTC 边界裁剪、时长过滤。
- **预约存储**：创建与规范化（去重排序）、幂等重放不新增记录、等价 UTC 偏移、
  同 `request_id` 不同内容 409、半开区间重叠（包含/部分重叠/微秒边界）、
  **相邻区间不冲突**、冲突时**无半条写入**、重启后数据持久。
- **预约并发**：多线程同时抢同一时段恰有一条成功、同 `request_id` 突发只落一行、
  互不相关的参与者并发全部成功、部分重叠并发不产生部分占用。

```bash
pytest -q
```
