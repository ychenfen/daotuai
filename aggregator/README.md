# 模型聚合层（20 家渠道 → 统一接口 + 计费）

一个可独立运行的聚合网关：**OpenAI 兼容的 chat（含 SSE 流式）/ image + 异步 video**，
背后挂 20 家上游，统一做「能力契约 → 选路降级 → 预扣 → 结算/退回 → 账本 → 成本毛利」。

- 版本：0.3.0
- 依赖：FastAPI / uvicorn / httpx / PyYAML（无数据库服务，SQLite 落盘）
- **没有 API Key 也能跑**：未配 key 的渠道自动走 mock，适合联调前端与计费

## 30 秒跑起来

```bash
cd aggregator
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
# 或 Windows 双击 / 执行：start.cmd
```

```bash
python examples/client.py                 # 全链路 demo
python tests/run_tests.py                 # 40 条离线测试（不联网）
python tests/http_check.py                # 对运行中的服务做 14 条 HTTP 检查
```

## 目录

```
aggregator/
├── providers.yaml        # 19 家渠道：base_url / key 环境变量 / 并发 / 超时 / 开关 / 代理
├── catalog.yaml          # 9 个模型：类型、规格、时长、多线路、价格
├── app/
│   ├── config.py         # YAML 加载、候选线路（按权重）
│   ├── concurrency.py    # 每渠道并发闸门（信号量 + 排队超时）
│   ├── providers.py      # OpenAI 兼容 / 火山 Ark / mock；chat_stream 支持 SSE
│   ├── router.py         # 权重排序 + 冷却熔断 + 降级 + 时延统计
│   ├── billing.py        # 报价 / 预扣 / 结算 / 退回 / 汇总 / 对账
│   ├── db.py             # SQLite：wallet / holds / ledger / idem / tasks
│   └── main.py           # FastAPI 路由 + 请求 ID + 流式 + 运维接口
├── examples/client.py    # 调用示例
├── tests/
│   ├── run_tests.py      # 离线全量测试（40 条断言）
│   └── http_check.py     # 真实 HTTP 检查（14 条）
└── data/                 # 运行时 SQLite（自动创建）
```

## 统一接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI 兼容；`stream:true` 返回 SSE |
| POST | `/v1/images/generations` | OpenAI 兼容，按张计费 |
| POST | `/v1/videos/generations` | 提交异步任务 → `vtask_xxx` |
| GET | `/v1/videos/{id}` | 查一次状态（成功即结算、失败即退回） |
| GET | `/v1/videos/{id}/wait?timeout_s=30` | 阻塞轮询到终态 |
| POST | `/v1/videos/{id}/cancel` | 本地取消并退回预扣（上游可能仍在跑） |
| POST | `/v1/quote` | 只报价不扣费 |
| GET | `/v1/models` | 目录 + 线路 + 零售价/成本 |
| GET | `/v1/wallet` / `POST /v1/wallet/topup` | 余额、累计充值/消费/成本/毛利、未结算预扣 |
| GET | `/v1/ledger` | 账本流水 |
| GET | `/admin/health` | 渠道健康度 + 并发占用 |
| GET | `/admin/stats` | 任务/预扣/账本统计 |
| GET | `/admin/reconcile` | 对账：账本加总 vs 钱包余额、孤儿预扣 |
| GET | `/admin/report?days=7` | 收入/成本/毛利率（按模型、按模型×渠道） |
| POST | `/admin/reap?older_than_minutes=60` | 回收过期预扣（dry_run 可先看） |

约定：
- 用户维度：`X-User-Id`
- 幂等：`Idempotency-Key`（同 key 同 body 返回同一响应，不重复扣费）
- 每个响应带 `X-Request-Id` / `X-Elapsed-Ms`
- 金额单位：元；积分 = 元 × 10

## 三个核心机制

### 1. 能力契约 + 多线路

```yaml
- id: seedance-2.5
  specs: [480P, 720P, 1080P]
  duration: { min: 4, max: 30, step: 1 }
  routes:
    - { provider: ch_ai_video,  api_id: rd-seedance-2.5-480p, spec: 480P, group: RD渠道, weight: 2 }
    - { provider: custom_api_1, api_id: seedance2.5-A,        spec: 480P, group: default, weight: 1 }
```

前端只认 `model + resolution`；后端按 weight 选路，失败自动换下一家。
同一模型可以按线路单独定价（`route.price` 覆盖模型价），支持"按公开价卖、按低价渠道采买"。

### 2. 预扣 → 结算 → 退回（账本可加总）

```
quote → reserve（冻结；余额不足 402）→ 调上游
          ├─ 成功 → settle（按实际 usage 多退少补，账本只记差额）
          └─ 失败 → refund（全额退回）
```

`topup + reserve + settle + refund == Σ钱包余额`，`/admin/reconcile` 的 `drift_rmb` 必须为 0。
重复 settle / refund 幂等（不会重复入账）。

### 3. 熔断与并发闸门

- 失败计数 + 指数退避冷却（2s→60s），冷却中的线路排到最后，但**全部冷却时仍给一次机会**，避免假死锁
- 每渠道信号量限流 `concurrency`，排队超过 8s 视为该家不可用，直接换下一家
- 非 4xx 一律降级；4xx 也继续尝试其他渠道（不同渠道模型 ID 可能不同）

## 价格口径（详见 `渠道价格调研.md`）

| 层 | 计费单位 | 锚点 |
|---|---|---|
| 文本 | tokens | 按上游返回 usage 的 prompt/completion tokens 结算 |
| 图像 | 张 | 5 积分/张 = ¥0.5 |
| 视频 | 输出秒 | 480P ¥0.80/s（刀徒零售实测 8 积分/秒）、720P ¥1.75、1080P ¥4.30 |

视频优先用上游返回的 `usage.output_seconds` 结算；拿不到才退回请求时长。

### 成本与毛利（price vs cost）

目录里每个模型/线路有两套价：

| 字段 | 含义 | 用途 |
|---|---|---|
| `price` | 对外零售价（按它扣用户） | 计费、报价 |
| `cost` | 上游批发价（按它算我们的成本） | 报表、毛利、选线优化 |

线路可以覆盖其中任意一层，所以"按 A6 的公开价卖、按 LI 的价采买"这种策略在数据层就成立。
`/admin/report` 直接给出收入、成本、毛利率，实测：

```
seedance-2.5   1 次  收入 4.00  成本 3.36  毛利 16.0%
gpt-image-2.5  1 次  收入 0.50  成本 0.32  毛利 36.0%
合计           收入 4.50  成本 3.68  毛利 18.2%
```

### 过期预扣回收

上游挂起 / 客户端断连会留下 `held` 状态的钱。后台每 5 分钟扫一次（`AGG_REAP_INTERVAL_S`），
把超过 60 分钟的预扣全额退回并标记 `expired`；也可以手动：

```bash
curl -X POST "127.0.0.1:8010/admin/reap?older_than_minutes=60&dry_run=true"   # 先看
curl -X POST "127.0.0.1:8010/admin/reap?older_than_minutes=60"                # 真退
```

## 渠道 / 代理 / 环境变量

```bash
set DMXAPI_KEY=sk-xxx          # 渠道 key（providers.yaml 里 api_key_env）
set ARK_API_KEY=xxx            # 火山方舟
set AGG_PROXY=http://127.0.0.1:7897   # 需要代理时用；默认 trust_env=False
set AGG_DB=D:\data\agg.db      # 账本位置
set MOCK_FAIL=dmxapi,a6_api    # 故意让某几家失败，演示降级
```

> ⚠️ 默认 `trust_env=False`。Windows 上 `NO_PROXY` 里出现 `[::1]` 会让 httpx 直接抛
> `InvalidURL(':1]')`；统一关掉 env 代理、显式用 `AGG_PROXY` 更可控。

## 测试覆盖

`tests/run_tests.py`（**53 条**，全 mock，离线）：

- 定价数学（文本 48.32 元/百万双计、图像 1.5 元/3 张、视频 4.0 元/480P×5s、720P 8.75 元）
- 预扣不足 402、按实际 usage 结算、幂等重放不重复扣费
- 视频异步：预扣 3.2 → 上游返回 5s → 结算 4.0（补扣）
- 降级：主线路挂 → attempt=2 换 dmxapi；全挂 → 502 + 全额退回 + attempts 明细
- SSE：≥3 个 delta + 最终 billing
- 对账：drift=0、无孤儿预扣、任务/并发统计
- 报表：收入/成本/毛利、无未知模型；reap dry-run/实退/余额恢复；取消视频全额退
- 配置一致性：线路数、权重排序

`tests/http_check.py`（**17 条**）：对真实 uvicorn 实例跑同样的主链路（含报表与取消）。

## 还没做（下一步）

- 上游账单每日对账任务（现在只做本地账本自洽 + 过期回收）
- 视频超时任务的状态回收（现在靠 `/wait` 或手动 cancel + reaper）
- 多租户隔离与企业账单导出
- 流式计费的 token 精确性（现在流式结束时若上游不给 usage，用字符数估算）
