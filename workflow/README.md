# 短剧工作流（内部自用）

把「剧本 → 分镜 → 提示词 → 视频」串成一条可跑的流水线，模型调用全部走隔壁的聚合层（`../aggregator`）。
当前定位：**内部跑通、单租户、无登录**。

```
剧本 → 分集 → 分镜(LLM) → 提示词(LLM) → 视频任务(聚合层) → 轮询回执
         ↑          ↑            ↑                 ↑
     规则拆分    llm.storyboard  llm.prompts    agg.submit_video
```

## 跑起来（两个服务）

```bash
# 1) 模型聚合层（没有 key 也能跑，走 mock）
cd aggregator && python -m uvicorn app.main:app --port 8010

# 2) 工作流
cd workflow  && python -m uvicorn wf.main:app  --port 8011

# 3) 端到端演示
python workflow/examples/demo.py
```

`WORKFLOW_LLM=mock` 时完全不调模型（本地规则生成分镜/提示词），整条流水线零成本跑通；
接上真实渠道 key 后把 `WORKFLOW_LLM=auto` 即可自动切到模型，失败再退回规则。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 服务状态与当前模型配置 |
| POST | `/projects` | `{name, style}` 建项目 |
| GET | `/projects` / `/projects/{id}` | 列表 / 详情（含分镜、提示词、任务） |
| POST | `/projects/{id}/script` | `{text}` 写入剧本 |
| POST | `/projects/{id}/parse` | 按「第N集 / EP1」拆集 |
| POST | `/projects/{id}/storyboard?episode_no=&force=&max_shots=` | 生成分镜（已有则跳过） |
| POST | `/projects/{id}/prompts?force=` | 生成视频提示词 |
| POST | `/projects/{id}/videos?resolution=&force=` | 提交视频任务（逐镜，失败不阻断） |
| POST | `/projects/{id}/videos/poll` | 拉回执（成功结算、失败退款） |
| POST | `/projects/{id}/run?max_shots=&resolution=&force=` | **一键跑完整条** |

项目状态机：`draft → script_ready → parsed → storyboard_done → prompts_done → videos_running → videos_done`。
镜头状态机：`pending → storyboard_ready → prompt_ready → video_submitted → video_done / video_failed`。

## 数据

SQLite 单文件（`WORKFLOW_DB`，默认 `workflow/data/workflow.db`）：

```
projects(id,name,style,script_text,status)
episodes(id,project_id,no,title,raw_text)
shots(id,project_id,episode_id,shot_no,duration_s,scene,size,angle,movement,picture,dialogue,sfx,status)
prompts(id,shot_id,positive,negative,model)
jobs(id,shot_id,agg_task_id,model,resolution,duration_s,status,url,charged_rmb,error)
```

## 关键设计

- **幂等**：分镜/提示词/视频都按状态跳过，`force=true` 才重做；重复调用不会重复扣费。
- **单镜失败不阻断**：批量提交时某镜 402/上游错误记进 `skipped`，其余继续。
- **余额自愈**：`WORKFLOW_AUTO_TOPUP=100` 时遇到 402 自动充值重试一次（内部自用不为难自己）。
- **两段式提示词**：`shot` 保存结构化分镜，`prompt` 保存可直接喂视频模型的连续时间轴文本。
- **LLM 兜底**：真实模型输出不是合法 JSON 时自动退回规则生成，流水线不会因为模型抽风中断。

## 测试

```bash
python workflow/tests/run_tests.py     # 31 条，聚合层 + 工作流同进程（ASGI），不联网不花钱
```

覆盖：建项目 → 剧本 → 2 集拆分 → 分镜（幂等）→ 提示词 → 提交视频 → 轮询结算 → 项目/镜头状态推进
→ 账本扣费 → 一键跑通 → 404/400 边界。

## 还没做

- 前端（现在只有 HTTP；先服务内部脚本/CLI）
- 人物/场景资产库（跨镜头一致性）——下一步接
- 站立位图、分镜图（聚合层的 image 接口已就绪，未接）
- 多集并行与断点续跑（现在是串行 + 幂等跳过）
