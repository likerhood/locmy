# Chart.js-10157 离线资源修复与恢复

## 范围与证据

失败样本为 `chartjs__Chart.js-10157`。GitHub 上的三个 horizontal PNG 下载超时；
测试只运行 54/1545 项后浏览器断连，另有 coverage 目录 EACCES。
不能将缺失目标失败记录解读为测试通过，也不能把这些问题统称为 Docker 拉镜像失败。

本地 `mycode-addtest4` 的该样本 VLM 图片缓存中找到六张原始 PNG，逐一验证 Git blob
SHA-1 与锁定数据集 test_patch 的新文件 index 前缀一致（不是仅按文件名匹配）。
资源来源 URL 固定到 Chart.js 提交 `368ad3cf70414de7769852cf52693c08aaf9e05c`。
保存为 `data/evaluation_seed/chartjs_assets.json`（Base64，便于校验与版本管理）；
适配器固定整个包 SHA-256，运行时再校验各图片完整 blob SHA-1。
只用于官方评测资源恢复，不加入模型提示词、定位输入或修复上下文。

## 本次修改

- 精确匹配 instance/path/URL 后使用六张离线图片，不再请求网络。
- 其他资源沿用官方加载方式；必需资源缺失立即报基础设施错误，不再静默跳过。
- Chart.js 评测前仅准备容器内 coverage、coverage/html、coverage/chrome 三个目录，
  owner 为 chromeuser；拒绝符号链接，不递归修改仓库或宿主机权限。
- Chart.js 日志解析拒绝缺少执行汇总、最终部分执行、断连、XHR 读取异常或 EACCES。
  正常完整执行且确有断言失败仍由原解析器判定，不改变目标测试、金补丁或成功标准。
- 不修改全局代理、Docker daemon、其他容器、批次超时，不新增跳过样本功能。

浏览器断连的具体根因尚未确定。本次不是承诺完整评测必然通过；服务器需要下面的独立复测。

## 服务器先单独测试（不调用模型，不动旧批次报告）

先同步已提交的代码和资源文件；若使用 Git，确保提交已推送到服务器可访问的远程分支。
在确认旧流水线已经停止后执行：

```bash
cd /data2/like/locmy/experiment_rq4
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
unset DOCKER_CONTEXT
RQ4_ROOT="$PWD"
RUN_ID='swe50v2-qwen-all5-top15-k10-v11-full1'
RQ4_INPUT="$RQ4_ROOT/runs/batches/$RUN_ID/eval_inputs/chartjs__Chart.js-10157"
RQ4_CHECK_DIR=$(mktemp -d "$RQ4_ROOT/runs/chartjs-check.XXXXXX")
printf '独立诊断目录: %s\n' "$RQ4_CHECK_DIR"

(
  cd "$RQ4_CHECK_DIR" || exit
  "$RQ4_ROOT/.venv/bin/python" -u "$RQ4_ROOT/scripts/official_eval.py" \
    --dataset "$RQ4_INPUT/dataset.jsonl" \
    --predictions "$RQ4_INPUT/control_noop.jsonl" \
    --workers 1 --timeout 1800 --run-id "$(basename "$RQ4_CHECK_DIR")" --no-op
)
find "$RQ4_CHECK_DIR" -name report.json -print -exec cat {} \;
```

检查六条 `Using verified bundled evaluation asset`，无缺图警告、EACCES、DISCONNECTED。
无补丁对照预期 `resolved: false`，且不能带基础设施失败；不能仅凭命令退出码判断通过。
若仍有浏览器/JS 读取错误，保留此目录继续诊断，暂不迁移或启动整批。
金补丁对照会在正常续跑时紧接着执行，只有它也通过才会开始该样本的新修复请求。

## 迁移旧批次并续跑

独立复测正常后，先 dry-run：

```bash
cd /data2/like/locmy/experiment_rq4
.venv/bin/python scripts/migrate_chartjs_assets.py \
  --run-id swe50v2-qwen-all5-top15-k10-v11-full1
```

核对输出无误后：

```bash
.venv/bin/python scripts/migrate_chartjs_assets.py \
  --run-id swe50v2-qwen-all5-top15-k10-v11-full1 --apply
```

迁移获取原批次共用锁，只接受已知旧适配器哈希、该样本 no-op 失败、无该样本生成记录、
未标完成、流水线 failed；其他受跟踪源码发生变化一律拒绝。
旧 manifest、暂停/失败记录及该样本 no-op 目录保存在批次 `migrations/` 下，不删除。
只更新适配器哈希；其他参数（包括网络路由和 timeout）保持原值。
不要为了通过检查手工覆盖 manifest。旧版本不匹配时应提供报错供审计。

随后重复原先的完整启动命令，保持同一个 run-id、env-file、limit、methods 等参数。
前 18 个完成标记和所有 API 记录保持不变；该样本重新执行环境对照。
不要另外创建 run-id 来无意重跑整个付费实验，也不要删除 completed_samples 或 records。

## 本地验证

```bash
python3 -m unittest discover -s experiment_rq4/tests -q
```

单元测试覆盖六个官方 blob/URL、离线不调用网络、缺失资源失败、部分测试拒绝、完整测试
沿用原解析器、coverage 设置、迁移 dry-run/归档/保留完成记录。未替代服务器 Docker 真测。
