# 刀徒复刻 · 内部自用版

按「先打通工作流」的目标，现在拆成两个服务，已经能端到端跑：

```
┌──────────────────────────┐        ┌──────────────────────────────┐
│  workflow  :8011         │  HTTP  │  aggregator :8010            │
│  剧本→分集→分镜→提示词    │ ─────► │  chat / image / video 统一接口 │
│  →视频任务→回执           │        │  选路降级 + 预扣结算 + 毛利    │
│  SQLite: 项目/镜头/任务   │        │  SQLite: 钱包/账本/任务        │
└──────────────────────────┘        └──────────────────────────────┘
                                                │
                                    ┌───────────┴───────────┐
                                    │ 20 家上游（DM/XC/SX/…）│
                                    │ 无 key 时自动走 mock    │
                                    └───────────────────────┘
```

## 快速开始

```bash
# 1) 聚合层（先起，工作流依赖它）
cd aggregator && python -m uvicorn app.main:app --host 127.0.0.1 --port 8010

# 2) 工作流
cd workflow && python -m uvicorn wf.main:app --host 127.0.0.1 --port 8011

# 3) 端到端演示（建项目 → 剧本 → 一键 → 轮询 → 报表）
python workflow/examples/demo.py
```

Windows 也可以直接双击 `aggregator/start.cmd` 和 `workflow/start.cmd`。

## 实测（全 mock，零成本）

```
storyboard: [{"episode":1,"shots":4,"source":"mock"},{"episode":2,"shots":3,"source":"mock"}]
prompts: 7
videos submitted: 7

项目状态：videos_done  镜头 7 个
  E1-1 远景/平视 6.0s [video_done] https://mock.local/…  ¥4.0
  E1-2 全景/俯拍 4.0s [video_done] https://mock.local/…  ¥4.0
  …
  E2-2 全景/俯拍 4.0s [video_done] https://mock.local/…  ¥4.0

聚合层报表: 收入 28.0  成本 23.52  毛利 16.0%
钱包: 余额 72.0  累计消费 28.0
```

## 环境变量

| 变量 | 服务 | 默认 | 说明 |
|---|---|---|---|
| `AGG_BASE` | workflow | `http://127.0.0.1:8010` | 聚合层地址 |
| `WORKFLOW_LLM` | workflow | `auto` | `auto`/`real`/`mock`；mock 用本地规则，零成本 |
| `WORKFLOW_AUTO_TOPUP` | workflow | `100` | 遇到 402 自动充值额度（内部自用） |
| `WORKFLOW_TEXT_MODEL` | workflow | `gpt-6.1-sol` | 分镜/提示词用的文本模型 |
| `WORKFLOW_VIDEO_MODEL` | workflow | `seedance-2.5` | 视频模型 |
| `WORKFLOW_POLL_INTERVAL_S` | workflow | `15` | 自动轮询回执间隔（0 关闭） |
| `WORKFLOW_DB` / `AGG_DB` | 两者 | `data/*.db` | SQLite 路径 |
| `*_API_KEY` | aggregator | — | 渠道 key，见 `aggregator/providers.yaml` |
| `AGG_PROXY` | aggregator | — | 显式代理（默认不信环境变量） |

## 测试

```bash
python aggregator/tests/run_tests.py   # 53 条：定价/预扣/幂等/降级/流式/对账/报表
python workflow/tests/run_tests.py     # 31 条：剧本→分镜→提示词→视频→回执 全链路
```

两套测试都不联网、不花钱；工作流测试把聚合层以 ASGI 方式挂进同进程。

## 目录

| 目录 | 内容 |
|---|---|
| `aggregator/` | 模型聚合层（20 渠道、计费、报表、对账） |
| `workflow/` | 短剧流水线（项目/剧集/分镜/提示词/视频任务） |
| `screens/` | 刀徒 AI 工作流截图册（学习用） |
| `browser/`、`probe_api/` | 逆向出的接口/模型目录/登录态 |
| `tools/` | 一次性脚本 |

## 下一步（按优先级）

1. 资产库：角色/场景跨镜头一致性（现在每个镜头独立提示词）
2. 分镜图 / 站位图：聚合层 image 接口已就绪，未接
3. 真实渠道联调：填 key，把 `WORKFLOW_LLM=auto`，跑同一套测试
4. 简单前端或 CLI：直接读 `/projects/{id}` 的 JSON 渲染
