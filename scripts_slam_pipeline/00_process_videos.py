"""
python scripts_slam_pipeline/00_process_videos.py data_workspace/toss_objects/20231113
"""
# %%
import sys
import os
import re

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.append(ROOT_DIR)
os.chdir(ROOT_DIR)

# %%
import pathlib
import click
import shutil
from exiftool import ExifToolHelper
from umi.common.timecode_util import mp4_get_start_datetime


def parse_episode_gripper(filename):
    """Extract episode number and gripper id from filename.

    Example: 'run_20260107T161428Z_ep001_gp00_GX011810' -> ('ep001', 'gp00')
    Returns: (episode_str, gripper_str) or (None, None) if not matched
    """
    match = re.search(r'(ep\d+)_(gp\d+)', filename)
    if match:
        return match.group(1), match.group(2)
    return None, None


def validate_filename_format(filename, context=""):
    """Validate filename contains required ep{N}_gp{XX} pattern.

    Raises ValueError with detailed message if validation fails.
    """
    episode_str, gripper_str = parse_episode_gripper(filename)
    if episode_str is None or gripper_str is None:
        has_ep = re.search(r'ep\d+', filename) is not None
        has_gp = re.search(r'gp\d+', filename) is not None

        if has_ep and not has_gp:
            hint = "Found 'ep{N}' but missing 'gp{XX}'"
        elif has_gp and not has_ep:
            hint = "Found 'gp{XX}' but missing 'ep{N}'"
        elif '_ep' in filename.lower() or '_gp' in filename.lower():
            hint = "Pattern found but format incorrect (need ep{N}_gp{XX})"
        else:
            hint = "No episode/gripper pattern found"

        raise ValueError(
            f"Invalid filename format{context}:\n"
            f"  Actual:   '{filename}'\n"
            f"  Expected: '{{run_id}}_ep{{N}}_gp{{XX}}_{{gopro_id}}' format\n"
            f"  Example:  'run_20260107T161428Z_ep001_gp00_GX011810'\n"
            f"  Issue:    {hint}"
        )
    return episode_str, gripper_str


def get_gripper_prefix(gopro_filename):
    """Extract {run_id}_ep{N}_gp{XX} prefix from GoPro filename.

    Example: 'run_20260107T161428Z_ep001_gp00_GX011810' -> 'run_20260107T161428Z_ep001_gp00'
    """
    match = re.match(r'(.+_ep\d+_gp\d+)', gopro_filename)
    if match:
        return match.group(1)
    return None

# %%
@click.command(help='Session directories. Assumming mp4 videos are in <session_dir>/raw_videos')
@click.argument('session_dir', nargs=-1)
def main(session_dir):
    for session in session_dir:
        session = pathlib.Path(os.path.expanduser(session)).absolute()
        # hardcode subdirs
        input_dir = session.joinpath('raw_videos')
        output_dir = session.joinpath('demos')
        motor_datas_dir = session.joinpath('motor_datas')
        print(f"session: {session}")
        print(f"input_dir: {input_dir}")
        print(f"output_dir: {output_dir}")
        print(f"motor_datas_dir: {motor_datas_dir}")

        # create raw_videos if don't exist
        if not input_dir.is_dir():
            input_dir.mkdir()
            print(f"{input_dir.name} subdir don't exits! Creating one and moving all mp4 videos inside.")
            for mp4_path in list(session.glob('**/*.MP4')) + list(session.glob('**/*.mp4')):
                out_path = input_dir.joinpath(mp4_path.name)
                shutil.move(mp4_path, out_path)

        # create MP4 name map to imu json name
        mp4_name_to_imu_json_name = dict()
        mp4_name_to_motor_data_path = dict()
        mp4_name_to_motor_meta_data_path = dict()
        gripper_prefix_to_motor_data_path = dict()
        gripper_prefix_to_uvc_video_path = dict()
        gripper_prefix_to_uvc_data_path = dict()
        # 记录每个 gripper 的第一个视频（用于 calibration）
        gripper_id_first_video = dict()  # {'gp00': (start_date, mp4_path), ...}

        for mp4_path in list(input_dir.glob('**/*.MP4')) + list(input_dir.glob('**/*.mp4')):
            name_without_ext = mp4_path.with_suffix('').name

            # 跳过 mapping 文件
            if name_without_ext.startswith('mapping'):
                continue

            # 解析文件名格式
            episode_str, gripper_str = parse_episode_gripper(name_without_ext)
            if episode_str is None or gripper_str is None:
                print(f"Warning: Skipping '{mp4_path.name}' - filename does not match expected format")
                print(f"  Expected: '{{run_id}}_ep{{N}}_gp{{XX}}_{{gopro_id}}.MP4'")
                continue

            gripper_prefix = get_gripper_prefix(name_without_ext)

            # IMU 使用完整文件名
            imu_json_path = session.joinpath(name_without_ext + "_imu.json")
            if imu_json_path.exists():
                out_json_path = input_dir.joinpath(name_without_ext + "_imu.json")
                shutil.move(imu_json_path, out_json_path)
                mp4_name_to_imu_json_name[name_without_ext] = out_json_path

            # Motor 和 UVC 使用 gripper 前缀（避免重复处理）
            if gripper_prefix and gripper_prefix not in gripper_prefix_to_motor_data_path:
                # Motor data (JSONL format)
                motor_data_path = session.joinpath(gripper_prefix + "_motor.jsonl")
                if motor_data_path.exists():
                    out_motor_data_path = input_dir.joinpath(gripper_prefix + "_motor.jsonl")
                    shutil.move(motor_data_path, out_motor_data_path)
                    gripper_prefix_to_motor_data_path[gripper_prefix] = out_motor_data_path

                # UVC files
                uvc_video_path = session.joinpath(gripper_prefix + "_uvc.MP4")
                uvc_data_path = session.joinpath(gripper_prefix + "_uvc.jsonl")
                if uvc_video_path.exists() and uvc_data_path.exists():
                    out_uvc_video_path = input_dir.joinpath(gripper_prefix + "_uvc.MP4")
                    out_uvc_data_path = input_dir.joinpath(gripper_prefix + "_uvc.jsonl")
                    shutil.move(uvc_video_path, out_uvc_video_path)
                    shutil.move(uvc_data_path, out_uvc_data_path)
                    gripper_prefix_to_uvc_video_path[gripper_prefix] = out_uvc_video_path
                    gripper_prefix_to_uvc_data_path[gripper_prefix] = out_uvc_data_path
                elif uvc_video_path.exists() or uvc_data_path.exists():
                    print(f"Warning: UVC files incomplete for {gripper_prefix}. Need both video and timestamps.")

            # 记录每个 gripper 的第一个视频（按时间）
            if gripper_str:
                start_date = mp4_get_start_datetime(str(mp4_path))
                if gripper_str not in gripper_id_first_video or start_date < gripper_id_first_video[gripper_str][0]:
                    gripper_id_first_video[gripper_str] = (start_date, mp4_path)

        # create mapping video if don't exist
        mapping_vid_path = input_dir.joinpath('mapping.mp4')
        if (not mapping_vid_path.exists()) and not(mapping_vid_path.is_symlink()):
            max_size = -1
            max_path = None
            for mp4_path in list(input_dir.glob('**/*.MP4')) + list(input_dir.glob('**/*.mp4')):
                size = mp4_path.stat().st_size
                if size > max_size:
                    max_size = size
                    max_path = mp4_path

            print(f"max_path: {max_path}, mapping_vid_path: {mapping_vid_path}")
            shutil.move(max_path, mapping_vid_path)
            imu_json_path = mp4_name_to_imu_json_name.get(max_path.with_suffix('').name, None)
            if imu_json_path is not None:
                shutil.move(imu_json_path, input_dir.joinpath('mapping_imu.json'))
            motor_data_path = mp4_name_to_motor_data_path.get(max_path.with_suffix('').name, None)
            if motor_data_path is not None:
                shutil.move(motor_data_path, input_dir.joinpath('mapping_motor.npz'))
            motor_meta_data_path = mp4_name_to_motor_meta_data_path.get(max_path.with_suffix('').name, None)
            if motor_meta_data_path is not None:
                shutil.move(motor_meta_data_path, input_dir.joinpath('mapping_motor_meta.json'))
            print(f"raw_videos/mapping.mp4 don't exist! Renaming largest file {max_path.name}.")
        # create gripper calibration video if don't exist (one per gripper)
        for gripper_str, (start_date, mp4_path) in gripper_id_first_video.items():
            gripper_cal_dir = output_dir.joinpath(f'gripper_calibration_{gripper_str}')

            if gripper_cal_dir.is_dir():
                print(f"{gripper_cal_dir.name} already exists, skipping")
                continue

            gripper_cal_dir.mkdir(parents=True, exist_ok=True)
            print(f"Creating {gripper_cal_dir.name} with {mp4_path.name}")

            # Move video
            out_path = gripper_cal_dir.joinpath('raw_video.mp4')
            shutil.move(mp4_path, out_path)

            # Move IMU
            imu_path = mp4_name_to_imu_json_name.get(mp4_path.with_suffix('').name, None)
            if imu_path is not None:
                shutil.move(imu_path, gripper_cal_dir.joinpath("imu_data.json"))

            # Move motor data (use gripper prefix)
            gripper_prefix = get_gripper_prefix(mp4_path.with_suffix('').name)
            if gripper_prefix:
                motor_data_path = gripper_prefix_to_motor_data_path.get(gripper_prefix, None)
                if motor_data_path is not None:
                    shutil.move(motor_data_path, gripper_cal_dir.joinpath("motor_data.jsonl"))

                # Move UVC files
                uvc_video_path = gripper_prefix_to_uvc_video_path.get(gripper_prefix, None)
                if uvc_video_path is not None:
                    shutil.move(uvc_video_path, gripper_cal_dir.joinpath("uvc_video.mp4"))
                    uvc_data_path = gripper_prefix_to_uvc_data_path.get(gripper_prefix, None)
                    if uvc_data_path is not None:
                        shutil.move(uvc_data_path, gripper_cal_dir.joinpath("uvc_data.jsonl"))

        # look for mp4 video in all subdirectories in input_dir
        input_mp4_paths = list(input_dir.glob('**/*.MP4')) + list(input_dir.glob('**/*.mp4'))
        print(f'Found {len(input_mp4_paths)} MP4 videos')

        with ExifToolHelper() as et:
            for mp4_path in input_mp4_paths:
                if mp4_path.is_symlink():
                    print(f"Skipping {mp4_path.name}, already moved.")
                    continue

                # special folders
                if mp4_path.name.startswith('mapping'):
                    out_dname = "mapping"
                else:
                    name_without_ext = mp4_path.with_suffix('').name
                    episode_str, gripper_str = validate_filename_format(
                        name_without_ext,
                        context=f" in file '{mp4_path.name}'"
                    )
                    out_dname = f'demo_{episode_str}_{gripper_str}'

                # create directory
                this_out_dir = output_dir.joinpath(out_dname)
                this_out_dir.mkdir(parents=True, exist_ok=True)
                
                # move videos
                vfname = 'raw_video.mp4'
                out_video_path = this_out_dir.joinpath(vfname)
                shutil.move(mp4_path, out_video_path)

                # move imu jsons
                if out_dname == "mapping":
                    imu_path = input_dir.joinpath("mapping_imu.json")
                    out_imu_path = this_out_dir.joinpath("imu_data.json")
                    shutil.move(imu_path, out_imu_path)
                    motor_data_path = input_dir.joinpath("mapping_motor.npz")
                    out_motor_data_path = this_out_dir.joinpath("motor_data.npz")
                    shutil.move(motor_data_path, out_motor_data_path)
                    motor_meta_data_path = input_dir.joinpath("mapping_motor_meta.json")
                    out_motor_meta_data_path = this_out_dir.joinpath("motor_meta_data.json")
                    shutil.move(motor_meta_data_path, out_motor_meta_data_path)
                else:
                    imu_path = mp4_name_to_imu_json_name.get(mp4_path.with_suffix('').name, None)
                    if imu_path is not None:
                        shutil.move(imu_path, this_out_dir.joinpath("imu_data.json"))

                    # Move motor and UVC using gripper prefix
                    gripper_prefix = get_gripper_prefix(mp4_path.with_suffix('').name)
                    if gripper_prefix:
                        motor_data_path = gripper_prefix_to_motor_data_path.get(gripper_prefix, None)
                        if motor_data_path is not None:
                            shutil.move(motor_data_path, this_out_dir.joinpath("motor_data.jsonl"))

                        uvc_video_path = gripper_prefix_to_uvc_video_path.get(gripper_prefix, None)
                        if uvc_video_path is not None:
                            shutil.move(uvc_video_path, this_out_dir.joinpath("uvc_video.mp4"))
                            uvc_data_path = gripper_prefix_to_uvc_data_path.get(gripper_prefix, None)
                            if uvc_data_path is not None:
                                shutil.move(uvc_data_path, this_out_dir.joinpath("uvc_data.jsonl"))
                # create symlink back from original location
                # relative_to's walk_up argument is not avaliable until python 3.12
                dots = os.path.join(*['..'] * len(mp4_path.parent.relative_to(session).parts))
                rel_path = str(out_video_path.relative_to(session))
                symlink_path = os.path.join(dots, rel_path)                
                mp4_path.symlink_to(symlink_path)

# %%
if __name__ == '__main__':
    if len(sys.argv) == 1:
        main.main(['--help'])
    else:
        main()
