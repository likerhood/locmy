# 评测异常后继续调度（不是忽略错误判分）

## 语义

图片缺失、浏览器启动失败和断连会破坏当前样本的评测有效性。本功能隔离该样本，
让其他样本继续；不声称异常不影响评测，不关闭 Firefox、不删测试、不伪造 resolved。

新增 `--eval-failure-policy defer`，默认仍为 `stop`。启用后：

- 无有效报告、评测子进程失败/超时、报告明确 infra_failure、环境对照与预期不符：
  整个样本标记为 `deferred_evaluation`，保存原因并保留原始日志和已生成补丁。
- 对照异常时不发起该样本新的模型调用；方法评测异常时停止该样本剩余方法。
- 普通有效测试的 resolved=false 是正常评测结论，不是需要暂缓的错误。
- 进程重启后不自动重跑已暂缓样本，也不重试已开始的付费请求。
- 为保持配对比较一致，暂缓样本所有方法均为 unknown，仍保留实际 token 消耗。
- 默认连续 3 个评测异常停止，避免系统性故障令整个数据集被略过。
  `--max-consecutive-eval-failures` 和策略纳入批次清单，不能续跑时随意修改。
- 磁盘不足、镜像/daemon 故障、配置不一致、未知代码异常仍停止，不盲目吞异常。
- 调度结束而存在暂缓样本时显示 `completed_with_deferred`，不表示全部评测完成。

`deferred_samples/` 是待补测清单，不是 `completed_samples/`。完整评测仍需修复环境后，
显式审计、归档对应旧评测缓存并重新开放样本；不要手工将 deferred 改成 completed。

## 服务器恢复当前批次

先确认旧流水线停止，拉取同一分支；如果有本地改动或分支不同，不强制覆盖。

```bash
cd /data2/like/locmy
git status --short
git branch --show-current
git pull --ff-only origin rq4work
cd experiment_rq4
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
unset DOCKER_CONTEXT

.venv/bin/python scripts/migrate_deferred_evaluation.py \
  --run-id swe50v2-qwen-all5-top15-k10-v11-full1
```

dry-run 成功后执行（不要重复执行旧的 migrate_chartjs_assets.py）：

```bash
.venv/bin/python scripts/migrate_deferred_evaluation.py \
  --run-id swe50v2-qwen-all5-top15-k10-v11-full1 --apply
```

该工具只接受已知旧版本和明确的 Chart.js 断连证据，持有 supervisor 与 batch 两把锁。
备份旧 manifest/暂停记录，然后更新调度器和分析器哈希、固定 defer/3 策略，
仅将 Chart.js-10157 标记为暂缓；不移动/删除任何测试证据或 API 记录。
如果哈希不匹配或显示 Already migrated，停止并检查，不篡改清单。

沿用原先参数，只加 defer 策略：

```bash
RUN_ID='swe50v2-qwen-all5-top15-k10-v11-full1'
LOG="runs/pipeline/$RUN_ID/pipeline-resume-deferred.log"
nohup env \
  -u RQ4_BASE_URL -u RQ4_API_KEY -u RQ4_MODEL \
  -u BASE_URL -u API_KEY -u MODEL_API_NAME -u MODEL_NAME \
  RQ4_IMAGE_PULL_TIMEOUT=2300 \
  RQ4_IMAGE_PULL_RETRIES=3 \
  RQ4_IMAGE_PULL_RETRY_DELAY=30 \
  bash scripts/server_pipeline.sh \
  --env-file ../.envaliyun-qwen.local \
  --dataset swe --limit 50 --run-id "$RUN_ID" \
  --min-free-gb 10 --image-cache sample \
  --eval-failure-policy defer --max-consecutive-eval-failures 3 \
  >> "$LOG" 2>&1 < /dev/null &
echo "后台进程 PID=$!"
```

若上次运行实际更改了 timeout、methods 等参数，必须保持那次的值，否则清单拒绝续跑。

```bash
watch -n 5 'python3 scripts/progress_view.py --run-id swe50v2-qwen-all5-top15-k10-v11-full1'
```

看见 `Skip deferred evaluation chartjs__Chart.js-10157` 后应调度下一样本。
已完成样本的标记保留。最终查看 `analysis.json` 的 coverage/deferred_samples，
以及 `per_instance.csv` 的 evaluation_deferred/deferred_reason；unknown 不能充当失败或成功。

## 验证范围

离线单元/集成模拟覆盖：异常对照跳到下一样本、方法评测异常后保留生成记录、重启不重复调用、
正常失败与基础设施错误区分、连续失败保护、未知异常仍停止、配对 unknown 与费用保留、
迁移 dry-run/保留证据、结束状态 completed_with_deferred。
尚未在用户服务器的真实 Docker/浏览器中执行，不保证所有剩余样本环境正常。
