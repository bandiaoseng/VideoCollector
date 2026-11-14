# 独立后台部署准备

这是供后续 Linux 测试环境使用的模板，尚未在 Linux 或云服务器运行验收。本机没有可用的 WSL 发行版、Docker 或 Podman；本轮没有安装系统功能、购买服务器或部署外部服务。

Windows 桌面已由用户实际验证抖音和视频号短视频及长视频自动存入夸克，画面、声音、内容正常。这证明当前本机链路可用，不代表手机入口或服务器网络已完成验收。

## 后台结构

手机网页保存分享链接和点评 → SQLite 任务 → 独立 worker → 对应平台下载与视频校验 → 独立夸克会话上传。worker 与网页请求分离，网页关闭不会停止后台处理。web 与 worker 共用同一台机器的持久数据目录；SQLite 不放网络盘，不多主机并发运行。

默认服务配置采用独立夸克上传，不依赖 Agent 环境。只有旧配置完全没有 `quark_web` 字段时，才保留原 CLI 分支兼容；独立配置存在但有误时会停止，不能降级调用另一上传方式。

web、worker 和本地检查使用同一个配置加载器。当前机器可沿用配置旁 `.runtime/desktop-tools.json` 的工具位置提示及 `.runtime/quark-web-settings.json`；配置内已经有 `quark_web` 时优先使用明确配置，不会被旁文件覆盖。加载配置不会开启智凌、修改密钥或写回文件。

同机的桌面、worker 与命令行执行入口共用 `coordination_dir`，任一入口执行期间会拒绝另一个入口执行。切换入口前只读核对已登记的其他队列；有上传结果不明或记录缺失时停止，不会执行其他队列、更换配置绕开旧任务或自动重传。网页的只读核对不占 worker 的长期执行锁。进程退出后操作系统释放锁，不删除锁文件来抢占运行中的进程。

模板把协调目录固定为 `/var/lib/video-collector/coordination`；省略时优先放在独立夸克会话旁，否则放在配置旁的 `.runtime/execution-coordination`。同一主机上的各配置必须共用此位置，运行中不能更改。该保护不是跨服务器账号锁；迁移时须停止原执行入口并处理未确认任务，不能用两份不同协调目录同时运行同一账号。

同一份夸克会话在上传、网页核对、扫码登录之间另有短期互斥。占用时保留任务并提示等待，不会重试上传。桌面与无桌面扫码共用临时会话流程，只有成功确认才替换旧登录记录；失败、取消或超时不覆盖旧记录。私有 JSON 使用独立临时文件及原子替换，Windows 短暂文件冲突只重试本地保存。

## 模板配置

- 代码放在 `/opt/video-collector`；服务账号为 `video-collector`，HOME 与状态目录为 `/var/lib/video-collector`。systemd 模板使用 `StateDirectory` 建立权限为 0700 的状态目录；仍需提前创建专用服务账号并安装依赖。
- 服务 Python 环境为 `/opt/video-collector/.venv`，安装根目录 `requirements.lock.txt`，其中已包含独立上传所需的 `httpx`。下载环境 `.venv-download` 安装 `requirements-downloader.txt`，避免 F2 与网页依赖相互影响。系统需有 Git、FFmpeg/ffprobe 和可选的 aria2。独立上传运行不需要 Node 或 Agent 技能目录。
- F2 固定源码提交 `f6be8c0ffba9a127075bbeafe4838716650b6325`。`constraints-downloader.txt` 来自当前 Windows 依赖快照；Linux 的安装、`jeepney` 和相关原生库仍须在实际 Linux 环境检查，不能把锁定版本视为已通过跨平台验证。
- 将示例配置作为 `/etc/video-collector/config.json` 的基础。不要直接使用本机 Windows 的绝对路径、真实配置或登录文件。
- `categories` 保存稳定的任务分类标识，例如 `{"二创":"category-erchuang"}`；独立上传的实际目录由 `quark_web.category_paths` 映射，例如 `{"二创":["搜集视频","二创"]}`。这只是配置示例，目录必须已经存在，上传前会核对唯一目标。不要在已有任务仍待处理时随意更换分类标识或目录。
- 独立登录记录配置到 `/var/lib/video-collector/auth/quark-web-session.json`，所在目录权限 0700、文件权限 0600，由服务账号读取与更新。`/opt/video-collector` 在服务内只读，不能把登录记录放到代码目录。
- 无桌面环境使用 `quark_login_cli.py`，相关二维码依赖已加入锁定依赖。它必须收到明确的 `--start-login` 才会取二维码；本轮仅用假客户端验证，真实云端扫码仍需用户授权。它不搬用桌面会话、不启动任务；失败或取消保留原登录记录。
- 智凌在示例中保持 `enabled: false`。以后明确启用时，使用服务环境变量 `ZHILING_API_KEY`，并配置可用的 FFmpeg/ffprobe；解析可能消耗服务商额度，不能用反复提交来检查网络。
- `collector.env.example` 对应 `/etc/video-collector/collector.env`，应由管理员保存为 0600。应用访问口令与夸克登录不同；真实口令、智凌密钥、会话和环境文件都不能打包或放入前端。
- Caddyfile 只保留 HTTPS 反向代理模板。域名、DNS、证书、端口及手机浏览器都未实测；当前不要启动公网服务。`COLLECTOR_PUBLIC_ORIGIN` 必须与真实来源完全一致，公网入口必须 HTTPS。

## 现在可以做的隔离检查

下列测试只使用临时任务、合成视频与假客户端，不接触真实解析、下载或网盘：

```sh
python -m unittest test_runtime_integration test_web_runtime test_service_check test_quark_login_cli test_quark_recovery test_upload_evidence test_web test_execution_coordination test_session_coordination -q
```

默认 worker 测试必须经过默认 `process_one` 选择上传器，仅替换网络客户端，防止注入假上传器掩盖接线问题。检查项包括：正确选择独立上传、正常关闭客户端、异常保留原任务、关闭网页后后台可继续、明确独立配置不会退回旧 CLI，以及状态检查不触发网络请求。

在未来已具备的 Linux 隔离环境，还应运行 `test_process_runner`、`test_cancellation`，检查 POSIX 进程组退出与文件锁；检查依赖能导入，再用 `systemd-analyze verify` 和 `caddy validate` 校验模板。仅在目标环境已有相应程序时执行，不为此自动安装系统功能。

## 本地就绪检查

在未来已批准的服务器上，以服务账号明确启动一次扫码登录：

```sh
/opt/video-collector/.venv/bin/python /opt/video-collector/quark_login_cli.py --config /etc/video-collector/config.json --start-login --timeout 300
```

命令只输出本次私有二维码图片的位置和状态，不输出二维码网址、令牌或 Cookie。通过受信任的连接打开该图片，用手机夸克扫码确认；二维码每 120 秒刷新，总等待默认 300 秒、最多 900 秒。正式会话在成功核验后原子替换，临时会话和二维码结束后清理。它不开放额外网络端口，不下载或上传视频。手机同屏扫码方式、Linux 文件权限和真实云端登录有效性仍须实测。

本轮没有执行以上真实扫码命令，也没有复制或上传本机密钥及登录记录。

在服务账号和实际环境变量下执行：

```sh
/opt/video-collector/.venv/bin/python /opt/video-collector/service_check.py --config /etc/video-collector/config.json --deep
```

它检查下载命令、脚本、工具路径、下载 Python 依赖、分类映射、独立登录记录的本地格式及 worker 心跳。状态查询不会构造网络客户端，不会请求智凌、查询夸克或上传；也不会输出路径、目录标识或密钥。登录文件格式正常不代表登录仍有效。

默认检查双平台。退出码 0 只表示本地配置和心跳通过，1 表示未就绪，2 表示配置无法读取。需要单平台诊断时显式加 `--platform douyin` 或 `--platform wechat`。示例未填写真实分类、未配置登录、智凌未启用且 worker 未启动时，检查不通过是预期结果。

## 进入真实服务器测试前

手机网页的明确解析重试、上传只读核对和无桌面扫码续登入口已通过本机隔离验证，仍需在选定 Linux 环境和安卓手机验收。服务器所在地的抖音访问信息、智凌连接、视频 CDN 和夸克会话都需要实际验证；不得用迁移服务器绕过工具拒绝。没有常开电脑时，长期运行最终仍需要独立服务环境，本轮没有代购或启用任何付费服务。

worker 停止时会等待当前任务；强行停止后的不明上传必须先核对，不能重传。当前保留已下载文件以支持恢复，尚无自动清理策略；长期运行前需要磁盘空间监测和仅清理已确认成功任务的保留规则。云资源与带宽预算应在依赖和样本体积明确后再核实，不沿用旧报价作购买建议。
