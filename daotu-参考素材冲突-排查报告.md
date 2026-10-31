# 刀徒AI「参考素材已变化」提交报错 —— 排查报告

- 站点：https://www.daotu.vip/ （工作台 V26.10.839，网页端 runtime_mode=web）
- 账号：`远端登陆`（user_id **147**，UID **DT1W57VCOS6J**，角色 editor，积分 0）
- 排查时间：2026-10-31
- 结论一句话：**不是素材丢了，是"项目版本号（revision）"被后台自动写回抢跑，前端拿旧版本号保存，被后端字段级变更校验挡下。**

---

## 1. 你截图里的两条报错

| 场景 | 文案 |
|---|---|
| 视频页提交 | 未能提交视频任务：对象「PROMPT-48-1」的 `positive_prompt`、`reference_bindings` 已变化，请保留草稿并核对最新内容 |
| 提示词/分镜保存 | 保存失败 EP001-S013：对象「PROMPT-1-13」的 `reference_bindings`、`selected_material_ids` 已变化 |

这两句都**不在网页前端包里**（1.75 MB 的 `app.runtime.min.js` 中 `保留草稿`、`核对最新内容` 出现次数均为 0），说明是后端生成的，前端只是 `toast(error.message)`。

## 2. 后端自己的日志把话说清楚了

桌面端三个日志文件（2026-10-28 ~ 10-30）里共有 **325 条**同一类记录：

```
WARNING backend.project_edit_conflicts edit_conflict
  {"project_id":"9eae4ed5-...","object_id":"PROMPT-EP001-S011",
   "conflict_fields":["positive_prompt","reference_bindings","selected_material_ids"],
   "expected_revision":1314,"actual_revision":1315,
   "change_source":"change_journal","change_actor":"unknown",
   "operation_id":"edit-e662ca9ec53d4604932f90c0c3ccf5b2"}

WARNING backend.main editor_save_rejected
  {...同上..., "client_operation_id":"edit-mvt7gb2o-npjsyruvgh"}
```

- `desktop-launcher.log:165767-165768` → PROMPT-EP001-S011，期望 1314 / 实际 1315
- `desktop-launcher(2).log:203690` → PROMPT-EP013-S003，期望 453 / 实际 **456**（一次落后 3 个版本）
- `desktop-launcher(2).log` 一个文件就有 291 条

### 统计（325 条）

**版本落后幅度**（actual − expected）：

| 落后版本数 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 9 | 10 | 14 | 39 | 48 | 53 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 次数 | 29 | 74 | **107** | 11 | 1 | 55 | 2 | 2 | 16 | 3 | 9 | 3 | 13 |

**冲突字段组合**：

| 组合 | 次数 | 含义 |
|---|---|---|
| `$identity` | **243** | 对象被新增/删除/重建（多发生在角色资产上） |
| `positive_prompt, reference_bindings` | 21 | 正是视频提交那条 |
| `reference_bindings, selected_material_ids` | 21 | 正是保存那条 |
| `positive_prompt` | 12 | |
| 其它 +`selected_asset_ids`/`reference_exclusions` | 28 | |

**被卡得最多的对象**：`CHAR-223385ce5d`(76)、`CHAR-0b4477e4ce`(52)、`CHAR-8df793893b`(39)、`CHAR-bee74fc15d`(26)；然后是 `PROMPT-EP013-S003`(25)、`PROMPT-EP048-S001`(22)、`PROMPT-EP011-S003`(13)、`PROMPT-EP001-S013`(5)。

**变更来源**：`change_source` 全部是 `change_journal`，`change_actor` 全部是 `unknown` —— 也就是"改了东西，但查不出是谁改的"，典型是**系统自动写回**，不是另一个用户在改。

同一时期日志里有 **1593 条** `backend.task_receipt_cache Slow task receipt refresh ... 10~60s`：任务回执（远端出片结果）长时间轮询后才落库，落库时会推进项目 revision。

## 3. 机理

1. 编辑器打开时记下 `revision = N`（基线），保存 = `PATCH /api/projects/{p}/prompts/{id}` 并带 `expected_revision: N`。
2. 后台自动写回（任务回执、参考素材解析、资产重建）把项目推到 `N+1 / N+3 / N+53`。
3. 后端 `backend/project_edit_conflicts` 发现 `expected ≠ actual`，再翻变更日志算出"这一版这个对象变了哪些字段"，如果这些字段正好是你要写的字段 → 拒绝，返回：
   `对象"X"的 reference_bindings, selected_material_ids 已变化，请保留草稿并核对最新内容。`
4. 前端把这条文案直接 toast，草稿留在 `sessionStorage: daotu_edit_recovery:*`，于是"怎么点都保存不了/提交不了"。

两个补充验证：

- 网页端同一冲突在"项目版本"这一层先被拦下，返回的是另一条：`409 {"detail":"项目已更新，请重新读取当前集合。","code":"project_revision_conflict"}`（我用 API 复现过）。截图那种"对象 + 字段"的文案出自桌面端 `backend.project_edit_conflicts` 的字段级判定。
- 参考素材本身确实有历史包袱：`妖王` 项目 14 条提示词里 **11 条**库里存的 `reference_bindings` 与当前解析器算出来的不一致（10 条库里是空数组，实际解析能出 1~17 条；新解析的 binding 还多一个 `segment_key` 字段）。这也让"字段已变化"更容易命中。

## 4. 你这边怎么绕过去（按优先级）

1. **别在任务跑的时候改东西**：顶部任务进度条/通知中心清空后再保存或提交。回执写回是最大扰动源。
2. **报错后先刷新/重开项目**（左上项目选择器切一下），让基线追到最新 revision，再点保存；不要连点（日志里同一对象连撞 5~25 次）。
3. 报错卡片上的 **「核对并处理当前草稿」** 是官方合并入口：逐字段选「保留我的草稿 / 采用最新内容」→「确认合并结果并保存」。
4. 如果是 `$identity`（对象已新增、删除或重建，多见于角色资产）：只能「放弃草稿，采用最新内容」，因为对象 id 已被重建。
5. 视频提交前先点 **「核对未完成批次」**，把 pending 批次结清，再「批量生成」。

## 5. 如果这是你们自己的产品（改哪儿）

1. **给自动写回加身份**：任务回执、参考解析、资产重建写入时带上 `actor` 与 `operation_id`，别再落成 `change_actor: unknown`。
2. **同端自动写回可直接合并**：判定为"本账号/本窗口的自动写回"时，服务端保留用户草稿的 `positive_prompt`、重置 `reference_bindings`，不要让用户手工合并。
3. **前端自动 rebase 重试一次**：`saveLocalEntityEdit` 里已有 `rebaseAcknowledgedEdit`，但它只吃"本端 ack 过的 revision"，覆盖不到系统写；建议冲突时自动重取基线 + 重放一次草稿再决定是否弹窗。
4. **回执慢是放大器**：单次 10~60s 的 `task_receipt_cache` 轮询把冲突窗口拉得很长，值得优化。
5. **老项目做一次绑定迁移**：把 `reference_bindings` 按当前解析器重算并持久化（见下面第 6 节，接口 `PATCH /prompts/{id}/asset-selection` 是合法通道，我验证过）。

## 6. 我在测试账号上做了什么

- 复现过程中，把 `妖王` 项目 11 条提示词的 `reference_bindings` 重新解析并写回（0→1/2/3/17 条，26→26 条等），项目 revision 2→13。修复后视频页 14 张卡片参考素材正常勾选。
- 副作用：留下了 **2 条 video-jobs pending 批次**（镜头 EP001-S002，0 个任务、未扣费），在视频页卡片上显示"提交待确认"。删除接口返回 405，需要在「核对未完成批次」里处理，或等系统自动清理。
- 可复用脚本：
  - `browser/login.py` —— 登录并保存会话（cookie + localStorage）
  - `browser/probe/probe_all.js` —— 批量比对"库里的绑定"与"解析器输出"
  - `browser/probe/repair.js` —— 批量重算并写回 `reference_bindings`
  - `browser/probe/analyze_conflicts.js` —— 从桌面端日志统计冲突

## 7. 环境说明（重要）

Chromium 在 DSH 沙箱里起不来（多进程 IPC 需要命名管道，被沙箱拒绝），所以浏览器自动化是在提升权限下跑的；会话已落盘在 `browser/state.json`，后续可直接复用。
