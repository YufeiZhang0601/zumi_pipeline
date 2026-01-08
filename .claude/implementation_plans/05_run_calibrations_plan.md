# Implementation Plan: 05_run_calibrations.py & calibrate_gripper_range.py

## Summary
Adapt calibration scripts to use gripper_id from directory name instead of tag detection.

## 问题背景

**当前流程**：
```
gripper_calibration_gp00/
       ↓
05_run_calibrations.py 调用 calibrate_gripper_range.py
       ↓
calibrate_gripper_range.py 用 tag detection 推断 gripper_id
       ↓
写入 gripper_range.json: {"gripper_id": X, ...}
```

**问题**：
- 目录名 `gripper_calibration_gp00` 已明确 gripper_id = 0
- 但 `calibrate_gripper_range.py` 仍用 tag detection 推断
- 两者可能不一致

## 解决方案

### 方案 A：从目录名传入 gripper_id（推荐）

**修改 1: calibrate_gripper_range.py - 新增 --gripper_id 参数**
```python
@click.command()
@click.option('-i', '--input', required=True, help='Tag detection pkl')
@click.option('-o', '--output', required=True, help='output json')
@click.option('-g', '--gripper_id', type=int, default=None, help='Gripper hardware ID (if not provided, infer from tags)')
@click.option('-t', '--tag_det_threshold', type=float, default=0.5)
@click.option('-nz', '--nominal_z', type=float, default=0.034)
def main(input, output, gripper_id, tag_det_threshold, nominal_z):
    tag_detection_results = pickle.load(open(input, 'rb'))

    # 如果提供了 gripper_id，直接使用；否则从 tag detection 推断
    if gripper_id is None:
        # 原有的 tag detection 推断逻辑
        gripper_id = infer_gripper_id_from_tags(tag_detection_results, tag_det_threshold)
    else:
        print(f"Using provided gripper_id: {gripper_id}")

    # ... 后续标定逻辑不变 ...
```

**修改 2: 05_run_calibrations.py - 从目录名解析 gripper_id 并传入**
```python
import re

def parse_gripper_id_from_dir(dir_name):
    """Parse gripper id from directory name like 'gripper_calibration_gp00'. Raises error if not found."""
    match = re.search(r'gp(\d+)', dir_name)
    if not match:
        raise ValueError(f"Directory name {dir_name} does not contain 'gpXX' pattern")
    return int(match.group(1))

# 在 gripper range calibration 循环中
for gripper_dir in demos_dir.glob("gripper_calibration*"):
    gripper_range_path = gripper_dir.joinpath('gripper_range.json')
    tag_path = gripper_dir.joinpath('tag_detection.pkl')
    assert tag_path.is_file()

    # 从目录名解析 gripper_id
    gripper_id = parse_gripper_id_from_dir(gripper_dir.name)

    cmd = [
        sys.executable, str(script_path),
        '--input', str(tag_path),
        '--output', str(gripper_range_path),
        '--gripper_id', str(gripper_id)
    ]

    subprocess.run(cmd)
```

### 方案 B：只在 06 中从目录名解析（最小改动）

如果不想改 05 和 calibrate_gripper_range.py，可以只改 06（见 06_generate_dataset_plan_plan.md Change 2）。

**但推荐方案 A**，同时改 05 和 06，保证 pipeline 一致性。

## 推荐

**推荐方案 A**：保持整个 pipeline 的 gripper_id 来源一致（全部来自目录名），避免潜在的不一致问题。

Tag detection 仍可保留用于：
1. 校验目录名中的 gripper_id 是否正确（可选警告）

**注意**：不支持旧数据格式（无 gp 前缀的目录）。如果目录名不包含 `gp{XX}`，脚本将抛出 ValueError 并停止。

## Testing
1. `gripper_calibration_gp00/` 正确传入 gripper_id=0
2. `gripper_range.json` 中的 gripper_id 与目录名一致
3. 缺少 gp 前缀的目录（如 `gripper_calibration_old/`）会抛出 ValueError 导致程序退出
