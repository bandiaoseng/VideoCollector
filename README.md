# 收藏助手 VideoCollector

把抖音或视频号的分享链接与点评交给程序，由程序下载视频、核验文件、按点评命名，并上传至用户自己配置的夸克网盘目录。

## 本仓库的范围

这是 2026-11-14 整理的私有源码快照，包含桌面入口、手机网页、独立后台 worker、下载上传适配、异常恢复与离线测试。没有导入个人运行配置、网盘会话、API 密钥、浏览器资料、下载视频、任务数据库或历史现场记录。

源码上传不会更改现有服务器或备案配置，也不会启动下载、上传或付费接口。仓库中的部署文件是通用模板，不包含当前服务器地址和凭据。本次不发布安装包，不把本地测试结果等同于手机或云端验收。

## 开发依赖

建议 Python 3.12。根目录 requirements.lock.txt 固定网页、桌面和上传层的依赖；下载器使用独立环境并安装 requirements-downloader.txt。系统需有 FFmpeg、ffprobe；F2 下载路径可能还需 aria2。前端逻辑测试使用 Node.js。

~~~powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.lock.txt
~~~

安装缓存、临时目录和测试输出请放在空间充足的磁盘。依赖列表保留原项目版本，本次测试复用已有本地环境，不代表已重新验证所有依赖在全新系统上的安装。

## 首次配置

1. 从 config.example.json 创建本机 config.json，填写自己的类目与目录映射。
2. 使用独立夸克登录流程，让使用者自行扫码；登录记录保存在 .runtime，不提交 Git。
3. 需要视频号解析时，阅读 ZHILING_SETUP.md，配置使用者自己的服务密钥并确认费用。
4. Windows 桌面入口为 start-collector.cmd；手机网页与 worker 的独立部署说明见 deploy/README.md。

示例配置中的密钥和目标目录均为空。未经配置不能当作开箱即用的成品。

## 离线测试

~~~powershell
.venv/Scripts/python -m unittest discover -p "test_*.py" -q
node --test test_preview.mjs test_submission.mjs test_draft_editor.mjs
~~~

Windows 测试请先把 TEMP 和 TMP 设置到较短的本机目录（本次使用 F:/Codex/tmp/vcg-1114）。较深目录会触发当前程序的长路径兼容问题；本次源码整理未修改这一业务实现。

测试使用临时目录、合成媒体和服务替身。validation/wechat_worker_offline.mjs 是可选的上游契约研究脚本，依赖本地另备的固定上游源码；上游仓库未随此项目上传，不属于上述常规测试。

## 防重复与恢复

普通继续操作优先复用已下载结果；解析结果不明时需要明确确认后重新解析，可能消耗额度。上传结果不明时先核对网盘结果，不能盲目重传。执行互斥覆盖同机共用协调目录的入口，不能视为跨服务器分布式锁。

## 来源

独立夸克网页协议参考 OpenListTeam/OpenList 的 quark_uc 驱动与 imeiming/quark-drive 的扫码流程，源码中的出处说明已保留。F2 与 yt-dlp 通过依赖清单安装，其上游代码与许可不在本仓库重新打包。

后续上传规则见 UPLOAD_POLICY.md；本次源码校验清单见 SOURCE_SNAPSHOT.json。

