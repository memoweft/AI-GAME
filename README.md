# WeftMod 手机组件

WeftMod 是 WeftMate 的可选手机能力，原名 AI-GAME；本机保留 AIGame/Repository 兼容路径。代码中的 AI_GAME、ai_game_console 与既有接口名保持不变。

先读 [组件说明](docs/COMPONENT.md) 和 [AGENTS.md](AGENTS.md)。全局方向与任务由主程序仓库的 docs 提供；本组件不另建全局进度或用户画像。独立克隆无需读取固定盘符或父目录。

本仓库保留 apps、scripts、config、contracts、services、workflows 和测试。源码未套用累计包补丁；包内“已加载插件”等描述不代表本机已验证。真实手机和用户体验本轮未测。

运行 `python scripts/check-project-layout.py` 检查文档入口。其他工程入口见 [控制台说明](apps/console/README.md)，只有具体运行任务才恢复服务与设备。

LICENSE 和所有第三方许可保持不变。未改远端、发布或重写 Git 历史。
