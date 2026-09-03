# AI-GAME

AI-GAME 是 WeftMate / DeepSeek Harness（DSH，执行智能体运行时）的本地 Android 模拟器任务后端。用户只在 WeftMate 中与 DSH 对话；当目标需要操作 Android 时，DSH 通过 `phone_execution` 调用 AI-GAME，结果回到原对话。

- [产品愿景](docs/product/VISION.md)
- [当前代码地图](docs/product/PROJECT_MAP.md)
- [V2 执行契约](docs/product/EXECUTION_CONTRACT.md)
- [通用 Android 长任务验收标准](docs/product/ACCEPTANCE.md)

## 当前状态

- 正式方向只使用 Android 模拟器和通用 `android_ui_agent/1`。
- 稳定 owner pair（所有者二元组）、canonical Task（权威任务）、模拟器 Profile、常驻调度、观察/动作/验证、恢复和 scoped experience（有范围经验）已形成工程基础。
- 旧真机、Android Companion、无线 ADB、ADB reverse、固定 Settings-only runner（仅设置页执行器）和旧阶段治理已退出活动路线。
- 项目修复基线已经建立，但当前仍不属于产品所有者 dogfood（亲自试用）候选。首个代表性验收场景已经冻结；WeftMate 还没有管理 AI-GAME 生命周期，因此用户界面不发布依赖本服务的全局入口。

## 开发诊断

PowerShell：

```powershell
.\scripts\console.ps1 setup
.\scripts\console.ps1 build
.\scripts\console.ps1 start
.\scripts\console.ps1 status
.\scripts\console.ps1 stop
```

默认开发地址是 `http://127.0.0.1:4310`。这是开发者诊断地址，不是最终用户应配置、理解或手工开关的产品前置条件。

运行测试：

```powershell
.\scripts\console.ps1 test -NoBrowser
```

## 目录

```text
apps/console/backend/     本地 API、V2 Task、调度、模拟器与持久化
apps/console/frontend/    开发者诊断控制台
config/                   非秘密配置示例
docs/product/             当前愿景、代码地图和执行契约
scripts/                  开发启动、模型与构建工具
runtime/                  本机数据库、日志、证据和进程状态（不入 Git）
```

通道接受、模型输出、ADB 命令成功和 UI 可见都不是完成证明。涉及真实动作时，必须由当前设备的新鲜观察、动作回执和后置验证共同支持结果。
