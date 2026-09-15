# RQ4 下游实验：单目录部署

仓库：`git@github.com:likerhood/locmy.git`；分支：**rq4work**。只推送 mycode 对应的这个仓库即可。`experiment_rq4` 包含服务器所需的实验输入、脚本、环境模板和分析程序，不需要外层 locCode、其他 baseline 工程或 addtest3 工作区。

## 已打包内容

| 路径 | 用途 |
|---|---|
| `normalized/swe/*.jsonl` | 五种方法各 50 例定位输出，供修复读取 |
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

`.env.local` 使用实际服务商支持的 MiMo 模型 ID：

```dotenv
RQ4_BASE_URL=https://你的接口基础地址/v1
RQ4_API_KEY=你的密钥
RQ4_MODEL=实际MiMo模型ID
RQ4_REQUEST_TIMEOUT=180
```

终端同名变量优先于文件；非空 RQ4_MODEL 优先于旧 MODEL_API_NAME。改变的是修复模型，Qwen 定位不重跑。

```bash
# 无收费请求
bash scripts/server_run.sh --mode check --dataset swe

# 一例 MAGNET addtest3 + MiMo，会收费；只生成和检查补丁，不运行官方测试
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

- 只有 50 GB 时，暂勿启动全量 Docker 流程：当前无磁盘限额及自动镜像回收，workers=1 只限制并发。
- 完整评测仍需官方元数据兼容性、Docker 与 gold/no-op 对照验证；MiMo 接口需要实际试跑。
- 修复器为 Agentless-inspired 独立编辑器，不是官方 Agentless-1.5 原样运行。协议为完整文件、72,000 UTF-8 字节上限、单补丁、无反馈迭代。
- Omni 最新结果尚未齐备，本包不宣称能完成两个数据集的正式实验。
- 更新定位输入或模型后使用新 run-id，不混用旧结果。

输出：`runs/batches/<run-id>/`，含补丁、逐例记录、官方报告（如已测试）、`analysis.md`、`analysis.json`、`per_instance.csv`。缺少官方判定时保留 unknown，不能把补丁可应用当成解决。

完整执行和分析命令见[一键运行与结果分析](一键运行与结果分析.md)。历史本地试跑见[运行环境与首次修复记录](运行环境与首次修复记录.md)，不代表当前 MiMo 或官方评测已跑通。
