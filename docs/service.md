# 素材谱系服务

在 `contracts/` 基础事件契约之上，`src/travel_music/service.py` 实现发行团队要求的业务规则，
`src/travel_music/worker.py` 负责后台期限任务。状态持久化在 SQLite（事件流 + 投影表，
同一事务写入），进程中断后新实例可直接继续处理。

## 角色

| 角色 | 职责 |
| --- | --- |
| `author` | 登记素材、创建剪辑方案、申报自己的创作与贡献 |
| `fact_checker` | 核验他人申报的权利、事实核验签署、解除隔离、登记更正 |
| `release_manager` | 商业发布签署、渠道发布与结束、关闭替换任务 |
| `rights_officer` | 登记来源撤回与下架通知 |

## 业务规则与对应接口

- **申报与核验分离**：`declare_rights` 要求申报人即权利人本人（作者可以申报自己的创作，
  不能替他人确认权利）；`verify_rights` 只能由 `fact_checker` 完成，且不能是申报人本人。
  贡献记录 `record_contribution` 同样只能申报自己的贡献。
- **双签署**：剪辑版本发布前需要 `signoff(fact)` 与 `signoff(commercial)`，
  系统强制两类签署由不同人员完成（即使一人兼有双角色也不行）。
- **发布冻结**：`post_release` 冻结该渠道实际采用的画面、声音与说明（`items_override`
  记录平台二剪后的真实条目）以及结构化限定 `notices`——限定作为数据随行，
  平台截掉片尾字幕后仍可在 `explain_release` 中查到。
- **渠道限制**：`ChannelPolicy` 按渠道声明是否允许 AI 音乐、是否要求肖像授权、是否商用；
  发布时逐项校验素材状态、词曲授权与肖像授权的覆盖范围（渠道、商用、期限）。
- **来源撤回**：`withdraw_source` 只阻止未发布的剪辑版本（`REVISION_BLOCKED` 并释放曲目锁），
  对在线发布生成范围明确的替换任务（精确到发布、渠道与冻结条目），已结束活动保留当时证据不动。
- **指纹幂等与隔离**：`register_asset` 对相同指纹且来源与授权范围一致的提交沿用原决定
  （`ASSET_REGISTRATION_DEDUPED`）；指纹相同但来源或授权范围漂移的登记进入隔离
  （`ASSET_QUARANTINED`），由 `resolve_quarantine` 审查后才可使用。
- **曲目原子锁**：`reserve_tracks` 在单个 `BEGIN IMMEDIATE` 事务内检查并写入全部锁，
  任一冲突整体回滚，不会留下部分锁；放弃、发布或阻止版本时自动释放。
- **生成参数不是版权证明**：AI 素材的生成参数只作谱系记录，发布门禁与许可解释
  均不以其替代授权；`explain_*` 输出中标注 `generation_params_rights_proof: false`。
- **后台任务**：替换逾期（`enforce_replacement`）、下架期限（`takedown_enforce`）、
  更正传播（`correction_propagation`）持久化在 `jobs` 表；认领与业务效果同事务提交，
  进程中断后由 `JobWorker.recover_stale` 回收超时任务继续处理，处理器按当前状态幂等，
  已提交的效果不会重复执行。

## 解释接口

- `explain_release(release_id, now)`：从一段成片解释每个条目的来源、授权状态、贡献者、
  冻结说明与限定，并给出 `license_status`（clear / issues）与未完成的替换任务；
  已结束活动同样返回当时证据。
- `explain_asset(asset_id)`：单个素材的登记、授权、贡献与状态流转事件流。

## 运行演示

```bash
PYTHONPATH=src python3 examples/walkthrough.py
```

演示覆盖：渠道拒绝 AI 音乐、发布冻结、来源撤回生成替换任务、中断恢复后处理逾期任务。
