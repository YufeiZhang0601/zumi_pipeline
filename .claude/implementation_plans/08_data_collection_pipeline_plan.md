# Implementation Plan: Data Collection Pipeline Enhancement

## Summary

为采集流程添加 gripper ID 支持，使文件命名符合 `{run_id}_ep{N}_gp{XX}_...` 格式，并为未来双臂扩展做准备。

## 目标文件命名格式

### 输出文件格式
| 类型 | 文件名格式 | 示例 |
|------|-----------|------|
| GoPro | `{run_id}_ep{N}_gp{XX}_{gopro_id}.MP4` | `run_20260107T161428Z_ep001_gp00_GX011810.MP4` |
| IMU | `{run_id}_ep{N}_gp{XX}_{gopro_id}_imu.json` | `run_20260107T161428Z_ep001_gp00_GX011810_imu.json` |
| Motor | `{run_id}_ep{N}_gp{XX}_motor.jsonl` | `run_20260107T161428Z_ep001_gp00_motor.jsonl` |
| UVC | `{run_id}_ep{N}_gp{XX}_uvc.MP4` | `run_20260107T161428Z_ep001_gp00_uvc.MP4` |
| UVC ts | `{run_id}_ep{N}_gp{XX}_uvc.jsonl` | `run_20260107T161428Z_ep001_gp00_uvc.jsonl` |

## Implementation Steps

### Step 1: 更新 zumi_config.py

**添加 GripperMapping 配置类：**

```python
@dataclass
class GripperMapping:
    """Gripper 与设备的映射关系"""
    GRIPPER_ID: str = "gp00"           # gripper 标识符
    MOTOR_SLAVE_ID: int = 0x16         # 对应的电机从地址
    GOPRO_SN: str = None               # 对应的 GoPro 序列号（可选）
    GOPRO_IP: str = None               # 对应的 GoPro IP（可选）
    UVC_DEVICE: str = None             # 对应的 UVC 设备（可选）


# 单臂配置（当前使用）
GRIPPER_MAPPINGS: Dict[str, GripperMapping] = {
    "gp00": GripperMapping(
        GRIPPER_ID="gp00",
        MOTOR_SLAVE_ID=0x16,
    )
}

# 便捷函数
def get_default_gripper_id() -> str:
    """获取默认 gripper ID（第一个配置的）"""
    return next(iter(GRIPPER_MAPPINGS.keys()), "gp00")

def get_gripper_mapping(gripper_id: str) -> Optional[GripperMapping]:
    """根据 gripper_id 获取映射配置"""
    return GRIPPER_MAPPINGS.get(gripper_id)
```

**保留现有配置的兼容性：**
- `MOTOR_CONF` 继续存在，作为默认/后备配置
- 新代码优先使用 `GRIPPER_MAPPINGS`

---

### Step 2: 更新 node_motor.py

**修改 MotorNode 类：**

```python
class MotorNode(NodeHTTPService):
    def __init__(self, gripper_id: str = None):
        # 获取 gripper 配置
        self.gripper_id = gripper_id or get_default_gripper_id()
        mapping = get_gripper_mapping(self.gripper_id)

        # 使用映射的电机配置，或回退到默认
        slave_id = mapping.MOTOR_SLAVE_ID if mapping else MOTOR_CONF.SLAVE_ID

        self.driver = self._build_driver(slave_id=slave_id)

        super().__init__(
            name=f"motor_{self.gripper_id}",  # 节点名称包含 gripper_id
            host=HTTP_CONF.MOTOR_HOST,
            port=HTTP_CONF.MOTOR_PORT,
        )
```

**修改文件命名 (on_start_recording)：**

```python
def on_start_recording(self, run_id: str, episode: int, start_time: float = None):
    ep_tag = f"ep{int(episode):03d}"
    # 新格式：包含 gripper_id
    filename = f"{run_id}_{ep_tag}_{self.gripper_id}_motor.jsonl"
    self.motor_file = STORAGE_CONF.DATA_DIR / run_id / filename
```

**修改启动入口：**

```python
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--gripper-id", default=None, help="Gripper ID (e.g., gp00)")
    parser.add_argument("--port", type=int, default=HTTP_CONF.MOTOR_PORT)
    args = parser.parse_args()

    node = MotorNode(gripper_id=args.gripper_id)
    node.run(port=args.port)
```

---

### Step 3: 更新 node_uvc.py

**修改 UvcNode 类：**

```python
class UvcNode(NodeHTTPService):
    def __init__(self, gripper_id: str = None):
        self.gripper_id = gripper_id or get_default_gripper_id()

        super().__init__(
            name=f"uvc_{self.gripper_id}",
            host=HTTP_CONF.UVC_HOST,
            port=HTTP_CONF.UVC_PORT,
        )
```

**修改文件命名 (on_start_recording)：**

```python
def on_start_recording(self, run_id: str, episode: int, start_time: float = None):
    ep_tag = f"ep{int(episode):03d}"
    # 新格式：包含 gripper_id
    video_name = f"{run_id}_{ep_tag}_{self.gripper_id}_uvc.MP4"
    meta_name = f"{run_id}_{ep_tag}_{self.gripper_id}_uvc.jsonl"

    self.video_path = STORAGE_CONF.DATA_DIR / run_id / video_name
    self.meta_path = STORAGE_CONF.DATA_DIR / run_id / meta_name
```

**修改启动入口：**

```python
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--gripper-id", default=None, help="Gripper ID (e.g., gp00)")
    parser.add_argument("--port", type=int, default=HTTP_CONF.UVC_PORT)
    args = parser.parse_args()

    node = UvcNode(gripper_id=args.gripper_id)
    node.run(port=args.port)
```

---

### Step 4: 更新 node_gopro.py

**修改 GoProNode 类：**

```python
class GoProNode(NodeHTTPService):
    def __init__(self, gripper_id: str = None):
        self.gripper_id = gripper_id or get_default_gripper_id()
        mapping = get_gripper_mapping(self.gripper_id)

        # 使用映射的 GoPro 配置
        gopro_ip = mapping.GOPRO_IP if mapping else GOPRO_CONF.IP
        gopro_sn = mapping.GOPRO_SN if mapping else GOPRO_CONF.SN

        self.cam = GoProController(ip=gopro_ip, sn=gopro_sn)

        super().__init__(
            name=f"gopro_{self.gripper_id}",
            host=HTTP_CONF.GOPRO_HOST,
            port=HTTP_CONF.GOPRO_PORT,
        )
```

**修改下载文件命名 (_download_one)：**

```python
def _download_one(self, run_id, episode, folder, filename):
    ep_tag = f"ep{int(episode):03d}"
    gopro_basename = Path(filename).stem  # e.g., "GX011810"

    # 新格式：包含 gripper_id
    save_name = f"{run_id}_{ep_tag}_{self.gripper_id}_{filename}"
    # 结果: run_20260107T161428Z_ep001_gp00_GX011810.MP4

    # IMU 文件命名
    imu_name = f"{run_id}_{ep_tag}_{self.gripper_id}_{gopro_basename}_imu.json"

    # 电机文件重命名（从旧格式到新格式）
    old_motor = run_dir / f"{run_id}_{ep_tag}_{self.gripper_id}_motor.jsonl"
    # 注意：电机文件已经包含 gripper_id，无需再添加 gopro_basename
```

---

### Step 5: 更新 orchestrator.py

**修改节点客户端创建：**

```python
def create_node_clients() -> List[NodeClient]:
    """根据配置创建节点客户端"""
    clients = []

    # 为每个配置的 gripper 创建节点
    for gripper_id, mapping in GRIPPER_MAPPINGS.items():
        # GoPro 节点
        if mapping.GOPRO_SN or mapping.GOPRO_IP:
            gopro_url = f"http://127.0.0.1:{8001}"  # 或根据 gripper_id 计算端口
            clients.append(NodeClient(f"gopro_{gripper_id}", gopro_url))

        # Motor 节点
        motor_url = f"http://127.0.0.1:{8002}"
        clients.append(NodeClient(f"motor_{gripper_id}", motor_url))

        # UVC 节点
        uvc_url = f"http://127.0.0.1:{8003}"
        clients.append(NodeClient(f"uvc_{gripper_id}", uvc_url))

    return clients
```

**单臂简化版本（推荐先实现）：**

```python
# 保持现有结构，只更新节点名称
gripper_id = get_default_gripper_id()
clients = [
    NodeClient(f"gopro_{gripper_id}", HTTP_CONF.GOPRO_URL),
    NodeClient(f"motor_{gripper_id}", HTTP_CONF.MOTOR_URL),
    NodeClient(f"uvc_{gripper_id}", HTTP_CONF.UVC_URL),
]
```

---

### Step 6: 更新 validator.py

**修改验证函数以支持 gripper_id：**

```python
def validate_episode(run_id: str, episode: int, gripper_id: str = None) -> ValidationResult:
    """验证指定 episode 的数据完整性"""
    gripper_id = gripper_id or get_default_gripper_id()
    ep_tag = f"ep{int(episode):03d}"

    run_dir = STORAGE_CONF.DATA_DIR / run_id

    # 检查各文件
    motor_pattern = f"{run_id}_{ep_tag}_{gripper_id}_motor.jsonl"
    uvc_video_pattern = f"{run_id}_{ep_tag}_{gripper_id}_uvc.MP4"
    uvc_meta_pattern = f"{run_id}_{ep_tag}_{gripper_id}_uvc.jsonl"
    gopro_pattern = f"{run_id}_{ep_tag}_{gripper_id}_*.MP4"  # GoPro ID 动态匹配

    # ... 验证逻辑
```

---

## 双臂扩展说明

### 配置示例

当需要支持双臂时，只需修改 `zumi_config.py`：

```python
GRIPPER_MAPPINGS = {
    "gp00": GripperMapping(
        GRIPPER_ID="gp00",
        MOTOR_SLAVE_ID=0x16,
        GOPRO_IP="172.21.151.51",
    ),
    "gp01": GripperMapping(
        GRIPPER_ID="gp01",
        MOTOR_SLAVE_ID=0x17,
        GOPRO_IP="172.22.152.51",
    ),
}
```

### 启动命令（双臂）

```bash
# 右臂
python node_motor.py --gripper-id gp00 --port 8002
python node_uvc.py --gripper-id gp00 --port 8003
python node_gopro.py --gripper-id gp00 --port 8001

# 左臂
python node_motor.py --gripper-id gp01 --port 8012
python node_uvc.py --gripper-id gp01 --port 8013
python node_gopro.py --gripper-id gp01 --port 8011
```

---

## 测试计划

### 单元测试

1. **配置测试**
   - `test_get_default_gripper_id()` 返回正确的默认值
   - `test_get_gripper_mapping()` 返回正确的映射

2. **命名测试**
   - `test_motor_filename_format()` 验证电机文件命名
   - `test_uvc_filename_format()` 验证 UVC 文件命名
   - `test_gopro_filename_format()` 验证 GoPro 文件命名

### 集成测试

1. **单臂采集测试**
   - 启动所有节点（使用默认 gp00）
   - 运行一次采集
   - 验证所有文件命名正确

2. **向后兼容测试**
   - 不传递 gripper_id 参数
   - 验证使用默认配置

---

## 改动摘要

| 文件 | 改动类型 | 影响 |
|------|---------|------|
| `zumi_config.py` | 新增 | 添加 GripperMapping 配置 |
| `node_motor.py` | 修改 | 文件命名加 gripper_id |
| `node_uvc.py` | 修改 | 文件命名加 gripper_id |
| `node_gopro.py` | 修改 | 下载命名加 gripper_id |
| `orchestrator.py` | 修改 | 节点名称更新 |
| `validator.py` | 修改 | 支持 gripper_id 参数 |

---

## 风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| 现有数据格式不兼容 | 提供迁移脚本重命名旧文件 |
| 节点名称变化导致 ZMQ 问题 | 更新 orchestrator 的 expected_names |
| 双臂模式端口冲突 | 使用端口偏移规则 (gp01 = base + 10) |
