# 跨时区可用时段聚合 API

基于 **Python + FastAPI + 标准库 `zoneinfo`** 的会议候选时段聚合服务。

每位参与者按**本地星期与钟面时刻**填写每周重复的可用区间并给出 IANA 时区
（如 `Asia/Shanghai`），服务按具体日期应用夏令时（DST）规则，统一换算到 UTC，
用**扫描线**算法计算所有满足最低出席人数与会议时长的候选时段。

- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：`GET /health`

## 语义说明

- **搜索区间**：`[start_date 00:00 UTC, end_date + 1 天 00:00 UTC)`，起止日期均含（以 UTC 日期为界）。
  这意味着 UTC+14 时区里落在本地周一凌晨的窗口，其 UTC 时刻可能处于周日、从而被裁剪在区间之外。
- **跨午夜窗口**：`end_local <= start_local` 时视为跨午夜，自动拆成“当日起点 → 次日终点”的半开区间后换算 UTC。
- **DST 处理**（按每个具体日期分别计算）：
  - **不存在的本地时刻**（春季跳变被跳过的钟面时间，如美东 2026-03-08 02:30）→ 返回 `400` 错误；
  - **歧义时刻**（秋季回退出现两次的钟面时间）→ 统一选择 `fold=0`，即 UTC 偏移量较大、较早出现的那一次。
- **候选段**：出席者集合保持恒定的最大区间；时长必须 `>= meeting_minutes`。
- **排序**：出席人数**降序**，人数相同时按 UTC 开始时间**升序**（稳定排序）。
- 同一参与者互相重叠的窗口会先按人合并，扫描线中一人在任一时刻至多计一份出席。

## 目录结构

```
app/
  scheduling.py   # 核心：本地时刻换算、跨午夜拆分、扫描线、排序
  schemas.py      # Pydantic 请求/响应模型
  main.py         # FastAPI 路由
tests/            # pytest 测试（DST 春跳/秋回退、跨午夜、排序、裁剪、API）
Dockerfile
docker-compose.yml
requirements.txt  # 运行依赖
requirements-dev.txt  # 测试依赖（含 pytest、httpx）
```

## 配置

| 配置项 | 位置 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `APP_PORT` | 容器环境变量 | `8000` | 容器内 uvicorn 监听端口 |
| `HOST_PORT` | 宿主机环境变量 / `.env` | `8000` | 映射到宿主机的端口 |
| `TZ` | 容器环境变量 | `UTC` | 容器系统时区（不影响按 IANA 时区进行的换算） |

构建时镜像内会安装 `tzdata`（IANA 时区数据库），Python 的 `tzdata` pip 包也作为兜底依赖安装。

## 用 Docker Compose 一条命令启动

```bash
docker compose up --build
```

服务启动后访问 <http://localhost:8000>。若 8000 端口被占用：

```bash
HOST_PORT=9000 docker compose up --build   # 访问 http://localhost:9000
```

后台运行与停止：

```bash
docker compose up -d --build
docker compose logs -f
docker compose down
```

## 本地开发（不使用 Docker）

需要 Python 3.11+。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# 启动开发服务器（端口 8000）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

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

## 测试覆盖要点

- **春季跳变**：不存在的钟面时刻报错；跨越跳变间隙的窗口 UTC 时长缩短（柏林 2026-03-29 00:00–04:00 实际只有 3 小时）。
- **秋季回退**：歧义窗口取 `fold=0`（美东 2025-11-02 01:00–02:00 → UTC 05:00–07:00，共 2 小时）。
- **跨午夜**：窗口拆分、与另一时区窗口求交、各自本地时刻回显。
- 扫描线排序（人数降序、开始时间升序）、同参与者重叠窗口合并、UTC 边界裁剪、时长过滤。

```bash
pytest -q
```
