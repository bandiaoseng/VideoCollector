# 收藏助手 VideoCollector

把抖音或视频号分享链接与点评交给程序，下载视频、校验、按点评命名，并上传至使用者自己配置的夸克网盘目录。

## 安装和首次使用

打开 [Windows 安装包下载页](https://github.com/bandiaoseng/VideoCollector/releases/tag/v0.1.0-windows.1)，下载 VideoCollector-Setup-0.1.0-x64.exe，双击安装，从桌面启动。支持 Windows 10/11 64 位，自带 Python、桌面依赖、FFmpeg 和 ffprobe。

1. 在自己的夸克网盘创建目标文件夹。
2. 启动后的“首次设置”中填写类目和路径，例如：二创=视频素材/二创。
3. 按需配置自己的解析服务：视频号使用智凌 API Key；抖音使用自己的 DTK 服务地址和 API Key。安装包不附带服务账号、服务器或解析额度，也不包含 F2 的独立下载环境。
4. 点击“登录夸克”，自行扫码；填写视频链接和点评（例如：应用案例 #二创），再点击开始。

保存设置不会自动登录、下载或上传。实际解析可能产生服务费用。任务处理期间需保持电脑和软件开启；结果不明时先核对网盘，避免重复处理。

数据、配置、密钥和网盘会话默认在 %LOCALAPPDATA%/VideoCollector，可用 VIDEOCOLLECTOR_DATA_DIR 指定其他绝对路径。重新安装不覆盖用户数据；卸载也保留。每位同事自行配置，不能分享数据目录或密钥文件。

本次是桌面试用安装版；不改变现有手机网页、云服务器和备案配置。已验证空白配置启动及媒体工具，未使用真实账号进行付费解析或网盘上传，也未完成另一台干净电脑的验收。

## 开发

原网页、worker、离线测试和部署模板仍保留。开发依赖见 requirements.lock.txt，通用部署说明见 deploy/README.md，Windows 打包说明见 packaging/README.md。源码来源和隐私规则见 SOURCE_SNAPSHOT.json、UPLOAD_POLICY.md。
