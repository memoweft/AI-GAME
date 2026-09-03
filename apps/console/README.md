# 本地执行诊断控制台

该 Console（控制台）是 AI-GAME 的开发者/运行诊断面，不是最终用户聊天产品。最终用户在 WeftMate 中与 DSH 对话。

## 用途

- 查看 V2 Task、事件、等待和恢复状态；
- 查看已运行 Android 模拟器与执行器事实；
- 配置或测试开发模型；
- 在隔离环境运行诊断请求。

它只展示当前模拟器与 V2 运行事实，不应扩展为第二套会话、规划、审批或长期记忆产品。

## 命令

从仓库根目录运行：

```powershell
.\scripts\console.ps1 setup
.\scripts\console.ps1 build
.\scripts\console.ps1 start
.\scripts\console.ps1 status
.\scripts\console.ps1 stop
.\scripts\console.ps1 test
```

默认地址 `http://127.0.0.1:4310` 只监听本机。当前它由开发者显式启动；最终产品必须由 WeftMate 管理 AI-GAME 生命周期，用户不负责端口开关。

云端模型密钥以当前 Windows 用户的 DPAPI（数据保护 API）保护，不返回浏览器。连接测试会发送真实请求，可能产生费用。

控制台中出现的 Task 接受、模型回复或 ADB 命令结果都不是用户目标完成证明；完成仍需要新鲜设备观察、动作回执和完成判据验证。
