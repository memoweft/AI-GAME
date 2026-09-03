# GUI model service

本目录定义当前 GUI-Owl 模型服务的固定 manifest（清单）和本机运行方式。服务是 loopback-only inference endpoint（仅回环地址推理端点）：它接收文本与当前画面并返回模型结果，不拥有用户对话、Android Task、设备控制、动作派发、证据或长期记忆。

AI-GAME 的 `android_ui_agent/1`、RuntimeKernel 和设备适配层负责把模型结果约束为当前 Task 中的一次候选动作，并完成身份检查、派发、后置观察、验证和恢复。WeftMate/DSH 继续拥有面向用户的对话、权限与结果呈现。

## 当前文件

- `model-manifest.json`：模型身份和固定来源；
- `scripts/wsl/bootstrap-gui-model.sh`：安装隔离运行环境；
- `scripts/wsl/start-gui-model.sh`、`status-gui-model.sh`、`stop-gui-model.sh`：本机服务生命周期；
- `scripts/model-runtime.ps1`：Windows 侧入口；
- `config/model-runtime.env.example`：不含秘密的配置示例。

运行时版本写入本机 runtime（运行目录），不复制到长期文档。模型端点可达或生成了动作文本都不证明设备动作发生，更不证明用户目标完成。
