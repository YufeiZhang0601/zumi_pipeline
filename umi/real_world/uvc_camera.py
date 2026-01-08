from typing import Optional, Callable, Dict
import enum
import time
import cv2
import numpy as np
import multiprocessing as mp
from threadpoolctl import threadpool_limits
from multiprocessing.managers import SharedMemoryManager
from umi.common.timestamp_accumulator import get_accumulate_timestamp_idxs
from umi.shared_memory.shared_memory_ring_buffer import SharedMemoryRingBuffer
from umi.shared_memory.shared_memory_queue import SharedMemoryQueue, Full, Empty
from umi.real_world.video_recorder import VideoRecorder
from umi.common.usb_util import reset_usb_device

class Command(enum.Enum):
    RESTART_PUT = 0
    START_RECORDING = 1
    STOP_RECORDING = 2

class UvcCamera(mp.Process):
    """
    Call umi.common.usb_util.reset_all_elgato_devices
    if you are using Elgato capture cards.
    Required to workaround firmware bugs.
    """
    MAX_PATH_LENGTH = 4096 # linux path has a limit of 4096 bytes
    
    def __init__(
            self,
            shm_manager: SharedMemoryManager,
            # v4l2 device file path
            # e.g. /dev/video0
            # or /dev/v4l/by-id/usb-Elgato_Elgato_HD60_X_A00XB320216MTR-video-index0
            dev_video_path,
            resolution=(1280, 720),
            capture_fps=60,
            exposure: Optional[float] = None,
            fourcc: Optional[str] = None,
            put_fps=None,
            put_downsample=True,
            get_max_k=30,
            receive_latency=0.0,
            cap_buffer_size=1,
            num_threads=2,
            transform: Optional[Callable[[Dict], Dict]] = None,
            vis_transform: Optional[Callable[[Dict], Dict]] = None,
            recording_transform: Optional[Callable[[Dict], Dict]] = None,
            video_recorder: Optional[VideoRecorder] = None,
            verbose=False
        ):
        super().__init__()

        if put_fps is None:
            put_fps = capture_fps
        
        # create ring buffer
        resolution = tuple(resolution)
        shape = resolution[::-1]
        examples = {
            'color': np.empty(
                shape=shape+(3,), dtype=np.uint8)
        }
        examples['camera_capture_timestamp'] = 0.0
        examples['camera_receive_timestamp'] = 0.0
        examples['timestamp'] = 0.0
        examples['step_idx'] = 0

        vis_ring_buffer = SharedMemoryRingBuffer.create_from_examples(
            shm_manager=shm_manager,
            examples=examples if vis_transform is None 
                else vis_transform(dict(examples)),
            get_max_k=1,
            get_time_budget=0.2,
            put_desired_frequency=capture_fps
        )

        ring_buffer = SharedMemoryRingBuffer.create_from_examples(
            shm_manager=shm_manager,
            examples=examples if transform is None
                else transform(dict(examples)),
            get_max_k=get_max_k,
            get_time_budget=0.2,
            put_desired_frequency=put_fps
        )

        # create command queue
        examples = {
            'cmd': Command.RESTART_PUT.value,
            'put_start_time': 0.0,
            'video_path': np.array('a'*self.MAX_PATH_LENGTH),
            'recording_start_time': 0.0,
        }

        command_queue = SharedMemoryQueue.create_from_examples(
            shm_manager=shm_manager,
            examples=examples,
            buffer_size=128
        )

        # create video recorder
        if video_recorder is None:
            # default to nvenc GPU encoder
            video_recorder = VideoRecorder.create_hevc_nvenc(
                shm_manager=shm_manager,
                fps=capture_fps, 
                input_pix_fmt='bgr24', 
                bit_rate=6000*1000)
        assert video_recorder.fps == capture_fps

        # copied variables
        self.shm_manager = shm_manager
        self.dev_video_path = dev_video_path
        self.resolution = resolution
        self.capture_fps = capture_fps
        self.put_fps = put_fps
        self.put_downsample = put_downsample
        self.receive_latency = receive_latency
        self.cap_buffer_size = cap_buffer_size
        self.exposure = exposure
        self.fourcc = fourcc
        self.transform = transform
        self.vis_transform = vis_transform
        self.recording_transform = recording_transform
        self.video_recorder = video_recorder
        self.verbose = verbose
        self.put_start_time = None
        self.num_threads = num_threads

        # shared variables
        self.stop_event = mp.Event()
        self.ready_event = mp.Event()
        self.ring_buffer = ring_buffer
        self.vis_ring_buffer = vis_ring_buffer
        self.command_queue = command_queue

    # ========= context manager ===========
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    # ========= user API ===========
    def start(self, wait=True, put_start_time=None):
        self.put_start_time = put_start_time
        shape = self.resolution[::-1]
        data_example = np.empty(shape=shape+(3,), dtype=np.uint8)

        # 第一次调用：setup + start_process
        if not self.video_recorder.is_setup:
            self.video_recorder.setup(
                shm_manager=self.shm_manager,
                data_example=data_example
            )
            self.video_recorder.start_process()
        else:
            # 后续调用：只准备录制
            self.video_recorder.prepare_recording()

        # must start video recorder first to create share memories
        super().start()
        if wait:
            self.start_wait()
    
    def stop(self, wait=True):
        self.video_recorder.stop()
        self.stop_event.set()
        if wait:
            self.end_wait()

    def start_wait(self):
        self.ready_event.wait()
        self.video_recorder.start_wait()
    
    def end_wait(self):
        self.join()
        self.video_recorder.end_wait()

    @property
    def is_ready(self):
        return self.ready_event.is_set()

    def get(self, k=None, out=None):
        if k is None:
            return self.ring_buffer.get(out=out)
        else:
            return self.ring_buffer.get_last_k(k, out=out)
    
    def get_vis(self, out=None):
        return self.vis_ring_buffer.get(out=out)

    def start_recording(self, video_path: str, start_time: float=-1):
        path_len = len(video_path.encode('utf-8'))
        if path_len > self.MAX_PATH_LENGTH:
            raise RuntimeError('video_path too long.')
        self.command_queue.put({
            'cmd': Command.START_RECORDING.value,
            'video_path': video_path,
            'recording_start_time': start_time
        })
        
    def stop_recording(self):
        self.command_queue.put({
            'cmd': Command.STOP_RECORDING.value
        })
    
    def restart_put(self, start_time):
        self.command_queue.put({
            'cmd': Command.RESTART_PUT.value,
            'put_start_time': start_time
        })

    # ========= interval API ===========
    def run(self):
        # limit threads
        threadpool_limits(self.num_threads)
        cv2.setNumThreads(self.num_threads)

        # open VideoCapture
        cap = cv2.VideoCapture(self.dev_video_path, cv2.CAP_V4L2)
        if not cap.isOpened():
            print(f"[UvcCamera] Failed to open device {self.dev_video_path}", flush=True)
            return

        # Force manual exposure if provided
        if self.exposure is not None:
            try:
                cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)  # manual mode on V4L2
                cap.set(cv2.CAP_PROP_EXPOSURE, float(self.exposure))
            except Exception:
                pass
        
        try:
            # set fourcc format first (must be before resolution/fps)
            w, h = self.resolution
            fps = self.capture_fps
            if self.fourcc:
                fourcc_code = cv2.VideoWriter_fourcc(*self.fourcc)
                cap.set(cv2.CAP_PROP_FOURCC, fourcc_code)
                # Verify FOURCC setting took effect
                actual_fourcc_raw = int(cap.get(cv2.CAP_PROP_FOURCC))
                actual_fourcc = ''.join([chr((actual_fourcc_raw >> 8*i) & 0xFF) for i in range(4)])
                if actual_fourcc != self.fourcc:
                    print(f"[UvcCamera] WARNING: requested FOURCC={self.fourcc}, actual={actual_fourcc}", flush=True)

            # set resolution and fps
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, self.cap_buffer_size)
            cap.set(cv2.CAP_PROP_FPS, fps)

            # validate settings took effect
            actual_fps = cap.get(cv2.CAP_PROP_FPS)
            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if actual_fps != fps:
                print(f"[UvcCamera] WARNING: requested FPS={fps}, actual={actual_fps}", flush=True)
            if (actual_w, actual_h) != (w, h):
                print(f"[UvcCamera] WARNING: requested resolution={w}x{h}, actual={actual_w}x{actual_h}", flush=True)

            # put frequency regulation
            put_idx = None
            put_start_time = self.put_start_time
            if put_start_time is None:
                put_start_time = time.time()

            # reuse frame buffer
            iter_idx = 0
            t_start = time.time()
            t_fps_measure_start = None
            rec_active = False
            last_t_cal = None
            while not self.stop_event.is_set():
                ts = time.time()
                ret = cap.grab()
                if not ret:
                    print("[UvcCamera] cap.grab failed", flush=True)
                    time.sleep(0.01)
                    continue

                # Only request recorder buffers while actively recording
                # and VideoRecorder is accepting frames (not flushing)
                use_recorder = rec_active and self.video_recorder.is_accepting_frames()
                frame = None
                if use_recorder:
                    try:
                        frame = self.video_recorder.get_img_buffer()
                    except Full:
                        use_recorder = False

                if use_recorder:
                    ret, frame = cap.retrieve(frame)
                else:
                    ret, frame = cap.retrieve()
                t_recv = time.time()
                if not ret:
                    print("[UvcCamera] cap.retrieve failed", flush=True)
                    time.sleep(0.01)
                    continue
                mt_cap = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
                t_cap = mt_cap - time.monotonic() + time.time()
                t_cal = t_recv - self.receive_latency # calibrated latency
                last_t_cal = t_cal
                     
                # record frame
                # Double-check is_accepting_frames in case flush started after get_img_buffer
                if use_recorder and self.video_recorder.is_accepting_frames():
                    try:
                        self.video_recorder.write_img_buffer(frame, frame_time=t_cal)
                    except Full:
                        if self.verbose:
                            print("[UvcCamera] Recorder queue full; dropping frame", flush=True)

                data = dict()
                data['camera_receive_timestamp'] = t_recv
                data['camera_capture_timestamp'] = t_cap
                data['color'] = frame
                
                # apply transform
                put_data = data
                if self.transform is not None:
                    put_data = self.transform(dict(data))

                # Track whether we wrote to main buffer (for vis_ring_buffer sync)
                wrote_to_main = False

                if self.put_downsample:
                    # put frequency regulation
                    local_idxs, global_idxs, put_idx \
                        = get_accumulate_timestamp_idxs(
                            timestamps=[t_cal],
                            start_time=put_start_time,
                            dt=1/self.put_fps,
                            # this is non in first iteration
                            # and then replaced with a concrete number
                            next_global_idx=put_idx,
                            # continue to pump frames even if not started.
                            # start_time is simply used to align timestamps.
                            allow_negative=True
                        )

                    for step_idx in global_idxs:
                        put_data['step_idx'] = step_idx
                        put_data['timestamp'] = t_cal
                        self.ring_buffer.put(put_data, wait=True)
                        wrote_to_main = True
                else:
                    step_idx = int((t_cal - put_start_time) * self.put_fps)
                    put_data['step_idx'] = step_idx
                    put_data['timestamp'] = t_cal
                    self.ring_buffer.put(put_data, wait=True)
                    wrote_to_main = True

                # signal ready and measure actual FPS
                if iter_idx == 0:
                    t_fps_measure_start = time.time()
                    self.ready_event.set()
                elif iter_idx == 30 and t_fps_measure_start is not None:
                    elapsed = time.time() - t_fps_measure_start
                    measured_fps = 30 / elapsed
                    if abs(measured_fps - fps) > fps * 0.15:  # >15% deviation
                        print(f"[UvcCamera] WARNING: configured FPS={fps}, measured FPS={measured_fps:.1f}", flush=True)
                    t_fps_measure_start = None  # Only measure once
                    
                # put to vis (skip if main buffer skipped due to rate regulation)
                if wrote_to_main:
                    vis_data = data
                    if self.vis_transform == self.transform:
                        vis_data = put_data
                    elif self.vis_transform is not None:
                        vis_data = self.vis_transform(dict(data))
                    self.vis_ring_buffer.put(vis_data, wait=False)

                # perf
                t_end = time.time()
                duration = t_end - t_start
                frequency = np.round(1 / duration, 1)
                t_start = t_end
                if self.verbose:
                    print(f'[UvcCamera {self.dev_video_path}] FPS {frequency}')


                # fetch command from queue
                try:
                    commands = self.command_queue.get_all()
                    n_cmd = len(commands['cmd'])
                except Empty:
                    n_cmd = 0
                except Exception as e:
                    # Catch all other exceptions to avoid killing VideoRecorder
                    # (Any unhandled exception would trigger finally block which stops VideoRecorder)
                    print(f"[UvcCamera] Command queue error: {type(e).__name__}: {e}", flush=True)
                    n_cmd = 0
                    time.sleep(0.1)  # Brief wait before retry

                # execute commands
                for i in range(n_cmd):
                    command = dict()
                    for key, value in commands.items():
                        command[key] = value[i]
                    cmd = command['cmd']
                    try:
                        if cmd == Command.RESTART_PUT.value:
                            put_idx = None
                            put_start_time = command['put_start_time']
                        elif cmd == Command.START_RECORDING.value:
                            video_path = str(command['video_path'])
                            start_time = command['recording_start_time']
                            if (start_time is None) or (start_time < 0):
                                start_time = last_t_cal if last_t_cal is not None else time.time()
                            self.video_recorder.start_recording(video_path, start_time=start_time)
                            rec_active = True
                        elif cmd == Command.STOP_RECORDING.value:
                            self.video_recorder.stop_recording()
                            rec_active = False
                    except Exception as e:
                        print(f"[UvcCamera] Command execution error (cmd={cmd}): {type(e).__name__}: {e}", flush=True)
                        import traceback
                        traceback.print_exc()

                iter_idx += 1
        finally:
            print("[UvcCamera] Process exiting", flush=True)
            self.video_recorder.stop()
            # When everything done, release the capture
            cap.release()
