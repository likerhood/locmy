#!/usr/bin/env python3
"""Read-only progress view; inspect live descendants without changing experiment identity."""
import argparse
import json
import os
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[1]


def descendants(pid):
    pending=[pid]; seen=set(); result=[]
    while pending:
        current=pending.pop()
        if current in seen: continue
        seen.add(current)
        try:
            proc=Path('/proc')/str(current)
            argv=[v.decode(errors='replace') for v in (proc/'cmdline').read_bytes().split(b'\0') if v]
            result.append((current, argv))
            # Thread-created subprocesses may belong to a non-main thread.
            for task in (proc/'task').iterdir():
                pending.extend(int(x) for x in (task/'children').read_text().split())
        except (OSError, ValueError):
            continue
    return result


def argument(argv, flag):
    try: return argv[argv.index(flag)+1]
    except (ValueError, IndexError): return ''


def describe(argv):
    names={Path(x).name for x in argv}
    if 'official_eval.py' in names:
        pred=Path(argument(argv,'--predictions'))
        method=pred.stem; instance=pred.parent.name
        label={'control_noop':'无补丁环境对照（预期未解决）', 'control_gold':'标准补丁环境对照（预期解决）'}.get(method,'官方补丁测试')
        return 90, label, instance, method
    if 'repair.py' in names:
        return 100,'生成补丁（等待模型或处理响应）',argument(argv,'--instance-id'),argument(argv,'--method')
    if 'check_patch.py' in names:
        out=Path(argument(argv,'--run-dir'))
        return 100,'检查补丁能否应用',out.name,out.parent.name
    if argv and Path(argv[0]).name=='docker' and 'pull' in argv:
        return 95,'拉取官方测试镜像','',''
    if argv and Path(argv[0]).name=='git': return 80,'获取或准备代码仓库','',''
    if '-m' in argv and 'pip' in argv: return 80,'安装 Python 评测依赖','',''
    if 'prepare_harness_dataset.py' in names: return 80,'准备并校验评测数据','',''
    if 'run_batch.py' in names: return 30,'调度样本／整理结果','',''
    if 'install_harness.py' in names: return 20,'准备 SWE-bench 评测工具','',''
    return 0,'基础准备或阶段切换','',''


def tail(path):
    try:
        with path.open('rb') as f:
            f.seek(0,2); size=f.tell(); f.seek(max(0,size-8192)); raw=f.read().decode(errors='replace')
        lines=[x.strip() for x in re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',raw).replace('\r','\n').splitlines() if x.strip()]
        text=' | '.join(lines[-2:])[:500]
        # Do not echo signed URLs, URL credentials or bearer values in live summaries.
        text=re.sub(r'https?://\S+', '[URL]', text)
        text=re.sub(r'(?i)(bearer\s+)\S+',r'\1[REDACTED]',text)
        return {'path':str(path),'age_seconds':max(0,int(time.time()-path.stat().st_mtime)), 'tail':text}
    except OSError: return None


def snapshot(root, directory, pid):
    batch=root/'runs/batches'/directory.name
    processes=descendants(pid)
    ranked=[(*describe(argv), p) for p,argv in processes]
    _,stage,instance,method,active_pid=max(ranked,key=lambda x:x[0]) if ranked else (0,'未发现进程，检查最终状态','','',None)
    data=dict(stage=stage,instance=instance,method=method,active_pid=active_pid,completed_samples=0,total_samples=None,logs=[])
    try:
        manifest=json.loads((batch/'manifest.json').read_text())
        data['total_samples']=len(manifest['instance_ids'])
        data['completed_samples']=sum((batch/'completed_samples'/f'{i}.json').exists() for i in manifest['instance_ids'])
    except (OSError,ValueError,KeyError): pass
    candidates=[]
    if method and stage.startswith(('无补丁','标准补丁','官方补丁')):
        folder=batch/'evaluation'/method
        if folder.exists():
            candidates=[p for p in folder.rglob('*') if p.is_file() and p.suffix in ('.log','.txt')]
            # Avoid displaying a different sample's old test output.
            candidates=[p for p in candidates if instance in str(p.relative_to(folder)) or p.name=='harness.log']
    elif stage=='拉取官方测试镜像': candidates=[batch/'image_pull.log']
    elif stage=='安装 Python 评测依赖': candidates=[root/'reports/setup/pip.log']
    # Model response files are deliberately not echoed.
    entries=[v for p in candidates if (v:=tail(p))]
    data['logs']=sorted(entries,key=lambda x:x['age_seconds'])[:2]
    return data


def render(data):
    total=data['total_samples'] if data['total_samples'] is not None else '?'
    lines=[f"[进度] 已完成样本 {data['completed_samples']}/{total} | 当前：{data['stage']} | PID={data['active_pid']}"]
    if data['instance'] or data['method']: lines.append(f"       样本={data['instance']} | 方法/对照={data['method']}")
    for item in data['logs']:
        lines.append(f"       日志 {item['path']}（{item['age_seconds']}秒前更新）\n       {item['tail']}")
    if not data['logs']: lines.append('       暂无可显示的步骤日志；进程存活不代表有进展。')
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-id',required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*',args.run_id):parser.error('Invalid run-id')
    directory=ROOT/'runs/pipeline'/args.run_id
    status=json.loads((directory/'status.json').read_text())
    print('流水线状态：',status['state'],' 最后更新：',status['updated_at'])
    if status['state'] in ('running','stopping'):
        print(render(snapshot(ROOT,directory,status['child_pid'])),end='')
    else: print('流水线已退出；查看 pipeline.log 和对应批次 analysis.md。')


if __name__=='__main__':main()
