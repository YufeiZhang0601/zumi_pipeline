import cv2
import time

def main():
    # 1. 打开相机 (通常是 0 或 1)
    # 使用 cv2.CAP_V4L2 指定 Linux 的 V4L2 驱动后端
    cap = cv2.VideoCapture(0, cv2.CAP_V4L2)

    if not cap.isOpened():
        print("无法打开相机")
        return

    # 设置 MJPG 格式以支持 60 FPS
    fourcc = cv2.VideoWriter_fourcc(*'MJPG')
    cap.set(cv2.CAP_PROP_FOURCC, fourcc)

    # 设置分辨率
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    # 设置帧率
    cap.set(cv2.CAP_PROP_FPS, 60)

    # 验证设置是否生效
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"实际分辨率: {actual_w}x{actual_h}, 实际FPS: {actual_fps}")
    if actual_fps != 60:
        print(f"警告: 请求 FPS=60, 实际={actual_fps}")

    # 2. 关键步骤：关闭自动曝光
    # 在 V4L2 中，通常 1 表示手动模式，3 表示自动模式
    # 注意：不同的相机厂家对这个值的定义可能略有不同
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1) 

    # 设置默认曝光值为 10
    cap.set(cv2.CAP_PROP_EXPOSURE, 10)

    # 3. 创建窗口和滑动条
    window_name = "UVC Camera Control"
    cv2.namedWindow(window_name)

    # 获取当前曝光值作为初始值
    current_exposure = cap.get(cv2.CAP_PROP_EXPOSURE)
    print(f"当前曝光值: {current_exposure}")

    # 创建滑动条 (参数：名称, 窗口名, 默认值, 最大值, 回调函数)
    # 注意：曝光值的范围取决于硬件，常见如 1-5000 或 1-1000
    def update_exposure(val):
        cap.set(cv2.CAP_PROP_EXPOSURE, float(val))
        # 读取一下确认是否设置成功
        actual_val = cap.get(cv2.CAP_PROP_EXPOSURE)
        print(f"设置曝光为: {val}, 实际生效: {actual_val}")

    cv2.createTrackbar("Exposure", window_name, int(current_exposure), 1000, update_exposure)

    print("按下 'q' 退出程序")

    while True:
        ret, frame = cap.read()
        if not ret:
            print("无法接收画面")
            break

        # 显示画面
        cv2.imshow(window_name, frame)

        # 按 'q' 退出
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    # 释放资源
    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()