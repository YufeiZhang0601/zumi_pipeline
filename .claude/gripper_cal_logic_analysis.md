# 夹爪初始状态约束：完整图景与改进方案

## 一、当前系统的核心逻辑

### 1.1 数据流全貌

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         gripper_calibration 阶段                             │
│  标准动作：完全闭合 → 完全打开 → 闭合 → 打开 ... (反复5次)                      │
│  目的：建立 motor弧度 ↔ tag宽度(米) 的精确映射                                 │
│  输出：gripper_cal_interp, min_width, max_width                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                              采集阶段                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  prepare()                                                                  │
│    ├─ 检查 position < 0.1  ←── 强制夹爪闭合                                  │
│    └─ set_zero()           ←── 当前位置 = 0（弧度）                          │
│                                                                             │
│  start() → 前 0.5s 锁定    ←── 提供稳定的校准窗口                             │
│         → 之后自由运动                                                       │
│                                                                             │
│  数据：                                                                      │
│    motor.jsonl: { ts, pos: [弧度] }  ← 相对于 set_zero() 的位置               │
│    GoPro 视频 + ArUco 标签                                                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                              处理阶段                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  Step 1: Tag 宽度提取                                                        │
│    tag_width = right_tag_x - left_tag_x (米，绝对测量)                        │
│    gripper_widths = gripper_cal_interp(tag_width) - min_width                │
│                                                                             │
│  Step 2: Motor 处理（当前逻辑）                                               │
│    close_pos = median(motor_pos[前0.5s])  ← 假设前0.5s是闭合静止               │
│    motor_widths = (motor_pos - close_pos) * ratio                            │
│    ratio = tag_span / motor_span_rad                                         │
│                                                                             │
│  Step 3: Cross-Correlation 时间对齐                                          │
│    找 motor 和 tag 信号的时间偏移                                             │
│                                                                             │
│  Step 4: 最终 gripper_width                                                  │
│    使用对齐后的高频 motor 数据                                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 当前约束的真正目的

| 约束 | 表面目的 | 深层目的 |
|------|----------|----------|
| **position < 0.1** | 确保闭合 | 让 set_zero() 在物理闭合位置，使 motor_pos ≈ 物理开度 |
| **set_zero()** | 清零 | 每个 episode 独立校准，避免累积误差 |
| **前 0.5s 锁定** | 准备时间 | 1. 提供稳定的 close_pos 计算窗口<br>2. 方便时间同步（已知起点） |
| **close_pos = median(前0.5s)** | 找闭合位置 | 校准 motor弧度 → 米 的零点 |

### 1.3 为什么需要每个 episode 校准？

```
问题：motor 是增量编码器，只知道"变化量"，不知道"绝对位置"

解决方案：
  1. 每个 episode 开始时 set_zero() → motor_pos = 0
  2. 要求此时夹爪物理闭合 → motor_pos = 0 = 物理闭合
  3. 处理时用 close_pos 验证/修正这个假设

如果初始不是闭合：
  - set_zero() 后 motor_pos = 0，但物理上可能是打开的
  - 处理端 close_pos = 0，但这不是真正的闭合位置
  - 整个宽度曲线的零点错误
```

## 二、问题场景

### 2.1 擦东西任务（一开始夹住物体）

```
用户期望：  打开(夹住) → 操作 → 释放 → 再夹 → ...

当前系统：
  1. prepare 拒绝（position >= 0.1）
  2. 即使绕过，set_zero() 把"打开"设为 0
  3. close_pos = 0，但物理上不是闭合
  4. 宽度计算错误
```

### 2.2 当前隐式假设

1. 每个 episode 从完全闭合开始
2. 前 0.5s 是静止闭合状态
3. motor_pos 的最小值在前 0.5s

## 三、核心问题：Motor 与物理宽度的对应

### 3.1 问题分析

```
每轮 set_zero() 的影响：
  ┌──────────────────────────────────────────────────────────────┐
  │  Episode 1                                                    │
  │    set_zero() 时夹爪完全闭合                                    │
  │    → motor_pos = 0 = 物理闭合                                  │
  │    → motor_pos = 0.3 rad = 物理打开 X 米                        │
  ├──────────────────────────────────────────────────────────────┤
  │  Episode 2                                                    │
  │    set_zero() 时夹爪稍微没闭紧（差 0.02 rad）                    │
  │    → motor_pos = 0 = 物理上差 0.02 rad 才闭合                   │
  │    → motor_pos = 0.3 rad ≠ Episode 1 的物理位置                 │
  ├──────────────────────────────────────────────────────────────┤
  │  Episode 3（新场景：一开始夹住物体）                             │
  │    set_zero() 时夹爪打开 0.2 rad                                │
  │    → motor_pos = 0 = 物理打开！                                 │
  │    → motor_pos 的语义完全不同                                   │
  └──────────────────────────────────────────────────────────────┘

问题：如何将每个 episode 的 motor_pos 映射到一致的物理宽度？
```

### 3.2 Tag 是唯一的绝对参考

```
Tag 测量的特性：
  - 直接测量物理宽度（米）
  - 与 motor set_zero() 无关
  - 每帧都是绝对值

关键洞察：
  - tag_width = 0.01m → 物理闭合
  - tag_width = 0.08m → 物理打开
  - 这个映射在所有 episode 中一致（来自 gripper_calibration）
```

### 3.3 改进方案：先对齐，再用 Tag 确定 close_pos

**核心思路：不假设 motor 的任何值对应闭合，而是用 Tag 来告诉我们**

```
Step 1: 形状对齐（不需要绝对值）
  motor_signal = motor_pos - mean(motor_pos)  # 只保留形状
  tag_signal = tag_widths - mean(tag_widths)
  t_offset = cross_correlation(tag_signal, motor_signal)

Step 2: 对齐后，用 Tag 确定 close_pos
  # 找 tag 最接近闭合的时刻
  t_close = argmin(tag_widths)

  # 在对齐后的 motor 时间轴上，找这个时刻的 motor_pos
  motor_at_close = interp(t_close, motor_ts + t_offset, motor_pos)
  close_pos = motor_at_close  # 这就是物理闭合对应的 motor 值

Step 3: 计算 motor_widths
  motor_widths = (motor_pos - close_pos) * ratio
  # 当 motor_pos = close_pos 时，motor_widths = 0（物理闭合）
```

**为什么有效？**

```
无论 set_zero() 时夹爪处于什么状态：
  - Tag 知道什么时候是物理闭合（tag_width 最小）
  - 对齐后，可以找到那个时刻的 motor_pos
  - 用这个 motor_pos 作为 close_pos
  - motor_widths = 0 真正对应物理闭合
```

## 四、改进方案

### 4.1 采集端改动 (node_motor.py)

**改动 1：移除强制检查，改为警告**

```python
# 行 158-164
if state.position >= 0.1:
    logger.warning(
        f"Gripper position ({state.position:.3f}) >= 0.1, "
        "starting with gripper open"
    )
# 仍然调用 set_zero()，但不 return False
self.driver.set_zero()
```

**改动 2：lock_duration 可配置**

```python
# zumi_config.py
class MOTOR_CONF:
    LOCK_DURATION = 0.5  # 默认 0.5s，可设为 0

# node_motor.py
self.lock_duration = getattr(MOTOR_CONF, 'LOCK_DURATION', 0.5)
```

### 4.2 处理端改动 (06_generate_dataset_plan.py)

**核心改动：先对齐，再用线性变换把 motor 转为绝对物理宽度**

```python
# ========== 当前代码流程 ==========
# 1. 假设前 0.5s 是闭合 → 计算 close_pos
# 2. 计算 motor_widths（相对值，最小=0）
# 3. 做时间对齐

# ========== 新代码流程 ==========
# 1. 先做时间对齐（只用形状，不需要绝对值）
# 2. 对齐后，用 Tag 的 min/max 时刻建立线性映射
# 3. 把 motor 转换为绝对物理宽度（米）

# --- Step 1: 形状对齐 ---
# 归一化 motor（处理正负极性：用绝对值的变化）
motor_centered = motor_pos - np.mean(motor_pos)
motor_range = np.percentile(np.abs(motor_centered), 98)
motor_normalized = motor_centered / motor_range

# tag 也归一化
tag_centered = tag_widths_smooth - np.mean(tag_widths_smooth)
tag_range = np.percentile(np.abs(tag_centered), 98)
tag_normalized = tag_centered / tag_range

# Cross-correlation 找时间偏移（可能需要检查正负相关）
motor_resampled = np.interp(full_video_timestamps, motor_ts, motor_normalized)
correlation = np.correlate(tag_normalized, motor_resampled, mode='full')
best_lag = lags[np.argmax(np.abs(correlation))]  # 用 abs 处理负相关
t_offset = best_lag / fps

motor_ts_aligned = motor_ts + t_offset

# --- Step 2: 用 Tag 的 min/max 时刻建立线性映射 ---
# 找 tag 的闭合和打开时刻
tag_min_idx = np.argmin(tag_widths_smooth)  # 闭合时刻
tag_max_idx = np.argmax(tag_widths_smooth)  # 打开时刻

# 在这两个时刻采样对齐后的 motor
t_close = full_video_timestamps[tag_min_idx]
t_open = full_video_timestamps[tag_max_idx]
motor_at_close = float(np.interp(t_close, motor_ts_aligned, motor_pos))
motor_at_open = float(np.interp(t_open, motor_ts_aligned, motor_pos))

tag_close = tag_widths_smooth[tag_min_idx]
tag_open = tag_widths_smooth[tag_max_idx]

# 计算线性变换参数（自动处理正负极性）
# tag = motor * ratio + offset
motor_diff = motor_at_open - motor_at_close
if abs(motor_diff) < 1e-6:
    print(f"Skipping: motor range too small")
    continue

ratio = (tag_open - tag_close) / motor_diff  # 可正可负
offset = tag_close - motor_at_close * ratio

# --- Step 3: 转换 motor 为绝对物理宽度 ---
motor_widths = motor_pos * ratio + offset
motor_widths = np.clip(motor_widths, 0.0, max_width - min_width)
```

**关键改进**：
1. **绝对物理宽度**：motor_widths 是真实物理尺寸（米），不是相对于某个零点
2. **自动处理极性**：ratio 可正可负，无论 motor 哪个方向是"打开"都能正确转换
3. **用时间对应点**：通过 tag 的 min/max 时刻找对应的 motor 值，避免 percentile 的极性问题

### 4.3 方案优势

```
新方案 vs 旧方案：

旧方案：
  close_pos = median(前 0.5s)
  motor_widths = (motor_pos - close_pos) * ratio
  假设：前 0.5s 是物理闭合
  问题：
    1. 如果初始不是闭合，close_pos 错误
    2. motor_widths 最小值 = 0（相对值）
    3. 不同 episode 的 "0" 可能对应不同物理宽度

新方案：
  motor_widths = motor_pos * ratio + offset
  ratio/offset 由 tag 的 min/max 时刻确定
  优势：
    1. 不依赖初始状态假设
    2. motor_widths 是绝对物理宽度（米）
    3. 所有 episode 的宽度值语义一致
    4. 自动处理 motor 正负极性
```

### 4.4 边界情况处理

#### 情况 1：整个 episode 夹爪运动范围很小

```
场景：一直夹着物体操作，tag_width 和 motor_pos 变化很小

问题：
  - motor_diff = motor_at_open - motor_at_close 很小
  - ratio = (tag_open - tag_close) / motor_diff 会变得很大
  - 放大 motor 噪声，导致 motor_widths 不稳定

检测与处理：
  # 检测 tag 范围
  tag_span = tag_open - tag_close
  if tag_span < 0.005:  # 5mm 阈值
      logger.warning(f"Episode {ep}: tag span too small ({tag_span:.4f}m), skipping")
      continue

  # 检测 motor 范围（已有逻辑）
  if abs(motor_diff) < 1e-4:  # 约 0.006 度
      logger.warning(f"Episode {ep}: motor range too small, skipping")
      continue
```

#### 情况 2：Tag 检测缺失或不完整

```
场景：
  a) 部分帧 tag 被遮挡（手挡住、离开画面）
  b) 整个 episode tag 检测率很低
  c) tag_min/tag_max 时刻恰好在遮挡区域

问题：
  - argmin/argmax 可能选到异常帧（噪声、错误检测）
  - interp 外推可能产生错误值
  - 线性映射建立在不可靠的数据点上

检测与处理：
  # 检测有效帧比例
  valid_ratio = np.sum(~np.isnan(tag_widths_raw)) / len(tag_widths_raw)
  if valid_ratio < 0.3:  # 30% 阈值
      logger.warning(f"Episode {ep}: low tag detection rate ({valid_ratio:.1%}), skipping")
      continue

  # 只在有效帧上找 min/max
  valid_mask = ~np.isnan(tag_widths_smooth)
  valid_indices = np.where(valid_mask)[0]
  valid_widths = tag_widths_smooth[valid_mask]

  tag_min_local_idx = np.argmin(valid_widths)
  tag_max_local_idx = np.argmax(valid_widths)
  tag_min_idx = valid_indices[tag_min_local_idx]
  tag_max_idx = valid_indices[tag_max_local_idx]

  # 验证 min/max 时刻附近有足够有效帧（不是孤立点）
  window = 5  # 前后各 5 帧
  for idx, name in [(tag_min_idx, 'min'), (tag_max_idx, 'max')]:
      start = max(0, idx - window)
      end = min(len(tag_widths_smooth), idx + window + 1)
      local_valid = np.sum(valid_mask[start:end])
      if local_valid < window:  # 至少一半有效
          logger.warning(f"Episode {ep}: tag {name} in sparse region, may be unreliable")
```

#### 情况 3：时间对齐失败

```
场景：
  a) 夹爪几乎不动，cross-correlation 无法找到明显峰值
  b) motor 和 tag 运动模式不匹配（比如 motor 有高频抖动）
  c) 相位差接近半周期，正负相关峰值相近

问题：
  - t_offset 错误，导致 motor_at_close/open 取到错误时刻
  - 最终 ratio 和 offset 完全错误

检测与处理：
  # 检测相关性强度
  correlation_normalized = correlation / (len(tag_normalized) * np.std(tag_normalized) * np.std(motor_resampled))
  max_corr = np.max(np.abs(correlation_normalized))

  if max_corr < 0.3:  # 弱相关阈值
      logger.warning(f"Episode {ep}: weak correlation ({max_corr:.2f}), alignment may be unreliable")
      # 绝对不回退, 绝对不静默, 宁愿不要这组数据

  # 检测峰值唯一性（是否有多个相近的峰）
  peaks, properties = scipy.signal.find_peaks(np.abs(correlation_normalized), height=max_corr * 0.8)
  if len(peaks) > 1:
      logger.warning(f"Episode {ep}: multiple correlation peaks, alignment ambiguous")
      # 绝对不回退, 绝对不静默, 宁愿宁愿不要这组数据

  # 检测 t_offset 是否在合理范围（比如 ±2 秒）
  if abs(t_offset) > 2.0:
      logger.warning(f"Episode {ep}: large time offset ({t_offset:.2f}s), checking data integrity")
      # 绝对不回退,  绝对不静默, 宁愿不要这组数据
```

#### 情况 4：Motor 极性问题

```
场景：
  - 不同硬件或配置下，motor 正方向可能不同
  - motor_at_open > motor_at_close（打开时弧度增加）
  - motor_at_open < motor_at_close（打开时弧度减少）

处理：
  新方案已自动处理：
  - ratio = (tag_open - tag_close) / (motor_at_open - motor_at_close)
  - ratio 可正可负
  - motor_widths = motor_pos * ratio + offset 自动正确

验证：
  # 检查 ratio 的符号和量级
  expected_ratio_magnitude = 0.1  # 典型值约 0.1 m/rad
  if abs(abs(ratio) - expected_ratio_magnitude) > 0.05:
      logger.warning(f"Episode {ep}: unusual ratio {ratio:.4f}, expected ~{expected_ratio_magnitude}")

  # 验证结果合理性
  motor_widths_range = motor_widths.max() - motor_widths.min()
  if motor_widths_range < 0.01 or motor_widths_range > 0.15:
      logger.warning(f"Episode {ep}: unusual width range {motor_widths_range:.4f}m")
```

#### 情况 5：argmin/argmax 受单帧异常影响

```
场景：
  - tag_widths 有个别异常大或异常小的值（检测错误）
  - argmin/argmax 选中异常帧

问题：
  - motor_at_close/open 对应的是异常时刻
  - 线性映射建立在错误数据点上

检测与处理：
  # 方案 A：使用 percentile 代替 argmin/argmax（更鲁棒但可能不准）
  # 不推荐：percentile 在极性未知时有问题

  # 方案 B：使用多帧平均（推荐）
  # 找到最小/最大区域，取该区域的平均值

  # 找 tag 最小的 N 帧
  N = 10
  sorted_indices = np.argsort(tag_widths_smooth[valid_mask])
  min_region_indices = valid_indices[sorted_indices[:N]]
  max_region_indices = valid_indices[sorted_indices[-N:]]

  # 计算区域中心时刻
  t_close = np.mean(full_video_timestamps[min_region_indices])
  t_open = np.mean(full_video_timestamps[max_region_indices])

  # 使用平均值而非单点
  tag_close = np.mean(tag_widths_smooth[min_region_indices])
  tag_open = np.mean(tag_widths_smooth[max_region_indices])

  motor_at_close = float(np.interp(t_close, motor_ts_aligned, motor_pos))
  motor_at_open = float(np.interp(t_open, motor_ts_aligned, motor_pos))
```

#### 情况 6：时间戳边界问题

```
场景：
  - motor_ts 和 video_timestamps 时间范围不一致
  - 对齐后 motor_ts_aligned 超出 video 范围
  - t_close/t_open 超出 motor_ts_aligned 范围

问题：
  - np.interp 会外推（使用边界值），可能导致错误

检测与处理：
  # 检测时间范围重叠
  motor_start = motor_ts_aligned[0]
  motor_end = motor_ts_aligned[-1]
  video_start = full_video_timestamps[0]
  video_end = full_video_timestamps[-1]

  overlap_start = max(motor_start, video_start)
  overlap_end = min(motor_end, video_end)

  if overlap_end - overlap_start < 0.5:  # 至少 0.5s 重叠
      logger.error(f"Episode {ep}: insufficient time overlap, skipping")
      continue

  # 确保 t_close/t_open 在重叠区域内
  if t_close < overlap_start or t_close > overlap_end:
      logger.warning(f"Episode {ep}: t_close ({t_close:.2f}s) outside overlap, clamping")
      t_close = np.clip(t_close, overlap_start, overlap_end)

  if t_open < overlap_start or t_open > overlap_end:
      logger.warning(f"Episode {ep}: t_open ({t_open:.2f}s) outside overlap, clamping")
      t_open = np.clip(t_open, overlap_start, overlap_end)
```

#### 情况 7：负宽度或超范围宽度

```
场景：
  - 线性外推导致 motor_widths 出现负值
  - motor_widths 超过物理最大宽度

处理（已有）：
  motor_widths = np.clip(motor_widths, 0.0, max_width - min_width)

额外检测：
  # 统计裁剪比例
  clipped_low = np.sum(motor_widths_raw < 0) / len(motor_widths_raw)
  clipped_high = np.sum(motor_widths_raw > max_width - min_width) / len(motor_widths_raw)

  if clipped_low > 0.05:
      logger.warning(f"Episode {ep}: {clipped_low:.1%} samples clipped to 0")
  if clipped_high > 0.05:
      logger.warning(f"Episode {ep}: {clipped_high:.1%} samples clipped to max")

  # 如果裁剪比例过高，标记数据质量问题
  if clipped_low + clipped_high > 0.2:
      logger.error(f"Episode {ep}: excessive clipping, check calibration")
```

#### 边界情况处理总结

| 情况 | 检测方法 | 处理策略 |
|------|----------|----------|
| 运动范围太小 | tag_span < 5mm 或 motor_diff < 1e-4 | 跳过 episode |
| Tag 检测率低 | valid_ratio < 30% | 跳过 episode |
| min/max 在稀疏区域 | 检查局部有效帧数 | 警告 |
| 对齐相关性弱 | max_corr < 0.3 | 警告或回退 |
| 时间偏移过大 | \|t_offset\| > 2s | 检查数据 |
| ratio 异常 | 偏离典型值过多 | 警告 |
| 单帧异常影响 | - | 用 N 帧平均替代单点 |
| 时间范围不重叠 | overlap < 0.5s | 跳过 episode |
| 宽度超范围 | 裁剪比例 > 5% | 警告 |

## 五、完整改动清单

| 文件 | 位置 | 改动 |
|------|------|------|
| `node_motor.py` | 行 158-164 | error → warning，不再 return False |
| `node_motor.py` | 行 41 | `self.lock_duration = getattr(MOTOR_CONF, 'LOCK_DURATION', 0.5)` |
| `zumi_config.py` | MOTOR_CONF | 添加 `LOCK_DURATION = 0.5` |
| `06_generate_dataset_plan.py` | 行 704-760 | 重构为"先对齐，再用 Tag 确定 close_pos" |

## 六、验证计划

### 6.1 采集测试

```bash
# 测试 1：打开状态 prepare
# 预期：警告但成功

# 测试 2：lock_duration=0
# 修改 zumi_config.py: LOCK_DURATION = 0
# 预期：录制开始立即可移动夹爪
```

### 6.2 处理测试

```bash
# 测试 1：传统数据（闭合→打开→闭合）
# 预期：close_pos 与旧代码接近（因为 tag_min 时刻 ≈ 前 0.5s）

# 测试 2：新任务数据（打开→释放→再夹）
# 预期：close_pos 对应真正闭合时刻，gripper_width 最小值 ≈ 0
```

### 6.3 对比验证

```python
# 添加调试输出
print(f"Old close_pos (0.5s median): {old_close_pos:.4f}")
print(f"New close_pos (tag-based): {new_close_pos:.4f}")
print(f"t_close (tag min time): {t_close:.3f}s")
print(f"Difference: {abs(old_close_pos - new_close_pos):.4f}")
```

## 七、风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| 采集端改动破坏流程 | 低 | 仅改为 warning，不改变 set_zero 逻辑 |
| 处理端结果不同 | 中 | 回归测试 + 对比输出 |
| 对齐精度影响 close_pos | 中 | 归一化后对齐更鲁棒 |
| 无闭合动作的 episode | 低 | tag_min 仍是最接近闭合的时刻 |

## 八、推理时的单位转换问题

### 8.1 为什么必须用物理宽度（米）训练

```
如果用弧度训练的问题：
  Episode 1: set_zero() 在闭合 → 0 rad = 闭合
  Episode 2: set_zero() 在打开 → 0 rad = 打开

  模型看到的 "0 rad" 在不同 episode 中意义完全不同！
  → 模型无法学到一致的语义

用物理宽度训练：
  所有 episode: 0 米 = 闭合, 0.08 米 = 打开
  → 模型学到一致的物理语义
```

### 8.2 推理时的数据流

```
训练数据：gripper_width (米) - 物理语义一致
模型输出：gripper_width (米)
执行命令：motor_pos (rad) - 控制器需要

问题：如何从 米 转换回 弧度？

转换公式：
  motor_pos = gripper_width_meters / ratio + offset

  ratio = 米/弧度 比例（来自 gripper_calibration）
  offset = 当前零点对应的物理宽度
```

### 8.3 推理时的解决方案：运行时标定

```
推理启动时：
  1. 让夹爪完全闭合（物理闭合）
  2. set_zero() → 此时 0 rad = 物理闭合
  3. 加载 gripper_calibration 中的 ratio 和 min_width

推理时转换（绝对物理宽度 → 弧度）：
  # 训练数据中：gripper_width = gripper_cal_interp(tag) - min_width
  # 所以 gripper_width = 0 对应物理闭合

  motor_pos = gripper_width_meters / ratio

  # ratio 来自 gripper_calibration 的 (tag_span / motor_span)
  # 因为推理启动时 set_zero 在闭合位置，所以 offset = 0

这确保了：
  - 模型输出 0 米 → 发送 0 rad → 夹爪闭合
  - 模型输出 0.06 米 → 发送对应 rad → 夹爪打开到 0.06m
```

### 8.4 需要的额外改动

```
1. gripper_calibration 阶段：
   - 保存 radian_to_meter_ratio 到配置文件
   - 或计算 meter_to_radian_ratio = 1 / radian_to_meter_ratio

2. 推理代码（umi_env.py 或类似）：
   - 加载 ratio
   - 在发送命令前做转换：
     motor_cmd = gripper_width_meters * meter_to_radian_ratio

3. 推理启动流程：
   - 添加"闭合并 set_zero"的步骤
   - 确保 0 rad = 物理闭合
```

## 九、完整方案总结

### 核心洞察

```
问题根源：
  - Motor 是增量编码器，每轮 set_zero() 后零点不同
  - 前 0.5s 假设只在"初始闭合"时有效
  - 推理时需要 米→弧度 转换

解决思路：
  采集处理端：
    - Tag 是绝对测量，用 Tag 确定 close_pos
    - 输出物理宽度（米），语义一致

  推理端：
    - 启动时标定：闭合 + set_zero
    - 用固定 ratio 做 米→弧度 转换
```

### 改动范围

| 阶段 | 文件 | 改动 |
|------|------|------|
| 采集 | node_motor.py | 移除强制闭合检查，lock_duration 可配置 |
| 处理 | 06_generate_dataset_plan.py | 先对齐，再用 Tag 确定 close_pos |
| 标定 | gripper_calibration | 保存 ratio 到配置 |
| 推理 | umi_env.py | 加载 ratio，做 米→弧度 转换 |
| 推理 | 启动流程 | 添加"闭合+set_zero"标定步骤 |

### 数据语义保证

```
训练数据：
  gripper_width = 0 米     → 物理闭合
  gripper_width = 0.08 米  → 物理打开（取决于校准）

推理执行：
  模型输出 0 米     → motor_cmd = 0 rad    → 夹爪闭合
  模型输出 0.08 米  → motor_cmd = X rad    → 夹爪打开

  一致的物理语义，端到端正确
```
