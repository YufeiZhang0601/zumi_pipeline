import cv2
import numpy as np

def main():
    # 1. 打开相机
    cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
    if not cap.isOpened():
        print("无法打开相机")
        return

    # 设置分辨率
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    # 2. 关闭自动曝光
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
    cap.set(cv2.CAP_PROP_EXPOSURE, 10)

    # 3. 创建窗口
    cv2.namedWindow("Raw")
    cv2.namedWindow("Diff")

    # 曝光滑动条
    current_exposure = cap.get(cv2.CAP_PROP_EXPOSURE)
    print(f"当前曝光值: {current_exposure}")

    def update_exposure(val):
        cap.set(cv2.CAP_PROP_EXPOSURE, float(val))
        actual_val = cap.get(cv2.CAP_PROP_EXPOSURE)
        print(f"设置曝光为: {val}, 实际生效: {actual_val}")

    cv2.createTrackbar("Exposure", "Raw", int(current_exposure), 1000, update_exposure)

    # Diff增益滑动条（放大差异便于观察）
    cv2.createTrackbar("Gain", "Diff", 1, 10, lambda x: None)

    # Reference frame
    ref_frame = None

    print("=" * 50)
    print("操作说明:")
    print("  空格键: 更新reference（夹爪张开时按）")
    print("  r键: 重置reference")
    print("  s键: 保存当前diff图像")
    print("  q键: 退出")
    print("=" * 50)

    frame_count = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            print("无法接收画面")
            break

        key = cv2.waitKey(1) & 0xFF

        # 空格更新reference
        if key == ord(' '):
            ref_frame = frame.astype(np.float32)
            print(">>> Reference已更新!")

        # r键重置
        if key == ord('r'):
            ref_frame = None
            print(">>> Reference已重置!")

        # s键保存
        if key == ord('s') and ref_frame is not None:
            filename = f"diff_{frame_count}.png"
            diff = frame.astype(np.float32) - ref_frame
            diff_vis = np.clip(diff + 128, 0, 255).astype(np.uint8)
            cv2.imwrite(filename, diff_vis)
            cv2.imwrite(f"raw_{frame_count}.png", frame)
            print(f">>> 已保存: {filename}")
            frame_count += 1

        # 显示原始图像
        cv2.imshow("Raw", frame)

        # 显示Diff图像
        if ref_frame is not None:
            diff = frame.astype(np.float32) - ref_frame
            gain = cv2.getTrackbarPos("Gain", "Diff")
            gain = max(1, gain)
            diff_vis = np.clip(diff * gain + 128, 0, 255).astype(np.uint8)
            cv2.imshow("Diff", diff_vis)
        else:
            # 没有reference时显示灰色提示
            gray_img = np.ones_like(frame) * 128
            cv2.putText(gray_img, "Press SPACE to set reference", 
                       (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            cv2.imshow("Diff", gray_img)

        if key == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()