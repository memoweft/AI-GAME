# 06 Android / ADB 设备层

## 1. 目标

设备层把 Android 和 ADB 的不稳定细节封装成 Task Runtime 可依赖的观察与动作能力。它提供事实和执行结果，不负责规划任务、理解用户意图或声明 Stage 完成。

## 2. 逻辑组件

```text
Task Runtime
   │
   ├─ Observation Provider
   │    ├─ Screenshot Provider
   │    ├─ UI Tree Provider
   │    └─ Device State Provider
   │
   └─ Action Executor
        └─ ADB Adapter
             └─ Android Device
```

v0.1 可以共进程实现这些组件，但接口责任应清晰分离。

## 3. Device Session

一个设备会话至少记录：

- `device_id`：ADB 可识别的稳定标识；
- `connection_state`：连接、断开、未授权等状态；
- `screen_size` 与 `orientation`；
- `foreground_app`；
- `keyboard_state`（可获取时）；
- 最近成功观察时间；
- 当前是否被 Task 占用。

MVP 只要求单设备。设备断开后，不允许把最后一张旧截图当作当前状态继续动作。

## 4. Observation 能力

### 4.1 Screenshot

截图结果至少包含：

- 图片引用或字节流；
- width、height；
- device_id；
- captured_at；
- orientation；
- capture 成功或错误信息。

### 4.2 UI Tree

对普通 Android App，在设备和页面允许时采集 UI hierarchy。UI Tree 是截图的补充，不是唯一真相；页面可能缺少可访问性节点、节点文本可能过时，游戏场景也可能基本不可用。

### 4.3 Device State

最小状态包括：

```text
foreground_app
screen_size
orientation
keyboard_state
connection_state
timestamp
```

## 5. Action 集合

MVP 保持动作 API 极小：

```text
tap
long_press
swipe
input_text
back
home
open_app
wait
screenshot
```

其中 `screenshot` 也可以作为 Observation 请求暴露；在动作集合中保留它，便于 Operator 明确请求重新观察。不得因为某个 App 的首个用例增加 `open_battery_page` 之类的业务专用 Action。

## 6. 动作校验

Action Executor 在调用 ADB 前至少检查：

- 当前设备已连接并授权；
- Task 持有该设备的执行权；
- Action 类型在允许集合中；
- 坐标处于当前屏幕范围；
- 滑动起终点和持续时间合法；
- 输入文本符合当前 Adapter 支持的编码能力；
- Action 未在暂停、取消或终态之后发出。

## 7. 执行结果

设备层返回 Transport 层结果，例如：

```json
{
  "action_id": "act_123",
  "accepted": true,
  "started_at": "...",
  "finished_at": "...",
  "adapter_code": 0,
  "error": null
}
```

`accepted: true` 只表示命令被执行层接受并完成传输，不表示 Expected Outcome 已达成。后者必须由 Verify 基于新 Observation 判断。

## 8. 超时与断连

- ADB 调用必须有明确超时，不能无限阻塞 Runtime 循环。
- 断连、未授权、设备离线和命令超时应返回结构化错误。
- 设备状态不明确时，Runtime 停止发出动作并进入 `WAITING` 或恢复流程。
- 恢复连接后必须先重新获取 Observation，不能直接重放不确定是否执行过的动作。

## 9. MVP 对“无固定脚本”的解释

允许使用通用 ADB 原语和通用模型 Adapter；不允许为电池任务写入：

- 固定坐标序列；
- 固定页面点击列表；
- 按设备型号硬编码的“设置 → 电池”路径；
- 看到任务文字后绕过 Operator 直接运行专用函数；
- 预先知道电池页面结构并直接伪造读取结果。

测试夹具可以模拟屏幕，但真机验收必须由通用 Operator 基于当时 Observation 决定每一步。

## 10. 诊断信息

设备层应提供足够的只读诊断：ADB 标识、连接状态、最后观察时间、当前分辨率、前台包名和最近错误。完整 ADB 命令输出默认只在展开式详情或日志中显示。

