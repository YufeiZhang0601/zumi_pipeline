"""
python scripts_slam_pipeline/01_extract_gopro_imu.py data_workspace/cup_in_the_wild/20240105_zhenjia_packard_2nd_conference_room
"""
# %%
import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.append(ROOT_DIR)
os.chdir(ROOT_DIR)

# %%
import pathlib
import click
import subprocess
import multiprocessing
import concurrent.futures
from tqdm import tqdm

# %%
@click.command()
@click.option('-d', '--docker_image', default="zumi/gpmf-extract:latest",
              help="IMU extractor image. Default uses the in-repo newer-gopro-telemetry "
                   "image (build via: docker build -t zumi/gpmf-extract:latest docker/gpmf_extract). "
                   "Pass chicheng/openicc:latest to fall back to the legacy image.")
@click.option('-n', '--num_workers', type=int, default=None)
@click.option('-p', '--docker_pull', is_flag=True, default=False,
              help="Force a docker pull before running. Default skips pulling and uses local cache. "
                   "Not meaningful for zumi/* local images.")
@click.argument('session_dir', nargs=-1)
def main(docker_image, num_workers, docker_pull, session_dir):
    if num_workers is None:
        num_workers = multiprocessing.cpu_count()

    is_local_only = docker_image.startswith('zumi/')

    # Check local image exists
    inspect = subprocess.run(
        ['docker', 'image', 'inspect', docker_image],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    has_local = inspect.returncode == 0

    if is_local_only:
        if not has_local:
            print(f"Image {docker_image} not found locally. Build it first:")
            print(f"  docker build -t {docker_image} docker/gpmf_extract")
            exit(1)
        print(f"Using local-only image {docker_image}")
    elif docker_pull or not has_local:
        reason = "forced by --docker_pull" if docker_pull else "image not found locally"
        print(f"Pulling docker image {docker_image} ({reason})")
        p = subprocess.run(['docker', 'pull', docker_image])
        if p.returncode != 0:
            print("Docker pull failed!")
            exit(1)
    else:
        print(f"Using local cached image {docker_image} (pass --docker_pull to refresh)")

    # zumi/gpmf-extract has an ENTRYPOINT, chicheng/openicc needs explicit node command.
    def build_extract_cmd(video_dir, video_path, json_path):
        base = [
            'docker', 'run', '--rm',
            '--volume', f"{video_dir}:/data",
            docker_image,
        ]
        if is_local_only:
            return base + [str(video_path), str(json_path)]
        return base + [
            'node',
            '/OpenImuCameraCalibrator/javascript/extract_metadata_single.js',
            str(video_path), str(json_path),
        ]

    for session in session_dir:
        input_dir = pathlib.Path(os.path.expanduser(session)).joinpath('demos')
        input_video_dirs = [x.parent for x in input_dir.glob('*/raw_video.mp4')]
        print(f'Found {len(input_video_dirs)} video dirs')

        with tqdm(total=len(input_video_dirs)) as pbar:
            # one chunk per thread, therefore no synchronization needed
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = set()
                for video_dir in tqdm(input_video_dirs):
                    video_dir = video_dir.absolute()
                    if video_dir.joinpath('imu_data.json').is_file():
                        print(f"imu_data.json already exists, skipping {video_dir.name}")
                        continue
                    mount_target = pathlib.Path('/data')

                    video_path = mount_target.joinpath('raw_video.mp4')
                    json_path = mount_target.joinpath('imu_data.json')

                    # run imu extractor
                    cmd = build_extract_cmd(str(video_dir), video_path, json_path)

                    stdout_path = video_dir.joinpath('extract_gopro_imu_stdout.txt')
                    stderr_path = video_dir.joinpath('extract_gopro_imu_stderr.txt')

                    if len(futures) >= num_workers:
                        # limit number of inflight tasks
                        completed, futures = concurrent.futures.wait(futures, 
                            return_when=concurrent.futures.FIRST_COMPLETED)
                        pbar.update(len(completed))

                    futures.add(executor.submit(
                        lambda x, stdo, stde: subprocess.run(x, 
                            cwd=str(video_dir),
                            stdout=stdo.open('w'),
                            stderr=stde.open('w')), 
                        cmd, stdout_path, stderr_path))
                    # print(' '.join(cmd))

                completed, futures = concurrent.futures.wait(futures)
                pbar.update(len(completed))

        print("Done! Result:")
        print([x.result() for x in completed])

# %%
if __name__ == "__main__":
    main()
