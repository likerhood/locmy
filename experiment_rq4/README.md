# RQ4 下游修复实验工作区

**服务器运行入口：见 [一键运行与结果分析](一键运行与结果分析.md)。目标分支为 `rq4work`。** 已增加批量生成、断点记录、官方SWE环境对照/测试接口和逐例/配对分析；当前本机无Docker，完整官方评测仍需服务器验收。

更新：已复制 mycode/.env.local 并兼容其变量名，完成一次真实API补丁生成与隔离应用检查。电脑配置、实际耗时/token、运行命令及Docker后续步骤见 [运行环境与首次修复记录](运行环境与首次修复记录.md)。下文的未调用模型状态描述属于初次准备阶段，以本记录为准。

本目录按用户指定创建，使用既有 adapted baseline 的 Qwen3.5-397B-A17B 定位结果。不增加 Agentless-FL 定位实验，不把不同方法结果合并，也不重跑已经可复用的定位。

## 已准备的内容

- `vendor/Agentless/`：从 https://github.com/OpenAutoCoder/Agentless.git 克隆的官方上游源码；固定提交见 `configs/vendor_lock.json`。没有修改上游源码。
- `snapshots/swe/`：四种 baseline 原始定位结果快照，可能包含 gold，仅供审计，不能传给修复模型。
- `normalized/{swe,omni}/`：每方法各 50 条白名单定位输出，字段仅 instance_id/found_files/status。
- `manifests/`：50 例候选清单以及按仓库排序的候补；尚未进行环境正负控，不能称正式冻结测试集。
- `data/inputs/{swe,omni}50.jsonl`：只有 instance_id/repo/base_commit/problem_statement，修复程序只读取这里。
- `data/evaluation_only/{swe,omni}50.jsonl`：完整50例，含 patch/test_patch/测试标签，隔离供评测。
- `configs/sources.json`：每种方法的来源文件和预测字段；Omni 缺失的同模型 baseline 显式为 null。
- `reports/preparation.json`：数据/预测来源 SHA-256、覆盖数、缺失项。
- `reports/readiness.json`：当前预检状态，不会发 API 请求。

MAGNET 从精简的 agent_traces.jsonl 提取 ranked.files 并记录来源 SHA，不复制大型原始结果。源码 trace_recorder 将最终 ranked_locations 前15项按原序导出到该字段；另按 instance_id 与每例 CSV 的 Top-5 交叉验证，结果见 reports/magnet_top5_parity.json。SWE 使用 front_rank_v1，Omni 使用历史 r4；两者不能直接作为同版本正式主表，暂作为开发素材。

## 环境与命令

所有命令从本目录执行：

```bash
cd /home/like/locCode/alltry/mycode/experiment_rq4
bash scripts/setup.sh
.venv/bin/python scripts/preflight.py
```

本次已创建独立 `.venv`。准备、预检、开发修复脚本仅使用 Python 标准库及 Git，无需安装整个 Agentless 依赖栈；这不代表官方 Agentless 或 SWE-bench harness 已安装可运行。上游依赖原样保留在 `vendor/Agentless/requirements.txt`，正式 harness 应另建并锁定其环境。

配置 `.env.local` 中 RQ4_BASE_URL（通常包含服务的 /v1 前缀）、RQ4_API_KEY、RQ4_MODEL。不要把整份既有 `.env.mimo1.local` 直接当成 Qwen 配置；确认实际修复模型后填写。脚本不打印密钥，不自动导入其他实验凭据，也不将密钥写入请求记录。

重新导出数据（会覆盖本目录生成的准备产物；不覆盖源结果）：

```bash
.venv/bin/python scripts/prepare.py
```

已改用约62MB的 Omni MAGNET精简轨迹，避免反复扫描19GB完整结果。将来补齐 Omni 同模型 baseline，只需更新 sources.json 为真实路径和字段再导出。不要把 Qwen3-VL-8B 结果改名成 Qwen3.5。

## 已验证的离线预演

```bash
.venv/bin/python scripts/repair.py \
  --dataset swe --method locagent \
  --instance-id markedjs__marked-1535 \
  --repo /home/like/locCode/LocAgent/repo_newtest_swebench_multimodal-full-dev/markedjs_marked
```

默认只读取指定 base_commit 源码并保存 request.json，不发模型请求、不修改仓库。即使该本地仓库当前 HEAD 不同，也使用 `git show base_commit:path` 获取冻结源码，不执行 checkout/reset，因此不会干扰定位任务。该实例属于候选清单，离线请求检查不用于调优修复成功率；后续若用它调 prompt，应从正式集合移出并按候补协议替换。

配置 API 后，给同一命令添加 `--execute` 才调用一次真实模型。输出在 `runs/<dataset>/<method>/<instance>/<unique-run>/`，包含 request.json、response.json（若有）、prediction.jsonl；没有自动多次尝试、测试反馈或挑选最佳结果。不要反复试同一个正式实例后仅保留成功记录。

**开发修复入口的准确定位：** 当前 scripts/repair.py 是独立编写的语言无关 JSON SEARCH/REPLACE smoke adapter，借鉴 Agentless 的编辑流程，没有调用官方 Agentless Python API。不能在论文中将它标成未修改的 Agentless-1.5。它可生成多文件 diff；目前拒绝新文件和超出上下文的编辑。正式协议中的新文件支持、tokenizer-aware 24k packing 尚待实现/验收。

开发版使用72,000 UTF-8字节的完整源码上限，不将其冒充24k token。遇到大文件直接明确停止，不偷偷截取文件开头。原始 issue 保持既有输入，SWE 三份 Compact 块的去重与证据来源仍需正式统一审计。没有宣称 input JSON 白名单能够排除问题文本内部所有潜在泄漏。

## 评测

Agentless 是修复参考框架，官方解决率由 SWE-bench Multimodal / OmniGIRL 对应 harness 判定。当前环境 Docker WSL 接口不可用，独立环境也未安装 SWE-bench。不能仅凭生成 diff 或 `git apply` 成功宣称 resolved。

先为最终候选跑通 no-op/gold controls、锁定数据 revision、镜像 digest 和测试超时，然后接入官方评测。评测程序输入字段通常为 instance_id/model_name_or_path/model_patch；开发输出额外字段只供审计，可白名单导出。

已经得到官方逐实例 `report.json` 后，使用外层汇总维持50分母：

```bash
.venv/bin/python scripts/summarize.py \
  --dataset swe \
  --reports /absolute/path/to/one_method_one_attempt_reports \
  --output reports/swe_locagent_eval.json
```

只传一种方法、一个候选轮次的目录，脚本遇到重复实例报告会拒绝，避免把多次尝试拼成 oracle 成绩。缺失报告单列，当前保守 resolved% 的分母仍是50，不把未完成评测说成完整结果。

## 测试与当前限制

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖 gold 字段隔离、50唯一ID、非法路径、歧义替换，以及 JS/Java 多文件补丁实际 git apply 后内容与末尾换行保持。

启动结论：SWE 已通过离线请求构建、一次真实API补丁生成和隔离应用检查。完整50+50正式实验尚缺 Omni 四个同模型 baseline、统一MAGNET版本、可用Docker与官方harness、正式上下文策略和环境正负控。尚未运行官方解决率测试，也未自动启动新一轮定位。
