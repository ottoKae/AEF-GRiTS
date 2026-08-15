# Linux NTFS 安全下载与恢复

AEF-GRiTS 现在把 Linux `ntfs3` 仅作为“已完成数据的最终交付盘”，不再把它作为
Zarr 的实时工作盘。代码可以降低再次触发内核阻塞的概率，但不能修复已经发生的
NTFS3 内核死锁。

## 三类存储目录

| 目录角色 | 推荐文件系统 | 保存内容 |
|---|---|---|
| 状态目录 | ext4/XFS | checkpoint、catalog、report、PID、日志、队列和故障记录 |
| 暂存目录 | ext4/XFS | 当前正在写入的一个格网 Zarr 或点位任务 |
| 最终目录 | 可以是 NTFS3 | 只接收已经完整并校验的 Parquet/Zarr 产物 |

下载 worker 可以并发请求 Earth Engine，但只有协调进程写入 Zarr。完整格网首先在
ext4/XFS 上生成；随后由单独的单写入进程顺序复制到 NTFS 的签名化 incoming 目录；
复制成功后才重命名为最终目录，并更新 ext4 上的提交状态和 catalog。

如果复制超时，程序只在状态目录写入 `commit_incident.json`，只发出一次终止请求，
不会自动删除 NTFS 临时目录，也不会反复执行 `kill -9`、`rm` 或递归扫描。发现与
NTFS 有关的 D 状态进程后，程序拒绝启动新任务。

## 安全启动示例

```bash
STATE=/home/duziwei/aef_state/anhui_mgrs_2019
STAGE=/home/duziwei/aef_staging/anhui_mgrs_2019
FINAL=/mnt/hdda/duziwei/anhui_mgrs_2019

mkdir -p "$STATE" "$STAGE"

aef-grits-storage-check --final "$FINAL" --state "$STATE" --staging "$STAGE"

nohup aef-grits-grid \
  --grid-scheme mgrs \
  --tiles 50RMU \
  --project YOUR_GEE_PROJECT \
  --years 2019 \
  --out-dir "$FINAL" \
  --state-dir "$STATE" \
  --staging-dir "$STAGE" \
  >"$STATE/download.log" 2>&1 &
printf '%s\n' "$!" >"$STATE/download.pid"
```

日志和 PID 不能再写入 `/mnt/hdda` 或 `/mnt/hddb`。

Web 服务使用：

```bash
export AEF_GRITS_WEB_OUTPUT=/mnt/hdda/duziwei/aef_web_output
export AEF_GRITS_WEB_STATE=/home/duziwei/aef_state/web
export AEF_GRITS_WEB_STAGING=/home/duziwei/aef_staging/web
python webapp/app.py
```

## 服务器恢复顺序

1. SSH 在认证前就被关闭时，必须使用管理员控制台重启，不能继续制造 SSH 或文件查询进程。
2. 重启后确认启动时间已经变化，并确认没有 D 状态进程。
3. 检查 `findmnt` 和内核日志；NTFS 存在脏卷或错误时，应卸载并执行管理员认可的离线检查。
4. 不要使用 NTFS3 的 `force` 选项强行挂载。
5. 重新挂载后只做一次有界健康检查和磁盘空间检查；检查不及时返回就停止。
6. 把旧 checkpoint 复制到 ext4 状态目录，保留原文件，不执行删除。
7. 只通过包含 `--state-dir` 和 `--staging-dir` 的新命令恢复任务。

对于“全部块已经保存、只缺最终报告”的旧格网，可增加：

```bash
--import-legacy-progress /mnt/hdda/.../LEGACY.progress.json \
--adopt-existing-complete
```

程序只有在签名一致且 ledger 的完成块数量严格等于请求块总数时才允许接管。
