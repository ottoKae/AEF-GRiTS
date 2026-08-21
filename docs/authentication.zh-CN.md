# 认证与身份边界

[English](authentication.md) | [简体中文](authentication.zh-CN.md)

AEF-GRiTS采用一条固定原则：**下载任务只复用已经存在的凭据；下载、重试、
断点续传和Web worker绝不自动发起登录**。只有用户主动执行
`aef-grits-auth login`或点击Web页面的登录按钮时，系统才进入交互认证。

## 三种身份必须分开

1. 应用用户：决定谁能查看和管理Web任务。
2. Google/Earth Engine凭据：决定以哪个用户访问数据。
3. Cloud Project：决定API启用、配额和请求归属。

登录Google并不会取消Project要求。每位用户仍需选择自己有权使用、且已启用
Earth Engine API的Project。Project ID不是OAuth密钥，但属于部署信息，仓库、
公共任务接口和日志均不写入具体默认值。

## CLI凭据自动发现

下载命令支持`--auth-source auto|earthengine|adc`和可选的`--project`。
`auto`严格按以下顺序选择：

1. 已显式设置的`GOOGLE_APPLICATION_CREDENTIALS`；
2. 当前用户通过`earthengine authenticate`保存的持久凭据；
3. 本地ADC或云平台附加服务账号。

显式来源无效时直接失败，不会静默切换到其他账号。Project按
`--project`、私有运行环境变量`AEF_GRITS_PROJECT`、凭据中已有Project或quota
Project的顺序解析。代码和README没有真实Project默认值。

只检查、不登录：

```bash
aef-grits-auth status
```

用户主动登录并验证：

```bash
aef-grits-auth login \
  --source earthengine \
  --auth-mode localhost \
  --project YOUR_GEE_PROJECT

aef-grits-auth verify --source auto --project YOUR_GEE_PROJECT
```

远程SSH环境应由用户明确选择`gcloud`或`notebook`模式。ADC同样需要主动建立：

```bash
aef-grits-auth login --source adc --project YOUR_GEE_PROJECT
aef-grits-auth verify --source adc --project YOUR_GEE_PROJECT
```

## 不同场景的推荐策略

| 场景 | 推荐身份策略 |
|---|---|
| 个人Windows/macOS/Linux | 当前用户Earth Engine持久凭据；下载使用`auto` |
| 单用户SSH服务器 | 任务开始前主动完成`gcloud`或`notebook`登录 |
| 校园共享Web服务器 | 每用户Google OAuth；禁止服务器全局Project回退 |
| 单位托管无人值守服务器 | 管理员可选服务账号ADC |
| Google Cloud运行环境 | 附加最小权限服务账号ADC |
| CI | Workload Identity Federation，不提交长期JSON密钥 |

服务器统一ADC只是可选的单位托管模式，不能用于冒充共享Web用户。

## 校园共享Web部署

本地默认仍为`AEF_GRITS_WEB_AUTH_MODE=local`。共享部署应通过可信反向代理提供
HTTPS，并把状态目录和加密凭据库放在ext4/XFS等原生文件系统。所有部署密钥只
在服务器运行环境中配置：

```bash
aef-grits-auth vault-init \
  --database /var/lib/aef-grits/auth/vault.sqlite \
  --key-file /etc/aef-grits/vault.key

export AEF_GRITS_WEB_AUTH_MODE=oauth
export AEF_GRITS_WEB_OAUTH_CLIENT=/etc/aef-grits/oauth-client.json
export AEF_GRITS_WEB_OAUTH_REDIRECT_URI=https://aef.example.edu/api/auth/callback
export AEF_GRITS_AUTH_VAULT=/var/lib/aef-grits/auth/vault.sqlite
export AEF_GRITS_AUTH_VAULT_KEY=/etc/aef-grits/vault.key
export AEF_GRITS_WEB_ALLOWED_DOMAINS=example.edu
export AEF_GRITS_WEB_TRUSTED_HOSTS=aef.example.edu
export AEF_GRITS_WEB_PROXY_COUNT=1
```

该模式不要设置服务器全局`AEF_GRITS_PROJECT`。用户登录后输入自己有权使用的
Project，服务器验证成功后才签署下载计划。计划和任务绑定匿名用户哈希、凭据
版本和Project指纹；其他用户不能查看、取消或续传。

refresh token加密保存，不进入命令参数、日志、Zarr、Parquet或NTFS。下载子进程
只通过私有进程环境获得不透明handle。凭据失效时任务保存checkpoint并进入
`auth_required`；用户重新登录后主动续传，不会自动切换账号。

直接`computeFeatures`和`computePixels`不申请Google Drive权限。旧Drive导出和
下载工具保留为单独的可选认证路径。

## 运行状态和安全边界

前端区分“未连接”“已连接但需要Project”“可执行”和“需要重新认证”。认证错误
与网络错误分开：401/403会暂停任务；TLS截断、超时、429和5xx才进入有界重试。

不要通过网页上传凭据JSON。OAuth client secret、vault key、session secret和服务
账号材料必须存放在操作系统密钥目录、systemd credentials或外部secret manager，
不能提交到Git，也不能放进下载输出目录。
