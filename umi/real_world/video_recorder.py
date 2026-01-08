from typing import Optional, Callable, Generator
import numpy as np
import av
import time
import enum
import multiprocessing as mp
from multiprocessing.managers import SharedMemoryManager
from umi.shared_memory.shared_memory_queue import SharedMemoryQueue, Full, Empty
from umi.common.timestamp_accumulator import get_accumulate_timestamp_idxs


class VideoRecorder(mp.Process):
    MAX_PATH_LENGTH = 4096 # linux path has a limit of 4096 bytes
    class Command(enum.Enum):
        START_RECORDING = 0
        STOP_RECORDING = 1
    
    def __init__(self,
        fps,
        codec,
        input_pix_fmt,
        buffer_size=128,
        no_repeat=False,
        # options for codec
        **kwargs
    ):
        super().__init__()  # ✅ 只调用一次，进程复用
        self.fps = fps
        self.codec = codec
        self.input_pix_fmt = input_pix_fmt
        self.buffer_size = buffer_size
        self.no_repeat = no_repeat
        self.kwargs = kwargs

        # 这些在 setup() 时初始化
        self.img_queue = None
        self.cmd_queue = None  # Legacy, kept for compatibility
        self.mp_cmd_queue = None  # Standard mp.Queue for commands (safer than SharedMemoryQueue)
        self.stop_event = None
        self.ready_event = None
        self.recording_event = None
        self.idle_event = None
        self.stop_writing_event = None  # Signal to producers to stop writing before flush
        self.shape = None
        self.is_setup = False  # ✅ 新增标志
        self.is_started = False

        self._reset_state()
        
    # ======== custom constructors =======
    @classmethod
    def create_h264(cls,
            fps,
            codec='h264',
            input_pix_fmt='rgb24',
            output_pix_fmt='yuv420p',
            crf=18,
            profile='high',
            **kwargs
        ):
        obj = cls(
            fps=fps,
            codec=codec,
            input_pix_fmt=input_pix_fmt,
            pix_fmt=output_pix_fmt,
            options={
                'crf': str(crf),
                'profile': profile
            },
            **kwargs
        )
        return obj
    
    @classmethod
    def create_hevc_nvenc(cls,
            fps,
            codec='hevc_nvenc',
            input_pix_fmt='rgb24',
            output_pix_fmt='yuv420p',
            bit_rate=6000*1000,
            options={
                'tune': 'll', 
                'preset': 'p1'
            },
            **kwargs
        ):
        obj = cls(
            fps=fps,
            codec=codec,
            input_pix_fmt=input_pix_fmt,
            pix_fmt=output_pix_fmt,
            bit_rate=bit_rate,
            options=options,
            **kwargs
        )
        return obj

    # ========= context manager ===========
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    # ========= user API ===========
    def setup(self, shm_manager: SharedMemoryManager, data_example: np.ndarray):
        """初始化共享内存资源（只调用一次）"""
        if self.is_setup:
            return  # 防止重复调用

        # 创建进程间通信资源
        self.ready_event = mp.Event()
        self.stop_event = mp.Event()
        self.recording_event = mp.Event()
        self.idle_event = mp.Event()
        self.stop_writing_event = mp.Event()  # Signal to producers to stop writing before flush
        self.mp_cmd_queue = mp.Queue()  # Standard mp.Queue for commands

        # 创建共享内存队列（ZeroCopy）
        self.img_queue = SharedMemoryQueue.create_from_examples(
            shm_manager=shm_manager,
            examples={
                'img': data_example,
                'repeat': 1
            },
            buffer_size=self.buffer_size
        )
        # Note: cmd_queue (SharedMemoryQueue) removed - using mp_cmd_queue instead
        # to avoid SIGSEGV from atomics library in stopped state loop
        self.shape = data_example.shape
        self.is_setup = True

    def start_process(self):
        """启动子进程（只调用一次）"""
        if not self.is_setup:
            raise RuntimeError("Must call setup() before start_process()")
        if self.is_started:
            return  # 进程已启动，不要重复启动

        super().start()  # 启动 mp.Process
        self.is_started = True
        self.ready_event.wait()  # 等待子进程进入就绪状态

    def prepare_recording(self):
        """准备新的录制（可多次调用）"""
        if not self.is_setup or not self.is_started:
            raise RuntimeError("Must call setup() and start_process() first")

        # 等待进入 idle 状态
        if not self.idle_event.wait(timeout=5.0):
            raise RuntimeError("VideoRecorder did not enter idle state")

        # 清空命令队列（清理残留命令）
        while not self.mp_cmd_queue.empty():
            try:
                self.mp_cmd_queue.get_nowait()
            except Empty:
                break

        # Clear img_queue to reset counters - safe because we're in idle state
        self.img_queue.clear()

        # Clear stop_writing_event to allow writes for new recording
        self.stop_writing_event.clear()

        # 重置录制状态
        self._reset_state()
    
    def stop(self):
        self.stop_event.set()

    def start_wait(self):
        self.ready_event.wait()
    
    def end_wait(self):
        self.join()
        
    def is_ready(self):
        return self.is_started and self.ready_event.is_set() and (not self.stop_event.is_set())

    def is_recording(self):
        return self.recording_event is not None and self.recording_event.is_set()

    def is_idle(self):
        return self.idle_event is not None and self.idle_event.is_set()

    def is_accepting_frames(self):
        """Check if VideoRecorder is accepting new frames (not flushing)."""
        return (self.stop_writing_event is not None and
                not self.stop_writing_event.is_set() and
                self.is_ready())

    def wait_idle(self, timeout=5.0):
        """Block until VideoRecorder enters idle state (stopped state loop)."""
        if self.idle_event:
            return self.idle_event.wait(timeout=timeout)
        return False

    def start_recording(self, video_path: str, start_time: float=-1):
        path_len = len(video_path.encode('utf-8'))
        if path_len > self.MAX_PATH_LENGTH:
            raise RuntimeError('video_path too long.')
        self.start_time = start_time
        self.mp_cmd_queue.put({
            'cmd': self.Command.START_RECORDING.value,
            'video_path': video_path
        })

    def stop_recording(self):
        self.mp_cmd_queue.put({
            'cmd': self.Command.STOP_RECORDING.value
        })
        self._reset_state()
    
    def write_frame(self, img: np.ndarray, frame_time=None):
        if not self.is_ready():
            raise RuntimeError('Must run start() before writing!')
            
        n_repeats = 1
        if (not self.no_repeat) and (self.start_time is not None):
            local_idxs, global_idxs, self.next_global_idx \
                = get_accumulate_timestamp_idxs(
                # only one timestamp
                timestamps=[frame_time],
                start_time=self.start_time,
                dt=1/self.fps,
                next_global_idx=self.next_global_idx
            )
            # number of apperance means repeats
            n_repeats = len(local_idxs)
        
        self.img_queue.put({
            'img': img,
            'repeat': n_repeats
        })
    
    def get_img_buffer(self):
        """
        Get view to the next img queue memory
        for zero-copy writing
        """
        data = self.img_queue.get_next_view()
        img = data['img']
        return img
    
    def write_img_buffer(self, img: np.ndarray, frame_time=None):
        """
        Must be used with the buffer returned by get_img_buffer
        for zero-copy writing
        """
        if not self.is_ready():
            raise RuntimeError('Must run start() before writing!')
            
        n_repeats = 1
        if (not self.no_repeat) and (self.start_time is not None):
            local_idxs, global_idxs, self.next_global_idx \
                = get_accumulate_timestamp_idxs(
                # only one timestamp
                timestamps=[frame_time],
                start_time=self.start_time,
                dt=1/self.fps,
                next_global_idx=self.next_global_idx
            )
            # number of apperance means repeats
            n_repeats = len(local_idxs)
        
        self.img_queue.put_next_view({
            'img': img,
            'repeat': n_repeats
        })

    # ========= interval API ===========
    def _reset_state(self):
        self.start_time = None
        self.next_global_idx = 0
    
    def run(self):
        import signal
        import sys
        import os
        import faulthandler

        # Enable faulthandler for better crash diagnostics
        faulthandler.enable()

        def crash_handler(signum, frame):
            sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
            print(f"[VideoRecorder] CRASH! Signal: {sig_name}", flush=True)
            import traceback
            traceback.print_stack(frame)
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(1)  # Use os._exit to prevent cascading crashes during cleanup

        signal.signal(signal.SIGSEGV, crash_handler)
        signal.signal(signal.SIGABRT, crash_handler)

        print("[VideoRecorder] Process started", flush=True)
        try:
            self._run_impl()
        except Exception as e:
            print(f"[VideoRecorder] Fatal error: {type(e).__name__}: {e}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            print("[VideoRecorder] Process exiting", flush=True)

    def _run_impl(self):
        self.ready_event.set()
        if self.recording_event is None:
            self.recording_event = mp.Event()
        self.recording_event.clear()
        while not self.stop_event.is_set():
            video_path = None
            # ========= stopped state ============
            while (video_path is None) and (not self.stop_event.is_set()):
                try:
                    # Use standard mp.Queue with timeout (safer than SharedMemoryQueue)
                    cmd_data = self.mp_cmd_queue.get(timeout=0.1)
                    cmd = cmd_data['cmd']
                    if cmd == self.Command.START_RECORDING.value:
                        video_path = str(cmd_data['video_path'])
                    elif cmd == self.Command.STOP_RECORDING.value:
                        video_path = None
                    else:
                        raise RuntimeError("Unknown command: ", cmd)
                except Empty:
                    # Queue empty - now we are safely in idle state
                    if not self.idle_event.is_set():
                        self.idle_event.set()
                except Exception as e:
                    # Catch all other exceptions to avoid silent crash
                    print(f"[VideoRecorder] Stopped state error: {type(e).__name__}: {e}", flush=True)
                    import traceback
                    traceback.print_exc()
                    time.sleep(0.5)  # Brief wait before retry
            if self.stop_event.is_set():
                break
            assert video_path is not None
            # Clear idle state before entering recording
            self.idle_event.clear()
            # ========= recording state ==========
            try:
                with av.open(video_path, mode='w') as container:
                    stream = container.add_stream(self.codec, rate=self.fps)
                    h,w,c = self.shape
                    stream.width = w
                    stream.height = h
                    codec_context = stream.codec_context
                    for k, v in self.kwargs.items():
                        setattr(codec_context, k, v)

                    # File opened successfully, now set recording state
                    self.recording_event.set()
                    print(f"[VideoRecorder] START {video_path}", flush=True)

                    # loop
                    while not self.stop_event.is_set():
                        try:
                            # Use standard mp.Queue with non-blocking get
                            cmd_data = self.mp_cmd_queue.get_nowait()
                            cmd = int(cmd_data['cmd'])
                            if cmd == self.Command.STOP_RECORDING.value:
                                print("[VideoRecorder] STOP", flush=True)
                                break
                            elif cmd == self.Command.START_RECORDING.value:
                                continue
                            else:
                                raise RuntimeError("Unknown command: ", cmd)
                        except Empty:
                            pass
                        
                        try:
                            with self.img_queue.get_view() as data:
                                img = data['img']
                                repeat = data['repeat']
                                frame = av.VideoFrame.from_ndarray(
                                    img, format=self.input_pix_fmt)
                            for _ in range(repeat):
                                for packet in stream.encode(frame):
                                    container.mux(packet)
                        except Empty:
                            time.sleep(0.1/self.fps)

                    # Signal producers to stop writing before flush
                    self.stop_writing_event.set()

                    # Brief wait for in-flight writes to complete
                    time.sleep(0.05)

                    # Flush queue
                    try:
                        while not self.img_queue.empty():
                            with self.img_queue.get_view() as data:
                                img = data['img']
                                repeat = data['repeat']
                                frame = av.VideoFrame.from_ndarray(
                                    img, format=self.input_pix_fmt)
                            for _ in range(repeat):
                                for packet in stream.encode(frame):
                                    container.mux(packet)
                    except Empty:
                        pass

                    # Flush stream
                    for packet in stream.encode():
                        container.mux(packet)
            except Exception as e:
                print(f"[VideoRecorder] ERROR: {type(e).__name__}: {e}", flush=True)
                import traceback
                traceback.print_exc()
            finally:
                self.recording_event.clear()
