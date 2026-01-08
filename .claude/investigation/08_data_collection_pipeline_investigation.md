# Investigation: Data Collection Pipeline Enhancement

## 1. 调研目标

调研现有采集流程，为支持 gripper ID 映射和未来双臂扩展做准备。

## 2. 现有架构分析

### 2.1 核心组件

| 文件 | 职责 |
|------|------|
| `orchestrator.py` | 中央调度，管理录制生命周期 |
| `node_motor.py` | 电机节点，200Hz 采样 |
| `node_gopro.py` | GoPro 节点，视频录制和下载 |
| `node_uvc.py` | UVC 相机节点，60fps 视频 |
| `zumi_core.py` | 节点基类，HTTP 服务框架 |
| `zumi_config.py` | 集中配置 |

### 2.2 当前配置结构 (zumi_config.py)

```python
@dataclass
class MotorConfig:
    DRIVER: str = "dm"
    SLAVE_ID: int = 0x16           # 硬编码单电机
    MASTER_ID: int = 0x26
    SERIAL_PORT: str = "/dev/dm_can0"

@dataclass
class GoProConfig:
    SN: str = None
    IP: str = None

@dataclass
class UvcConfig:
    DEVICE: str = "/dev/v4l/by-id/..."
    RESOLUTION: tuple = (640, 480)
    FPS: int = 60
```

**问题：**
- 无 gripper ID 概念
- 电机配置硬编码，无法支持多电机
- 设备与 gripper 无映射关系

### 2.3 文件命名规则（当前）

采集时生成：
```
{run_id}_ep{episode:03d}_motor.jsonl          # 电机原始
{run_id}_ep{episode:03d}_uvc.MP4              # UVC 视频
{run_id}_ep{episode:03d}_uvc.jsonl            # UVC 元数据
```

GoPro 下载后重命名：
```
{run_id}_ep{episode:03d}_{gopro_id}.MP4       # 视频
{run_id}_ep{episode:03d}_{gopro_id}_imu.json  # IMU
{run_id}_ep{episode:03d}_{gopro_id}_motor.npz # 电机（同步后）
```

**问题：**
- 缺少 `gp{XX}` gripper 标识符
- 无法区分多个 gripper 的数据

### 2.4 已重命名数据格式（目标格式）

```
run_20260107T161428Z_ep001_gp00_GX011810.MP4
run_20260107T161428Z_ep001_gp00_GX011810_imu.json
run_20260107T161428Z_ep001_gp00_motor.jsonl
run_20260107T161428Z_ep001_gp00_uvc.MP4
run_20260107T161428Z_ep001_gp00_uvc.jsonl
```

**关键变化：** 在 `ep{N}` 后增加 `gp{XX}` 标识符

## 3. 录制流程详解

### 3.1 状态机

```
IDLE -> READY -> RECORDING -> SAVING -> IDLE
           \                     /
            \-> ERROR <-> RECOVERING
```

### 3.2 流程步骤

1. **Prepare**: Orchestrator 发送 `/prepare` 到所有节点
2. **Start**: 计算同步时间戳，发送 `/start`
3. **Recording**: 各节点独立采集
4. **Stop**: 发送 `/stop`，节点保存数据
5. **Download**: GoPro 下载视频，重命名电机文件

### 3.3 关键函数

| 函数 | 文件 | 作用 |
|------|------|------|
| `do_prepare()` | orchestrator.py:526 | 发送准备命令 |
| `do_start()` | orchestrator.py:542 | 启动录制 |
| `do_stop()` | orchestrator.py:564 | 停止录制 |
| `_download_one()` | node_gopro.py:308 | 下载并重命名 |

## 4. 需要修改的位置

### 4.1 zumi_config.py

**新增配置：**

```python
@dataclass
class GripperMapping:
    """Gripper 与设备的映射关系"""
    GRIPPER_ID: str = "gp00"           # gripper 标识符
    MOTOR_SLAVE_ID: int = 0x16         # 对应的电机从地址
    GOPRO_SN: str = None               # 对应的 GoPro 序列号（可选）
    GOPRO_IP: str = None               # 对应的 GoPro IP（可选）
    UVC_DEVICE: str = None             # 对应的 UVC 设备（可选）

# 单臂配置示例
GRIPPER_MAPPINGS = {
    "gp00": GripperMapping(
        GRIPPER_ID="gp00",
        MOTOR_SLAVE_ID=0x16,
    )
}

# 双臂配置示例（未来）
# GRIPPER_MAPPINGS = {
#     "gp00": GripperMapping(GRIPPER_ID="gp00", MOTOR_SLAVE_ID=0x16),
#     "gp01": GripperMapping(GRIPPER_ID="gp01", MOTOR_SLAVE_ID=0x17),
# }
```

### 4.2 node_motor.py

**修改点：**
1. 接受 `gripper_id` 参数
2. 文件命名包含 `gp{XX}`

```python
# 当前
save_path = f"{run_id}_ep{episode:03d}_motor.jsonl"

# 修改后
save_path = f"{run_id}_ep{episode:03d}_{self.gripper_id}_motor.jsonl"
```

### 4.3 node_gopro.py

**修改点：**
1. 下载时使用 gripper_id 而非 gopro_id
2. 重命名逻辑更新

```python
# 当前
save_name = f"{run_id}_{ep_tag}_{filename}"

# 修改后
save_name = f"{run_id}_{ep_tag}_{self.gripper_id}_{filename}"
```

### 4.4 node_uvc.py

**修改点：**
1. 接受 `gripper_id` 参数
2. 文件命名包含 `gp{XX}`

```python
# 当前
video_path = f"{run_id}_ep{episode:03d}_uvc.MP4"
meta_path = f"{run_id}_ep{episode:03d}_uvc.jsonl"

# 修改后
video_path = f"{run_id}_ep{episode:03d}_{self.gripper_id}_uvc.MP4"
meta_path = f"{run_id}_ep{episode:03d}_{self.gripper_id}_uvc.jsonl"
```

### 4.5 orchestrator.py

**修改点：**
1. 从配置读取 gripper 映射
2. 传递 gripper_id 给各节点

## 5. 双臂扩展考虑

### 5.1 架构变化

```
单臂模式（当前）：
┌─────────────────────────────┐
│ Orchestrator                │
│   ├── GoPro Node (8001)     │
│   ├── Motor Node (8002)     │
│   └── UVC Node (8003)       │
└─────────────────────────────┘

双臂模式（未来）：
┌─────────────────────────────────────────────────┐
│ Orchestrator                                    │
│   ├── GoPro Node gp00 (8001)                   │
│   ├── Motor Node gp00 (8002)                   │
│   ├── UVC Node gp00 (8003)                     │
│   ├── GoPro Node gp01 (8011)  [可选]           │
│   ├── Motor Node gp01 (8012)                   │
│   └── UVC Node gp01 (8013)    [可选]           │
└─────────────────────────────────────────────────┘
```

### 5.2 配置扩展

双臂只需修改 `zumi_config.py`：

```python
GRIPPER_MAPPINGS = {
    "gp00": GripperMapping(
        GRIPPER_ID="gp00",
        MOTOR_SLAVE_ID=0x16,
        GOPRO_SN="C3150123",
    ),
    "gp01": GripperMapping(
        GRIPPER_ID="gp01",
        MOTOR_SLAVE_ID=0x17,
        GOPRO_SN="C3150456",
    ),
}
```

### 5.3 HTTP 端口规划

| 设备类型 | gp00 端口 | gp01 端口 |
|---------|----------|----------|
| GoPro | 8001 | 8011 |
| Motor | 8002 | 8012 |
| UVC | 8003 | 8013 |

## 6. 数据完整性验证

### 6.1 当前验证逻辑 (validator.py)

- 检查视频文件存在
- 检查 IMU 数据有效
- 检查电机数据时间戳连续
- 检查 UVC 视频与元数据匹配

### 6.2 扩展验证

需要验证同一 episode 的所有 gripper 数据完整：

```python
def validate_episode(run_id, episode):
    for gripper_id in GRIPPER_MAPPINGS.keys():
        validate_gripper_data(run_id, episode, gripper_id)
```

## 7. 向后兼容性

### 7.1 默认行为

- 如果未配置 `GRIPPER_MAPPINGS`，使用默认 `gp00`
- 现有代码路径保持兼容

### 7.2 迁移策略

1. Phase 1: 添加 gripper_id 支持，默认 `gp00`
2. Phase 2: 更新所有节点使用新命名
3. Phase 3: 添加多 gripper 支持

## 8. 总结

### 8.1 核心改动范围

| 文件 | 改动量 | 说明 |
|------|--------|------|
| `zumi_config.py` | 中 | 新增 GripperMapping 配置 |
| `node_motor.py` | 小 | 文件命名加 gripper_id |
| `node_gopro.py` | 小 | 下载命名加 gripper_id |
| `node_uvc.py` | 小 | 文件命名加 gripper_id |
| `orchestrator.py` | 小 | 传递 gripper_id |

### 8.2 风险评估

- **低风险**: 改动集中在文件命名，不影响核心采集逻辑
- **向后兼容**: 默认配置保持单臂行为
- **可测试**: 可以在不影响生产的情况下验证
