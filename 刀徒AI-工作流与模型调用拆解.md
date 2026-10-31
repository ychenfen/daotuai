# 刀徒AI（daotu.vip）工作流与模型调用拆解

> 数据来源：登录态实测抓包（70 个端点）+ 三个模型目录 API + `/api/system/status` + 前端主包静态分析。
> 抓包时间：2026-10-31；账号 `远端登陆`（editor 角色）。

---

## 0. 一句话结论

它不是"某个模型的应用"，而是**一条六段式生产线 + 一个 20 家上游 API 的聚合计费层**。
真正的护城河在**流水线工程**（一致性、幂等、回执、计费），不在模型本身——模型全是第三方。

---

## 1. 架构（`/api/system/status` 直接读出来的）

| 字段 | 值 | 含义 |
|---|---|---|
| `deployment_mode` | `cloud_control_plane` | 客户端是"本地工作台 + 云端控制面" |
| `host:port` | `127.0.0.1:8088` | 本地服务端口 |
| `public_base_url` | `https://www.daotu.vip` | 云端入口 |
| `auth_mode` | `employee_accounts` | 员工账号 + 角色 + 审计 |
| `legacy_app` | `/app/legacy/AI短剧工作流7.17.2.pyw` | 前身是个 Py 桌面脚本，现在是它的 Web 化 |
| `project_dir` / `output_dir` | `/data/legacy/...` | **项目 JSON / 图片 / 视频都在本地**，只有账号积分在云 |

**上游 API 池（20 家，18 家已配置）**：

```
XB API、DM API(dmxapi)、XC API、SX API、New API、A6 API、XY API、
Volcengine Ark(火山方舟)、GG API、Jimeng API(即梦)、
MY API(梦语)、CH AI、XR API、LI API(custom_api_1)、上海煜邦、
video-2.5-flash、WC API、KUN API、LD API、HG API（未配置）
```

---

## 2. 六段流水线

| # | 环节 | 入口 API | 产物 | 典型失败点 |
|---|---|---|---|---|
| 1 | 剧本 | `PUT /script`、`POST /script/upload`、`POST /script-parse/v3/jobs` | 剧集 + 角色/场景/道具实体 | AI 解析额度 / 结构校验 |
| 2 | 资产 | `/assets`、`/asset-image/models`、`/assets/{id}/family-review` | 角色形态 / 场景 / 道具图（1K-4K，带历史版本） | 参考图一致性、版本错配 |
| 3 | 分镜 | `POST /prompts`（`mode:"ai"`）、`/query/entities/shots` | shot：画面/表演/台词/运镜/建议时长 | 人物连续性、时长不合理 |
| 4 | 站位图 | `/blocking/direct`、`POST /blocking/photo/start` | 场景站位图（人物空间关系） | 遮挡、比例 |
| 5 | 提示词 | `POST /prompts/{id}`、`POST /prompts/{id}/resolve-references` | `positive_prompt` + `reference_bindings` | **@提及解析不到真实资产** |
| 6 | 视频 | `/pricing/quote` → `/video-jobs/batch/prepare` → `/video-jobs/batch` | 每镜成片 + 历史版本 | 报价、幂等、回执超时 |

核心数据结构（项目 JSON）：

```
project{ revision, shots[], assets[], prompts[], video_jobs[], blocking_* }
prompt{ prompt_id, shot_id, positive_prompt, reference_bindings[],
        selected_asset_ids, selected_material_ids, model, aspect_ratio }
binding{ kind, asset_id, version, history_id, source_identity, segment_key }
```

`reference_bindings` 是整个产品的关键字段：它把"@孔宣（蛋形态）"解析到"具体资产 + 具体历史版本"，解决 AI 出图的角色一致性。

---

## 3. 模型调用矩阵

### 3.1 文本平面（三任务共享一套路由）

```
PUT /api/agent-generation/{key}/routing   { provider, model }
GET /api/agent-generation/options         （需 agent 配置权限；editor 账号 401）
```

| 路由 key | 用途 | 说明 |
|---|---|---|
| `storyboard_analysis` | 剧本 → 分镜解析 | **三者共享同一设置**（前端 `sharedTextGenerationRouteKeys`） |
| `storyboard` | 分镜生成 | 同上 |
| `shot_prompt` | 分镜 → 视频提示词 | 同上 |
| `asset_prompt` | 资产提示词 | 独立 |
| `polish` | AI 润色 | 独立 |
| `4k` | 4K 高清 | 独立 |

计费：`unified_text_token_billing = true`，按 token 结算，1 元 = 10 积分。

### 3.2 图像平面

```
GET /api/asset-image/models
```

- 默认 provider：`custom_api_6`（KUN API）
- 模型：`gpt-image-2`、`gpt-image-2.5`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`
- 计价：**5 积分/张**，1K/2K/4K 同价（`points_by_resolution`）
- 支持：批量生图、4K 高清（独立 upscale provider）、AI 润色（独立 polish provider）
- 参考图策略：`image-reference-20261015-v2`，单图 20 MB / 总 64 MB，`provider_validated`

### 3.3 视频平面（最复杂的一层）

```
GET /api/video/options    →  catalog_version: video-products-20260923-v1.1
```

**28 个模型，12 个可提交**：

| 可用 | 分辨率 | 上游线路数（按 480P/720P/1080P） |
|---|---|---|
| seedance-2.5 | 480/720/1080P | 4 / 5 / 2 |
| seedance-2.0 | 480/720/1080P/2K/4K | 3 / 8 / 4 / – / 3 |
| seedance-2.0-fast | 480/720/1080P | 3 / 3 / 0 |
| seedance-2.0-mini | 480/720P | 4 / 5 |
| wan-3.0 | 480/720/1080P | 2 / 3 / 2 |
| minimax-h3 | 720P 起 | 3 |
| grok-imagine(-1.5) | 720P | 1 |
| omni-flash-video-edit | 720P 起 | 1（**INPUT_SECOND 计费**） |
| vidu-q3 | 540/720/1080P | 1 |
| wan-2.2-motion-transfer | 720P | 1 |
| agnes-video-2.5-flash | 720P | 1 |

**关闭**：kling 全系（1.0/1.6/2.1/2.5-turbo/3.0/3.0-omni）、veo 3.1(+fast)、seedance 1.x、happyhorse、agnes-2.5、wan-3.0-prime。

**关键设计：能力契约 + 线路轮换**

```json
"capability_contract": {
  "version": "effective-capabilities-20261014-v1",
  "identity": { "provider": "CH AI", "api_id": "rd-seedance-2.5-480p",
                "group": "RD渠道", "spec": "480P" },
  "supplier_evidence": "seedance-rotation-20260930-v1",
  "classification": "unverified_catalog"
}
```

同一个"Seedance 2.5 / 480P"背后挂了 4 条不同上游（CH AI 的 RD渠道/纯血/default、LI API），后端按 profile 轮换/降级；前端只显示"模型 + 分辨率"。
计费一律 `OUTPUT_SECOND`（按输出秒），只有 omni-flash 视频编辑是 `INPUT_SECOND`。

### 3.4 对话平面

```
GET /api/chat/agents   →  4 个智能体，482+100+65+25 个模型
```

| 智能体 | 渠道 | 默认模型 |
|---|---|---|
| chat_5019a6f598 | DMXAPI | gpt-6.1-sol（482 个模型） |
| chat_8d9807eff8 | A6 API | gpt-6.1-sol（100 个） |
| chat_fe7c336aae | SX API | gemini-3.8-flash（65 个） |
| chat_f3cbf81975 | custom_api_1 | gpt-6.1-sol（25 个） |

**同模型跨渠道价差极大**（这就是"聚合层"的价值）：

| 模型 | A6 API | custom_api_1 |
|---|---|---|
| gpt-6.1-sol | 8.05 / 40.27 元/百万 token | 0.72 元/百万 token（积分折算） |
| gpt-6-astra | 99.28 / 496.4 元/百万 token | 99.28 / 496.4（同价） |

---

## 4. 一次视频生成的完整调用链

```
1. GET  /api/video/options                      读目录、能力、可用规格
2. POST /api/projects/{id}/prompts              落库编辑后的提示词（带 expected_revision）
3. POST /api/projects/{id}/prompts/resolve-references
                                                把 @提及 解析成 reference_bindings（含历史版本）
4. POST /api/projects/{id}/pricing/quote        { kind:"video-batch", metadata:{model,resolution,duration} }
                                                → 报价 + 预扣积分
5. POST /api/projects/{id}/video-jobs/batch/prepare
                                                { items[], client_request_id } → 批次记录（幂等）
6. POST /api/projects/{id}/video-jobs/batch     真正提交 → 每镜 accepted / rejected / unknown
7. GET  /api/tasks、/video-jobs、/video-jobs/batch-pending
                                                轮询回执；失败自动退积分
8. 成片入库 /video-shots/{shot}/history/v{n}/poster
```

其中 `quote → prepare → batch` 三段式是防重复扣费的核心：
- `client_request_id` 幂等键，断线重连不会重复下单；
- `prepare` 先落"准备记录"，`batch` 才真正调上游；
- 失败回执带 `recovery.state`（resumable / complete），客户端可续跑。

---

## 5. 值得学走的 7 个设计

1. **能力契约（capability contract）+ 线路轮换**：模型×规格 → 多条上游，前端只暴露"模型+分辨率"，后端按 profile 轮换降级。这是聚合型 AI 产品的标准做法。
2. **三段式提交 + 幂等键**：quote / prepare / batch，`client_request_id` 贯穿。断网、重复点击、服务重启都不会重复扣费。
3. **预扣 + 失败退回**：积分先行冻结，失败自动回滚（`points_wallet`）。用户信任感来自这里。
4. **双解析通道**：AI 解析（走模型）与"本地解析"（零费用离线规则）并存，没额度也能干活。
5. **参考素材绑定模型**：`@提及 → resolve-references → reference_bindings(version/history_id/source_identity)`，这是"角色一致性"的工程解法。
6. **乐观锁 + 变更日志**：项目 `revision` + 字段级冲突提示（我们上一轮排查的 325 次冲突就出在这）。
7. **本地资产 / 云端计费分离**：项目 JSON、图片、视频都在本地盘，只有账号/积分/订单在云。隐私友好，也降低云存储成本。

---

## 6. 如果要复刻：最小可行切片

| 阶段 | 范围 | 说明 |
|---|---|---|
| MVP | 剧本 → 分镜 → 提示词 → 视频卡片 | 先不碰资产一致性与站位图 |
| 模型层 | 接 2–3 家（如 Seedance 2.0/2.5 + Wan 3.0 + gpt-image-2） | 先不做轮换，只做 fallback |
| 计费 | 预扣 + 失败退回 | 兑换码/人工审核可后置 |
| 数据 | 单机 JSON 项目文件 | 别急着上数据库 |
| 必做 | capability contract 从第一天就设计 | 否则后期加线路会重构 |

---

## 7. 坑（都是实测遇到的）

- **上游是"未验证目录"**：`classification: unverified_catalog`，同模型不同 group 的实际质量/限流不同，必须做健康度与降级。
- **视频按秒计费**：时长 × 分辨率 × 是否带音频 三维度，报价必须先算，否则容易被刷额度。
- **多上游 = 多套错误码**：需要统一错误归一化层（它现在用的是 `website_gateway_rejected` / `QUOTE_REQUIRED` 这类包装）。
- **素材一致性最容易出问题**：`reference_bindings` 与库不一致会直接拒绝提交（我们实测 14 条提示词里 11 条不一致）。
- **回执慢**：`task_receipt_cache` 单次 10–60s，会把"编辑 → 保存"的竞态窗口拉得很长。

---

## 附：抓包产物

- `probe_api/api_capture.json` —— 155 条请求（70 个去重端点）
- `probe_api/endpoints.txt` —— 端点清单
- `probe_api/video_options.json`（166 KB）/ `image_models.json`（80 KB）/ `chat_agents.json`（916 KB）—— 三份完整模型目录
- `probe_api/system.json` —— 部署形态与 20 家上游配置
