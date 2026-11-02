# 首次使用：官网入口与本机工作台

日期：2026-10-30。沿用“本地运行、网页操作”，不另建云端下载服务或桌面安装器。

## 用户看到的流程

```text
公开首页
├─ 首次使用：安装 Python → 安装 AEF-GRiTS → 启动本机网页
└─ 已启动：打开 http://127.0.0.1:5555/portal

本机网页：P0 数据源与授权 → P1 位置 → P2 年份 → P3 保存与下载
```

- 官网是静态说明页，代码在 `website/index.html`。不接收账号、样本、任务或数据流量；不扫描访问者本机端口。
- `website/index.html#install` 可直接预览首次使用页。三步为准备环境、复制安装/启动命令、打开本机工作台；浅米色配色与本机 GUI 保持一致。
- 复制按钮只操作浏览器剪贴板，不执行命令。权限不足时选中代码供手动复制；禁用 JavaScript 后仍可阅读命令和使用工作台链接。
- “打开本机工作台”只打开已经运行的服务，不能自动安装或启动 Python。
- 用户只需首次安装。之后启动同一环境中的工具，浏览器便会自动打开；无需每次重新下载仓库，也无需编写脚本。
- GEE 的认证在本机主动发起，跳转 Google 官方授权。Project 是用户有权限使用的项目，不是密码；官网不应收集密码或凭据。
- 公开 COG 暂不能创建下载任务；本轮不扩大范围修复其读取器或接入任务队列。

## 当前可用的安装说明

尚未发布包含这轮界面修改的新发行包，因此首页保留开发版本提示，不提供假 Release 链接或未经验证的 PyPI 安装命令。

当前先交付匹配版本的源码 ZIP。用户准备 Python 3.11/3.12，在解压目录（含 `pyproject.toml`）执行：

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[download,web]"
.\.venv\Scripts\python.exe -m webapp.launcher
```

macOS/Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ".[download,web]"
.venv/bin/python -m webapp.launcher
```

不要求激活脚本，不需要放宽 PowerShell 执行策略。之后运行最后一行即可。已有 conda 环境的用户可直接安装到所选环境，不必叠加 venv。

正式发布时，将首页主入口关联到真实 GitHub Release，提供经验证的 wheel 和源码 ZIP。对于 wheel，从存放它的目录运行 `python -m pip install "./aef_grits-<实际版本>-py3-none-any.whl[download,web]"`；再执行 `python -m webapp.launcher` 或已安装的 `aef-grits-web`。占位符须替换为实际发布文件，不应原样展示为可点击下载。

## 存储位置

默认是运行 Python 的用户的 `Downloads/AEF-GRiTS`，不是官网服务器，也不是 Python 包的安装目录。每个任务在所选根目录内建立独立子目录，预检按实际选中的磁盘检查剩余空间。

不在官网硬编码某个用户的 `C:\Users\用户名\…` 路径。需要其他数据盘时，可使用本机系统文件夹窗口；或者启动时设置：

```text
python -m webapp.launcher --output-root "目标数据目录"
```

系统选择器属于运行 Python 的电脑。无桌面或缺少 Tk 的环境使用“网页内选择子目录”；远程网页无法用此接口选择访问者电脑的磁盘。

## 本轮修复与重启约束

- P2 的“全部年份”移到标题中，与“可多选”同一行，不再占用年份列表下方的一行。保持现有选年逻辑、键盘交互和配色。
- 目录选择 404 已定位到旧服务：后端进程启动后才加入该接口，静态 HTML 却已变更。最新源码含接口，不应再加一个重复接口或删除安全检查。
- 前端遇到该 404 时，提示重启本机服务并保留网页内目录浏览。
- 升级必须先完成/取消下载，再停止旧 Python 服务，更新后启动。仅刷新浏览器不会加载新的 Python 路由。

本轮已验证打包后在仓库外启动网页；依赖复用现有环境，不代表全新 Windows/macOS/Linux 的完整安装验收。正式发布前仍应完成干净环境依赖安装、首次 GEE 授权与小数据下载。
