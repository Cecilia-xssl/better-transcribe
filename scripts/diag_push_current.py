#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推动电流/信号自测 —— 检查“用手推笔架”能否产生任何可检测的电信号。

三条监测通道（20Hz 高频采样 30 秒）：
  1. GRBL 状态报告字段变化（MPos / Pn: 限位、探针、门、保持 等引脚位）
  2. 串口调制解调器线状态（CTS / DSR / DCD / RI）—— 若控制板接了相关引脚
  3. RX 线噪声字节 —— 推动电机产生的反电动势干扰串口时会出现乱码字节

用法：
  python diag_push_current.py                  # 电机松开状态（$1=25，可自由推动）
  python diag_push_current.py --hold           # 电机锁死状态（$1=255，推着费力）
  python diag_push_current.py --port COM5 --seconds 40

流程：运行后先 3 秒采集基线（不要碰），然后提示开始推动，用力推 30 秒，
最后打印三通道差异报告。Ctrl+C 可提前结束。
"""

import argparse
import re
import sys
import threading
import time

import serial


def main() -> int:
    ap = argparse.ArgumentParser(description="推动电流/信号自测")
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=int, default=30, help="推动测试时长（秒）")
    ap.add_argument("--hold", action="store_true",
                    help="测试电机锁死状态（$1=255）；默认松开（$1=25）")
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.05)
    time.sleep(1.5)
    ser.reset_input_buffer()

    # 电机状态
    idle_delay = "255" if args.hold else "25"
    ser.write(f"$1={idle_delay}\n".encode())
    time.sleep(0.3)
    print(f"电机状态：{'锁死($1=255)' if args.hold else '松开($1=25)'}")

    # ---- 后台读线程：收集一切原始字节 ----
    raw_chunks: list[bytes] = []
    stop_flag = threading.Event()

    def reader():
        while not stop_flag.is_set():
            data = ser.read(256)
            if data:
                raw_chunks.append(data)

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    def poll_status() -> str:
        """发 '?' 并在原始流里找状态行；同时记录调制解调器线。"""
        ser.write(b"?")
        time.sleep(0.045)
        return ""

    def modem() -> tuple:
        return (ser.cts, ser.dsr, ser.cd, ser.ri)

    # ---- 采集 ----
    def capture(seconds: float, label: str):
        print(f"\n== {label}（{seconds:.0f} 秒）==")
        t0 = time.time()
        while time.time() - t0 < seconds:
            poll_status()
            time.sleep(0.05)  # 20Hz
        return {
            "t": seconds,
            "raw_len": sum(len(c) for c in raw_chunks),
            "raw": b"".join(raw_chunks),
            "modem": modem(),
        }

    raw_chunks.clear()
    base = capture(3.0, "基线采集：请勿触碰笔架")
    base_raw = base["raw"]
    base_modem = base["modem"]
    base_status = re.findall(rb"<[^>]*>", base_raw)

    print(f">>> 现在开始用力推动笔架（上推/推到角落/来回推）持续 {args.seconds} 秒…")
    raw_chunks.clear()
    mid_modem_samples: list[tuple] = []
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        poll_status()
        mid_modem_samples.append(modem())
        time.sleep(0.05)
    push_raw = b"".join(raw_chunks)

    stop_flag.set()
    th.join(timeout=1)
    ser.close()

    # ---- 分析 ----
    push_status = re.findall(rb"<[^>]*>", push_raw)

    def clean_bytes(raw: bytes) -> bytes:
        """去掉 ok/状态行/空白后剩下的“噪声字节”。"""
        s = raw
        s = re.sub(rb"<[^>]*>", b"", s)
        s = re.sub(rb"ok\r?\n", b"", s)
        s = re.sub(rb"error:\d+\r?\n", b"", s)
        s = re.sub(rb"\s+", b"", s)
        return s

    base_noise = clean_bytes(base_raw)
    push_noise = clean_bytes(push_raw)

    # 状态字段差异
    def fields(lines: list[bytes]) -> set:
        out = set()
        for ln in lines:
            out.add(ln.decode("ascii", errors="replace"))
        return out

    base_f = fields(base_status)
    push_f = fields(push_status)
    new_f = push_f - base_f

    modem_changed = any(m != base_modem for m in mid_modem_samples)

    print("\n" + "=" * 52)
    print("自测报告")
    print("=" * 52)
    print(f"[1] 状态报告：基线 {len(base_status)} 条 / 推动期 {len(push_status)} 条")
    if new_f:
        print(f"    推动期出现新状态：")
        for f in sorted(new_f)[:10]:
            print("      ", f)
    else:
        print("    无任何状态字段变化（MPos/Pn 恒定）")
    print(f"[2] 调制解调器线 CTS/DSR/DCD/RI：{'有变化！' if modem_changed else '无变化'}"
          f"（基线 {base_modem}）")
    print(f"[3] RX 噪声字节：基线 {len(base_noise)} B → 推动期 {len(push_noise)} B")
    if push_noise:
        print("    推动期噪声内容：", push_noise[:80])

    verdict = []
    if new_f:
        verdict.append("状态字段有变化 → 可用状态检测")
    if modem_changed:
        verdict.append("调制解调器线有变化 → 可用线路检测")
    if len(push_noise) > len(base_noise):
        verdict.append("推动产生串口噪声（反电动势）→ 可用噪声检测")
    print("\n结论：", "；".join(verdict) if verdict else
          "三通道均无信号 —— 当前硬件无法感知推动（需加装限位开关/编码器）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
