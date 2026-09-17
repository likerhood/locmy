# RQ4 下游实验：单目录部署

当前串行版本已实现仓库复用、Git 镜像回退、镜像空间管理和逐样本评测，运行以[串行评测与50GB部署](串行评测与50GB部署.md)为准。下文旧版本限制如有冲突，以新说明为准。
仓库：`git@github.com:likerhood/locmy.git`；分支：**rq4work**。只推送 mycode 对应的这个仓库即可。`experiment_rq4` 包含服务器所需的实验输入、脚本、环境模板和分析程序，不需要外层 locCode、其他 baseline 工程或 addtest3 工作区。

## 已打包内容

| 路径 | 用途 |
|---|---|
| `normalized/swe/*.jsonl` | 五种方法各 50 例文件排序与函数定位输出，供细粒度定位和修复读取 |
| `normalized/omni/magnet.jsonl` | 历史 Omni MAGNET 50 例；四个 baseline 尚缺 |
| `data/inputs/*50.jsonl` | 修复任务输入，只有 ID、仓库、基础提交、问题描述 |
| `data/evaluation_seed/*.jsonl.gz` | 隔离评测记录，含 gold；只供恢复评测数据，不进入修复提示词 |
| `manifests/` | 固定 50 例名单和候补顺序 |
| `configs/` | 协议、文件哈希和上游版本锁定 |
| `scripts/` | 校验、初始化、修复、官方评测、结果分析 |
| `.env.example` | API 配置模板；实际密钥不提交 |
| `reports/source_audit_20260915.json` | 最新 SWE 导出的来源、旧新哈希和变化数量 |

SWE MAGNET 已替换为用户指定的 addtest3（运行记录 git SHA 9cef95a，API 模型 ID xopqwen35397b）。LocAgent 使用本机 v2，GALA 使用 Clean15-92；CoSIL 和 GraphLocator 沿用本机尚未找到更新版本的 Qwen397 来源。原 50 例及顺序不变。详见[定位来源与分支操作](定位来源与分支操作_20260915.md)。

`configs/sources.json` 记录原始来源路径，只供审计或维护者重新导出；服务器运行不访问这些路径。不要在服务器执行 `scripts/prepare.py`。大型原始轨迹和 baseline snapshots 不需要上传。

## 服务器从零开始

```bash
git clone --depth 1 --branch rq4work --single-branch git@github.com:likerhood/locmy.git locmy-rq4
cd locmy-rq4/experiment_rq4
python3 scripts/verify_bundle.py
bash scripts/server_setup.sh
nano .env.local
chmod 600 .env.local
```

初始化会校验输入、建立 Python venv、下载锁定版本 Agentless、从本目录压缩记录恢复 `data/evaluation_only/` 并预检；不收费。Python 需要 3.10+，建议 3.12；模型走 API，无需本地 GPU。Git、venv 支持和上游下载网络需服务器提供。输入完整不代表 Docker 和官方评测已可用。

`.env.local` 使用实际服务商支持的模型 ID：

```dotenv
RQ4_BASE_URL=https://你的接口基础地址/v1
RQ4_API_KEY=你的密钥
RQ4_MODEL=服务商实际模型ID
RQ4_REQUEST_TIMEOUT=900
RQ4_HTTP_RETRIES=2
RQ4_HTTP_RETRY_SLEEPS=10,30
# 服务器代理会破坏该 API 的 TLS 时启用；只让上述 API 主机直连
RQ4_API_DIRECT=1
# 自动禁用 GitHub 镜像，并让 GitHub 绕过继承的 HTTP(S) 代理
RQ4_GITHUB_DIRECT=1
```

终端同名变量优先于文件；非空 RQ4_MODEL 优先于旧 MODEL_API_NAME。改变的是修复模型，Qwen 定位不重跑。

```bash
# 无收费请求
bash scripts/server_run.sh --mode check --dataset swe

# 一例 MAGNET + 所选模型，会发起 1 次细粒度定位和 10 次候选修复请求；
# 只生成、选择和检查补丁，不运行官方测试
bash scripts/server_run.sh --mode generate --dataset swe --methods magnet --limit 1 --run-id swe-addtest3-mimo-api-smoke-v1
```

目标项目源码由批量脚本按需下载到 `repos/`。因此“单目录部署”不表示完全离线：仍需模型服务、GitHub、评测数据和容器镜像网络。直接调用 `repair.py` 时必须提供 `--repo`，不再自动搜索外层 mycode/LocAgent 缓存。

## 已有服务器仓库

在 locmy 仓库根目录检查本地修改后：

```bash
git status --short
git fetch origin
git switch rq4work
git pull --ff-only origin rq4work
cd experiment_rq4
python3 scripts/verify_bundle.py
```

本地没有分支时使用 `git switch --track origin/rq4work`。不要切 main 来运行 RQ4；不必切 addtest3。切换前保留已有改动，不使用强制覆盖。

## 当前限制和结果

- 已有串行调度与空间监测，默认预留 10 GiB；50 GB 下仍需单例实测环境峰值，详见新部署说明。
- 完整评测仍需官方元数据兼容性、Docker 与 gold/no-op 对照验证；所选模型接口需要实际试跑。
- Rootless Docker 可能让镜像源码出现只有权限变化的 Git `M` 标记。RQ4 在官方 `eval.sh` 执行前，只在该测试容器的 `.git/config` 设置 `core.filemode=false`。锁定的 evaluator 压缩快照保持逐字节不变用于来源审计；生成本地 evaluation-only 数据时，一个严格匹配的适配器会删除其中对 `node_modules` 的冗余递归 `chmod`，因为锁定 harness 以 root 运行，而该元数据改写会在 overlay2 上耗尽超时。依赖安装仍只在 `package.json` 有真实内容变化时执行，现在会保留输出、记录阶段时间，并在失败时明确停止，不再吞掉错误。gold patch、test patch、测试命令、F2P/P2P 集合和 resolved 判定均未改变。此 evaluator 适配变化需要新的 run-id，并须重新验证 no-op/gold 对照。
- Automattic Calypso 的锁定版解析器可能把 shell 跟踪或补丁正文误当作 Jest suite，给测试 ID 加入无关前缀或挤掉最外层 suite。RQ4 对每个官方 F2P/P2P ID 选择最长的完整层级后缀；通常使用 `#函数 + 测试描述`，外层 suite 和函数层都被挤掉时才允许唯一的描述级匹配。只有全部期望 ID 都唯一、一一对应时才规范化；缺失、重复或歧义均保持失败。原始 `test_output.txt`、测试状态和官方评测脚本保持不变。解析器兼容处理需要新的 run-id 和 no-op/gold 重验；不能把先前的 gold 对照失败改写为通过。
- 细粒度定位要求纯 JSON；为兼容部分 OpenAI 兼容模型，也接受且仅接受一个可独立解析的 `json` Markdown 代码块。下游修复使用 CoSIL 风格的英文推理加 SEARCH/REPLACE 块，并保留旧 JSON `edits` 作为兼容输入。响应格式、结束原因和失败阶段写入候选记录；格式失败不会作为同一收费请求自动重发。若所有修复候选都因 API 基础设施失败且没有可评测补丁，该方法样本标为 `infrastructure_failure`，分析结果保持 `unknown`，不计作未解决。
- 下游流程按 CoSIL RQ3 的结构适配到所选模型：每种方法先保留最多 Top-15 文件及其上游函数排序，再执行一次共享的函数/行区间定位；修复上下文只包含这些区间及前后各 10 行。随后生成 10 个候选（1 个 temperature=0，9 个 temperature=0.8），按归一化补丁投票和稳定破平规则选择，最终只评测一个补丁。
- `Top-15` 表示使用上游实际提供的前 15 名。CoSIL 历史快照只有 5 个文件、GALA 某些样本少于 15 个时，程序使用全部已有项，不补造排名。每条 `normalized` 记录同时保存 `found_files` 和 `found_functions`；上游函数为空时，共享细粒度定位阶段从候选文件函数定义清单中选择。
- 这不是逐字复刻 CoSIL 的 `patch_gen.sh`：模型由所选 env 文件决定，修复输出采用 CoSIL SEARCH/REPLACE，候选选择使用归一化去重多数票；当前不运行 CoSIL 的模型生成 reproduction/regression 测试，避免额外测试生成质量成为五种定位方法之间的混杂变量。no-op/gold 对照和最终官方 SWE-bench 测试保持不变。
- 每个样本方法最多产生 11 次收费请求，所以 1 样本 × 5 方法最多 55 次，50 样本 × 5 方法最多 2750 次。`--mode check` 会在收费前打印精确计划数。每次请求独立记录在 `attempts/<method>/<instance>/paid_calls/`；只有完整响应可自动复用。明确的 429/500/502/503/504 按 `RQ4_HTTP_RETRIES` 有界重试。单次请求默认等待 900 秒；修复候选若发生结果不确定的超时或连接失败，会保留现场、记为 `failed:api_request` 并继续后续候选，不重发同一候选。共享细粒度定位失败仍停止该运行，因为缺少定位上下文时继续会改变实验协议。
- Omni 最新结果尚未齐备，本包不宣称能完成两个数据集的正式实验。
- 更新定位输入或模型后使用新 run-id，不混用旧结果。

若源码 Git 镜像和 GitHub 克隆都很慢或失败，但同一题的官方评测镜像已本地缓存，可在**流水线停止后**运行 `python3 scripts/cache_repo_from_image.py --run-id <run-id> --instance-id <instance-id>`。该工具只从此批次锁定摘要的镜像 `/testbed/.git` 导入 Git 对象，核验历史 `base_commit` 和至少一个定位候选文件后，原子地建立 `repos/<owner>__<name>/`，并记录 `.rq4-source.json` 来源。不会复制镜像当前工作树、gold 补丁或测试结果。成功后重复相同流水线命令即可复用既有 no-op/gold 报告；若镜像缺少所需 Git 对象，工具会停止且不建立缓存。

输出：`runs/batches/<run-id>/`，含补丁、逐例记录、官方报告（如已测试）、`analysis.md`、`analysis.json`、`per_instance.csv`。缺少官方判定时保留 unknown，不能把补丁可应用当成解决。

### 网络路径与镜像配置

RQ4 涉及四条相互独立的网络路径，不能用一项“镜像”配置替代全部路径：

- 模型 API 由 Python 进程访问，默认继承 shell 的 `HTTP(S)_PROXY`。若代理对 API 主机产生 TLS EOF，在所选 env 文件设置 `RQ4_API_DIRECT=1`；加载配置时只把 `RQ4_BASE_URL` 的主机追加到 `NO_PROXY` 和 `no_proxy`，无需每次手工 export。该选择写入批次 manifest，改变设置后必须使用新 run-id。
- GitHub 源码默认使用 `RQ4_GITHUB_DIRECT=1`：加载 env 时会覆盖继承的镜像设置、将 `RQ4_GITHUB_MIRROR_PREFIX` 清空，并把 GitHub 下载主机追加到 `NO_PROXY`/`no_proxy`。配置由 pipeline 进程传给 setup、仓库缓存和修复子进程，不需要在终端手工 `export`；它不会修改父终端。若某台机器必须使用镜像，可显式设置 `RQ4_GITHUB_DIRECT=0` 和 `RQ4_GITHUB_MIRROR_PREFIX=https://...`。每次 clone/fetch 的真实错误保存在 `runs/batches/<run-id>/repo_clone.log`。评测镜像已经包含同一提交时，也可按上文工具核验后导入 Git 对象。该选择写入批次 manifest，改变后必须使用新 run-id。
- Python 包和 Hugging Face 数据由宿主 Python/pip 下载，使用各自的 index、endpoint、代理或 `NO_PROXY` 配置；Docker registry mirror 对它们无效。
- Docker Hub 镜像由 rootless Docker daemon 拉取，使用该 daemon 的 `daemon.json` registry mirror 或 systemd 代理。只有修改 daemon 配置后才需重启 Docker；普通实验重跑不需重启。
- Docker 拉取默认最多尝试 3 次（`RQ4_IMAGE_PULL_RETRIES`），每次最长 1800 秒（`RQ4_IMAGE_PULL_TIMEOUT`）。失败后 Docker 已完成的层会被下一次尝试复用，尝试与失败原因同时写入 `image_pull.log` 和 `resources.jsonl`。

### CoSIL RQ3 对齐的下游修复协议

完整的数据流、超参数逐项对比、候选失败语义和审计文件说明见[CoSIL RQ3 下游修复对齐协议](CoSIL_RQ3下游修复对齐协议.md)。

当前协议 `cosil_rq3_repair_top15_k10_v5` 将每种上游方法的前 15 个可用文件及函数定位交给一次细粒度函数/行定位，再把选中区间前后各 10 行交给同一修复器。修复器使用英文 CoSIL 风格的先分析、后 SEARCH/REPLACE 输出协议，生成 1 个 temperature=0、top_p=1 的贪心候选和 9 个 temperature=0.8、top_p=1 的采样候选。细粒度定位使用 temperature=0.8、top_p=1。定位与修复的最大输出均为 8192 token，以容纳 Qwen3.5 推理输出；这是面向当前模型的兼容设置，不等于原 CoSIL 对所有模型都采用相同预算。v5 将结果不确定的修复请求固定记为基础设施候选失败，不重发该候选并继续后续候选。

每个候选必须同时满足：编辑文件来自细粒度上下文；SEARCH 文本出现在模型看到的代码中，并在完整原文件中唯一匹配；修改非空；Python/JSON 文件通过标准库语法解析。有效候选按修改后文件内容归一化，先按同一补丁的票数选择，再以修改行数和候选编号稳定破平。只有最终候选进入一次官方评测。所有候选的原始 provider response、解析错误、校验结果、归一化摘要和选择依据均保存在对应 attempt 目录。

原版脚本向修复模型请求 20 个样本，但其公开候选测试和排序路径只消费编号 0–9。当前实验直接生成这 10 个实际进入选择阶段的候选，避免额外购买 10 个随后被丢弃的响应。这里的候选 `generated` 只表示响应成功解析、编辑可唯一应用并通过静态检查；它不表示 SWE-bench 测试已经解决。`failed` 会进一步记录失败发生在 `api_request`、`response_parse`、`edit_application` 或 `static_validation`，方便区分网络响应丢失、输出被截断、SEARCH 不唯一和语法无效等原因。`analysis.md` 单列汇总基础设施候选失败，正式比较时必须披露候选数不足的样本。

原版 CoSIL RQ3 还用独立生成的 reproduction/regression tests 过滤候选。当前 SWE/Omni 冻结数据没有与五种定位方法共享且经过验证的独立生成测试集，因此本协议不会拿官方 F2P/P2P 测试做候选选择；这样避免使用最终评测证据进行 rerank。该差异写入 `configs/protocol.json` 的 `rerank_test_policy`，不能把本协议描述为逐行复刻原版测试 reranker。若后续加入候选无关、按样本冻结的生成测试，必须给测试来源和模型调用单独留痕、先在 base/gold 对照验证，并使用新协议名和新 run-id。

因此，Docker 镜像拉取成功不代表 Git、PyPI、Hugging Face 或模型 API 一定可达；排错时应先确认失败属于哪条路径。

完整执行和分析命令见[一键运行与结果分析](一键运行与结果分析.md)。历史本地试跑见[运行环境与首次修复记录](运行环境与首次修复记录.md)，不代表当前 MiMo 或官方评测已跑通。

### 独立 Docker 服务与存储检查

共享服务器可使用独立 Rootless Docker，将其数据目录配置到 `/data2`。
先完成该服务的安装与验证；仅设置下面的变量不会安装服务或迁移数据。
启动 RQ4 前，在同一个终端指定实际 socket：

```bash
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock
docker info --format 'Root={{.DockerRootDir}} Driver={{.Driver}} Security={{json .SecurityOptions}}'
python3 scripts/storage_check.py --min-free-gb 10
```

确认 Root 位于自己的 `/data2` 目录、Security 包含 rootless，再运行 pipeline。
`DOCKER_HOST` 也可放在 `--env-file` 指定的配置中；独立运行 storage_check.py 时需在 shell 中 export。
RQ4 对齐 Docker CLI 与 Python SDK 的服务 ID，并将 socket 传给子进程。
未设置 DOCKER_HOST 时明确使用 `/var/run/docker.sock`，不跟随 CLI 保存的 context。

overlay2/fuse-overlayfs 检查所连接服务的 DockerRootDir，不再无条件检查系统
`/var/lib/containerd`。如果检测到 containerd snapshotter，必须设置
`RQ4_CONTAINERD_DATA_ROOT` 为管理员或服务配置确认的实际数据目录；不能为通过检查而填任意目录。
这些设置不重启、不迁移系统 Docker，也不操作其他人的容器。

### 流水线进度、日志与中断

`server_pipeline.sh` 现在自动保存终端 stdout/stderr 到
`runs/pipeline/<run-id>/pipeline.log`，同一 run-id 重启时追加日志。
四个阶段分别是存储检查、基础准备、评测工具安装、修复与测试。
每 30 秒显示子进程 PID 和耗时；心跳只表示进程存活，不证明下载或测试有进展。
`runs/pipeline/<run-id>/status.json` 每 30 秒刷新，记录 running/stopping/completed/failed/interrupted。
进程被 SIGKILL 或服务器断电时无法写最终状态，所以要结合更新时间及实际进程检查。

```bash
# 在 experiment_rq4 目录，用自己的 run-id 替换下面的值。
tail -f runs/pipeline/swe-mimo-rootless-smoke-v1/pipeline.log
cat runs/pipeline/swe-mimo-rootless-smoke-v1/status.json
# 安装工具的详细输出仍在这里；主日志会提示这个路径。
tail -f reports/setup/pip.log
```

Ctrl+C 或给 supervisor 发 SIGTERM 会请求停止该流水线的进程组，等待子程序清理后记录中断。
不要用 kill -9 作为日常停止方法。看到 interrupted/failed 后先检查错误，恢复时使用相同参数和 run-id。
已有 API 请求不自动重试，无法保证任何中断位置都能无人工处理恢复。
同一 run-id 的 supervisor 不允许重复启动；实验缓存原有锁仍生效。
此日志功能不是后台运行工具，长任务仍建议使用 tmux。
实验结果继续位于 `runs/batches/<run-id>/analysis.md` 等原有文件中。
本次只改流水线包装与输出，不改变已有实验 manifest 的修复代码哈希。

### SWE 50 样本修订 v2（2026-09-15）

官方锁定 dev 数据缺少 `chartjs__Chart.js-8650`，现替换为同仓库的
`chartjs__Chart.js-10806`：选择原 candidate_order 中第一个未选、官方存在且五种定位齐全的候选，
没有参考补丁修复结果。五种 normalized 输入、CSV 清单和评测 seed 同步更新。
此外，`diegomura__react-pdf-1552` 的历史 FAIL_TO_PASS 比官方多一个测试文件；
现采用锁定官方版本的列表，历史值及理由保存在 `reports/sample_replacement_20260915.json`。
标准补丁、测试补丁、base_commit 未被修改；原有 49 条定位输入不变。

`configs/official_snapshot.json` 记录上游 parquet 及打包记录的 SHA256 和 revision。
`prepare_harness_dataset.py` 优先加载打包的官方 50 条记录，验证哈希并逐项核对本地 seed，
无需联网 Hugging Face；不跳过后续无补丁/标准补丁环境对照。
restore 脚本仅对已知旧 seed 哈希自动备份迁移，未知内容拒绝覆盖。

更新后先运行：

```bash
python3 scripts/verify_bundle.py
python3 scripts/restore_eval_data.py
python3 scripts/prepare_harness_dataset.py
```

本次样本/测试协议改变，必须使用新 run-id，例如 `swe50v2-mimo-smoke-v1`。
不要覆盖原批次结果；即使只跑一个样本，输入文件哈希也已改变。

### 直观进度显示

新版流水线每 30 秒显示当前阶段、活动子进程 PID、当前样本及方法/对照、已完成样本数。
官方测试阶段自动读取当前样本的 harness/test 日志末尾两行，并显示距上次写入的秒数。
下载阶段显示 image_pull.log，安装阶段显示 pip.log。补丁生成阶段不回显模型响应。
详细信息同时写入 pipeline.log 和 status.json 的 progress 字段。
进度通过 Linux /proc 子进程树只读识别，不修改修复/评测脚本或实验 manifest 哈希。
“官方测试”表示评测进程存在，具体是在启动容器还是运行测试要结合下方日志判断；
完成样本数以 completed_samples 标记为准，不把 0/1 进度解释成通过率。

旧版已运行的任务无需中断：更新后在另一个终端执行以下命令也能查看当前步骤：

```bash
python3 scripts/progress_view.py --run-id swe50v2-mimo-smoke-v1
# 持续刷新（Ctrl+C 只停止查看）
watch -n 5 python3 scripts/progress_view.py --run-id swe50v2-mimo-smoke-v1
```

新启动的 server_pipeline.sh 自动显示这些内容，命令参数不变。
旧任务的终端不会热更新，使用独立查看命令即可。若服务器重启或进程被强杀，
旧 status.json 可能残留 running，应结合更新时间与 PID 判断。

Progress display uses English labels. Each refresh shows one preferred step log with
its basename and age; unchanged log excerpts are omitted by the pipeline supervisor.
Excerpts are limited to 180 characters and embedded paths are abbreviated. Full log
paths and excerpts remain available in status.json; original log files are unchanged.
