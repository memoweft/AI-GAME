# WeftMod 手机组件

本仓库负责手机任务、控制台和设备执行接入，是个人助手的一项可选能力，不是第二个全能助手或共享记忆库。

2026-09-09起，WeftMod整体方向为跨桌面和手机的可编程执行能力；本仓库承担Android设备适配，WeftMate内的runtime/weftmod承接对话工具、Windows适配及DSH代码运行。对话Agent（智能体）直接观察与编写脚本，不必把新目标交给旧内置planner（规划器）。

新增已鉴权的`/api/execution/v2/device-runs`：创建执行实例、读取状态、`observe`读取UI XML（界面结构）与可选截图、`actions`顺序执行动作数组、`controls`暂停/恢复/取消/结束。创建时复用选定device_profile_id和已有owner（归属身份）鉴权，设备实例持有共享serial（序列号）租约；旧正式运行器也使用该锁。command_id（命令标识）防止重复派发，未知结果需重新观察；进程重启暂停原执行，不自动重放。`health.capabilities.direct_device`单独说明直接设备能力，`requires_model:false`。组件测试使用隔离SQLite（轻量数据库），实际用户体验仍由主项目S05统一记录。

| 路径 | 责任 |
| --- | --- |
| apps/console/backend/ai_game_console/ | 现有任务、状态、观察、执行和验证代码 |
| apps/console/frontend/ | 当前控制台界面 |
| apps/console/tests/ | 组件测试 |
| scripts/ | 控制台、模型和维护脚本 |
| config/ | 脱敏模板；真实 .env 只留本机 |
| contracts/、services/、workflows/ | 接口与设备接入工程材料 |

主程序提供用户交互，组件在获授权任务范围内执行。手机状态和截图不直接等于长期偏好；不复制独立用户画像。现有授权、停止和结果验证边界保持，加载或健康状态不能充当真实设备成功。

全局方向、当前任务及用户验收来自 WeftMate 主仓库 docs，由任务提供所需明确版本；独立克隆不依赖固定机器父目录。功能状态与真实设备验证统一记在主项目当前任务中。

托管入口 `managed_runtime.py` 通过stdin（标准输入）接收启动帧，除既有字段外可带可选 `execution` 配置。缺省、null或enabled:false时保留needs_setup（需要配置）；启用时接收绝对ADB路径、可选serial和本机回环模型的endpoint/name/api_key，模型密钥仅保存在进程内。宿主可以复用自己的凭据服务，不必另启动固定名称的GUI模型。

正式设备选择与任务控制沿用 `/api/execution/v2`；当前通路为Android模拟器，任务使用device_profile_id（设备配置标识）。服务ready（就绪）只表示运行组件和角色接线具备条件，不代表已有设备或任务已完成。手机结果接回MemoWeft时应使用真实task_id（任务标识）、版本、结果和事件来源；本组件不自行生成第二份记忆。

本机目录 AIGame/Repository 保留，因为当前 PowerShell（命令行与脚本环境）和 WSL（适用于 Linux 的 Windows 子系统）脚本仍引用它。原接口、Python 导入、环境变量和数据库兼容名不全局替换。远端未改名或推送。

docs/product/VISION.md 与 PROJECT_MAP.md 是现有发布打包器读取的兼容资料，保留最小职责说明；无旧路线或第二份当前状态。运行数据、模型、日志和设备截图由现有受忽略 runtime 路径承载，不能作为公开源码上传。

`python scripts/check-project-layout.py` 仅验证文档与忽略规则；它不启动设备、不验证模型、权限执行或用户体验。
