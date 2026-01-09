"""Motor-to-Tag alignment utilities for gripper calibration."""

import numpy as np
from scipy import signal
from scipy.signal import medfilt, savgol_filter
from scipy.ndimage import label as scipy_label
from typing import Tuple, List, Dict


def interpolate_nan(arr: np.ndarray) -> np.ndarray:
    """Fill NaN values using linear interpolation."""
    valid_mask = ~np.isnan(arr)
    if valid_mask.sum() < 2:
        raise ValueError("Too few valid samples for interpolation (need at least 2)")
    valid_indices = np.where(valid_mask)[0]
    valid_values = arr[valid_mask]
    return np.interp(np.arange(len(arr)), valid_indices, valid_values)


def preprocess_tag_signal(
    tag_widths: np.ndarray,
    medfilt_kernel: int = 31,
    savgol_window: int = 15,
    savgol_polyorder: int = 2
) -> np.ndarray:
    """
    Preprocess tag width signal: interpolate NaN + medfilt for spike removal + savgol smoothing.

    Args:
        tag_widths: Raw tag width array (may contain NaN)
        medfilt_kernel: Median filter kernel size (odd number)
        savgol_window: Savitzky-Golay filter window length
        savgol_polyorder: Savitzky-Golay filter polynomial order

    Returns:
        Preprocessed signal (NaN-free)
    """
    # 1. Linear interpolation to fill NaN
    result = interpolate_nan(tag_widths.copy())

    # 2. Median filter to remove spikes
    result = medfilt(result, kernel_size=medfilt_kernel)

    # 3. Savitzky-Golay filter for smoothing
    if len(result) > savgol_window:
        result = savgol_filter(result, window_length=savgol_window, polyorder=savgol_polyorder)

    return result


def normalize_z_score(arr: np.ndarray) -> np.ndarray:
    """Z-score normalization: (x - mean) / std"""
    arr_std = np.std(arr)
    if arr_std < 1e-9:
        return np.zeros_like(arr)
    return (arr - np.mean(arr)) / arr_std


def cross_correlate_diff_signals(
    tag_signal: np.ndarray,
    motor_signal: np.ndarray,
    fps: float,
    max_lag_sec: float = 2.0
) -> Tuple[int, float, float]:
    """
    Time alignment using cross-correlation of differentiated signals.

    Args:
        tag_signal: Preprocessed tag width signal
        motor_signal: Resampled motor position signal (polarity already applied)
        fps: Frame rate
        max_lag_sec: Maximum search range in seconds

    Returns:
        (best_lag_frames, t_offset_sec, max_correlation)
    """
    # 1. Compute derivatives
    tag_diff = np.diff(tag_signal)
    motor_diff = np.diff(motor_signal)

    # 2. Z-score normalization
    tag_diff_norm = normalize_z_score(tag_diff)
    motor_diff_norm = normalize_z_score(motor_diff)

    # 3. Cross-correlation
    correlation = signal.correlate(tag_diff_norm, motor_diff_norm, mode='full')
    n = len(tag_diff_norm)
    lags = np.arange(-(n-1), n)

    # 4. Limit search range to +/-max_lag_sec
    max_lag_frames = int(max_lag_sec * fps)
    center = n - 1
    search_start = max(0, center - max_lag_frames)
    search_end = min(len(correlation), center + max_lag_frames)

    # 5. Find maximum within limited range
    search_corr = correlation[search_start:search_end]
    best_local_idx = np.argmax(search_corr)
    best_lag_idx = search_start + best_local_idx
    best_lag = lags[best_lag_idx]

    t_offset = best_lag / fps
    max_corr = correlation[best_lag_idx] / n

    return best_lag, t_offset, max_corr


def shift_signal(arr: np.ndarray, lag: int) -> np.ndarray:
    """Shift signal by lag frames."""
    result = np.zeros_like(arr)
    if lag > 0:
        result[lag:] = arr[:-lag]
    elif lag < 0:
        result[:lag] = arr[-lag:]
    else:
        result[:] = arr
    return result


def find_stable_regions(
    signal_arr: np.ndarray,
    diff_threshold: float = 1e-3,
    min_duration_frames: int = 60
) -> List[Dict]:
    """
    Detect stable regions in signal (change rate below threshold for sufficient duration).

    Args:
        signal_arr: Input signal
        diff_threshold: Frame-to-frame difference threshold
        min_duration_frames: Minimum duration in frames

    Returns:
        List of stable region dictionaries
    """
    # Compute absolute frame-to-frame difference
    abs_diff = np.abs(np.diff(signal_arr, prepend=signal_arr[0]))

    # Mark stable frames
    is_stable = abs_diff < diff_threshold

    # Find contiguous stable regions
    labeled_array, num_features = scipy_label(is_stable)

    valid_segments = []
    for i in range(1, num_features + 1):
        indices = np.where(labeled_array == i)[0]
        if len(indices) >= min_duration_frames:
            avg_pos = np.mean(signal_arr[indices])
            valid_segments.append({
                'indices': indices,
                'length': len(indices),
                'avg_pos': avg_pos,
                'start': indices[0],
                'end': indices[-1]
            })

    return valid_segments


def find_reference_points(
    tag_widths: np.ndarray,
    motor_pos_aligned: np.ndarray,
    diff_threshold: float = 1e-3,
    min_duration_frames: int = 60
) -> Tuple[Dict, Dict]:
    """
    Find close and open reference points.

    - Close: argmin of tag_widths
    - Open: Stable region with max motor position, OR simple argmax for calibration data

    NOTE: This function supports two scenarios (NOT a fallback):
    - Calibration data: Quick open/close without long holds -> uses argmax
    - Task data: Has stable holding periods -> uses stable region detection
    Both are valid algorithms for their respective data patterns.

    Returns:
        (close_point, open_point)
    """
    # Close point: simple argmin
    idx_close = np.argmin(tag_widths)
    close_point = {
        'index': idx_close,
        'width': tag_widths[idx_close],
        'motor': motor_pos_aligned[idx_close]
    }

    # Open point: stable region with max motor position
    stable_regions = find_stable_regions(motor_pos_aligned, diff_threshold, min_duration_frames)

    if not stable_regions:
        # Calibration scenario: no stable regions (quick open/close motion)
        # Use simple argmax - this is a valid approach for calibration data
        idx_open = np.argmax(tag_widths)
        open_point = {
            'index': idx_open,
            'width': tag_widths[idx_open],
            'motor': motor_pos_aligned[idx_open]
        }
    else:
        # Task scenario: has stable holding periods
        # Choose region with maximum average motor position
        best_segment = max(stable_regions, key=lambda x: x['avg_pos'])
        indices = best_segment['indices']
        open_point = {
            'index': int((indices[0] + indices[-1]) / 2),
            'width': np.mean(tag_widths[indices]),
            'motor': np.mean(motor_pos_aligned[indices]),
            'segment': best_segment  # for debugging
        }

    return close_point, open_point


def calculate_linear_mapping(
    close_point: Dict,
    open_point: Dict
) -> Tuple[float, float]:
    """
    Calculate linear mapping parameters: width = ratio * motor_pos + offset

    Returns:
        (ratio, offset)
    """
    motor_diff = open_point['motor'] - close_point['motor']
    width_diff = open_point['width'] - close_point['width']

    if abs(motor_diff) < 1e-6:
        raise ValueError("Motor positions for open/close are too close")

    ratio = width_diff / motor_diff
    offset = open_point['width'] - ratio * open_point['motor']

    return ratio, offset
