# 土豆片通用核心 · todoocard-general-core

TodooCard / T3 六色墨水屏卡片的跨平台核心：把图片转成 528×792 六色 payload，并通过 BLE 写入卡片。

这是对 macOS 专用 [TodooCard_Skills](https://github.com/Sunbelife/TodooCard_Skills) 的独立 Python 重写，不包含、不分发其 Swift 源码。macOS、Windows、Linux 共用同一套协议状态机，平台差异集中在 `todoocard_core/ble.py`。

## 能力

- **图片管线**：EXIF 校正 → cover 铺满 528×792 → Floyd–Steinberg 六色抖动 → 面板标定（180° + 水平翻转）→ 控制器顺序（顺时针 + 镜像）→ legacy 兼容包。
- **v157+ 可靠传输**：2 KB QuickLZ level-3 独立块、32 块一个应用层窗口、设备 Flash 检查点、断点续传与窗口修复。
- **legacy 固件**：pre-v157 的 64 字节 stored QuickLZ 分包格式。
- **安全配对契约**：加密 GATT 位、60 秒配对窗口、加密电量读取作为绑定证明；OTA 恢复模式拒绝传图。
- **失败诊断**：v165+ 保留的 image-ACK 诊断特征值。
- **跨平台**：macOS（CoreBluetooth）、Windows（WinRT）、Linux（BlueZ），统一用 [bleak](https://github.com/hbldh/bleak)。

## 安装

需要 Python 3.9+。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

HEIC/HEIF 输入额外安装：

```bash
.venv/bin/pip install pillow-heif
```

也可以 `pip install -e .` 后直接使用 `todoocard` 命令。

## 用法

```bash
# 扫描（默认 30 秒）
todoocard --list

# 配对：卡片需先进入物理配对窗口（长按背部按钮到指示灯快闪）
todoocard --pair --device-id <UUID 或 MAC>

# 校验绑定与加密链路
todoocard --probe --device-id <UUID 或 MAC>

# 只转换图片，不碰蓝牙
todoocard --input photo.jpg

# 转换并发送（必须显式 --send）
todoocard --input photo.jpg --device-id <UUID 或 MAC> --send

# 源图需要顺时针转 90° 时
todoocard --input photo.jpg --rotate-right-90 --device-id <UUID> --send
```

`--output DIR` 指定产物目录，默认是系统临时目录下的 `todoocard-general/`。产物：

| 文件 | 用途 |
| --- | --- |
| `<prefix>.bin` | 面板顺序六色位图（209088 字节，预览用） |
| `<prefix>.controller.bin` | 控制器顺序原始位图（v157+ raw 档） |
| `<prefix>.protocol.qlz` | v157+ QuickLZ payload |
| `<prefix>.protocol.legacy.qlz` | pre-v157 兼容 payload |

## 输出契约

CLI 的固定输出（思源插件和其他调用方依赖这些行判定结果）：

```text
Color mode: six-color (T3 native palette)
Wrote /path/image-xxxx.bin (209088 bytes)
Wrote /path/image-xxxx.controller.bin (209088 bytes)
Wrote /path/image-xxxx.protocol.legacy.qlz (218893 bytes)
Prepared v157+ payload: /path/image-xxxx.protocol.qlz
...
BLE TodooCard id=AA:BB:CC:DD:EE:FF RSSI=-61 services=... manufacturer=0x5053 screen=0x134C firmware=v167(raw=0xA7) flags=0x03 secure=yes pairing=closed recovery=no turbo=yes
Transfer completed and refresh acknowledged by 0x05/0x08
```

失败时输出 `Transfer failed: ...`，并在支持诊断的固件上追加 `Failure summary: ...`。

## 平台差异

| 平台 | 配对方式 | 备注 |
| --- | --- | --- |
| macOS | 读取加密的 Battery Level 触发系统绑定弹窗 | 首次配对需允许系统蓝牙提示 |
| Windows | `BleakClient.pair()` 建立系统绑定 | 绑定失败时到「设置 → 蓝牙」移除设备后重试 |
| Linux | `bluetoothctl` / BlueZ agent 提前配对 | 本核心只复用已有绑定 |

Windows 没有 `canSendWriteWithoutResponse`，核心用「窗口内连续不带响应写入 + 窗口末块带响应屏障 + 设备检查点」模拟流控；macOS 的 CoreBluetooth 路径由 bleak 转译，协议层完全一致。

**macOS 的 Service Changed 刷新（0.1.1）**：加密链路建立后卡片会下发 Service Changed，CoreBluetooth 会作废连接早期缓存的 GATT 树，而 bleak 不会自动重建。继续使用旧句柄时写命令拿不到 ATT 响应、notify 也永不回调，表现为「控制命令超时、收不到任何通知」。核心现在与 Swift 版一致：读电量确认加密后等待 1.8 秒，丢弃旧缓存并重新发现服务，再开始握手。

## 固件矩阵

| 固件 | 行为 |
| --- | --- |
| < v140 (`0x8C`) | 不要求加密 GATT |
| ≥ v140 | 加密 GATT；配对窗口 60 秒 |
| < v152 (`0x98`) | 配对键需按住 10 秒 |
| ≥ v157 (`0x9D`) | 2 KB QuickLZ + 32 块检查点（本核心的 verified 路径） |
| ≥ v165 (`0xA5`) | 失败后可读保留诊断 |
| OTA 恢复位 `0x08` | 拒绝图片传输，先修复固件 |

## 与上游 Swift 版的差异

- 颜色量化、面板标定、控制器顺序和协议状态机逐项对齐。
- **调色板内精确颜色**（文字卡、模板、纯色块）与 Swift 版逐字节一致；照片由于 CoreGraphics 与 Pillow 的重采样核不同，抖动相位会有差异，视觉等价但 md5 不同。
- 上游「先 probe 再正式连接」的 macOS 经验保留为连接重试；Windows 侧改为按地址/对象轮换重连并立即验证 GATT 链路。

## 测试

```bash
.venv/bin/pip install pytest
.venv/bin/pytest
```

测试覆盖：广告解析与描述契约、六色转换与面板/控制器坐标、legacy 包结构、QuickLZ 往返、CLI 输出契约、verified/legacy 传输状态机（含检查点、提前 0x08、缺失刷新确认）。

## 许可证

MIT。QuickLZ 编码器移植自 MIT 许可的 [todoocard-eink](https://github.com/shidaifufen/todoocard-eink)，详见 `NOTICE`。
