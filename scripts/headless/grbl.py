"""GRBL 串口发送器 —— 流式发送 G-code，支持急停。

安全设计：
- 默认 dry-run（只统计不发）；必须显式 --send 才动机器。
- Ctrl+C / 停止文件触发 急停：立即发 feed-hold '!'，再确认后可发 0x18 软复位。
- 行级 ok/ack 握手（GRBL 1.1），每行 < 80 字符，安全。
- 发送前打印坐标范围；范围超限需 --allow-out-of-bounds。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import serial
except ImportError:
    print("需要 pyserial：pip install pyserial", file=sys.stderr)
    raise

STOP_FILE = Path.home() / ".KenjoyDraw" / "mimo-outbox" / "GRBL_STOP"
PAUSE_FILE = Path.home() / ".KenjoyDraw" / "mimo-outbox" / "GRBL_PAUSE"
CONTINUE_FILE = Path.home() / ".KenjoyDraw" / "mimo-outbox" / "GRBL_CONTINUE"
STOP_STATE_FILE = Path.home() / ".KenjoyDraw" / "mimo-outbox" / "GRBL_STOP_STATE.json"

# 当前活动连接（供关窗口钩子直接旁路释放电机）
_ACTIVE: "GRBLSender | None" = None


def release_motors() -> bool:
    """释放电机：升顶后 $1=25（空闲断电）+ $SLP（强制睡眠断电）。

    释放前先把 Z 升到最高（Z-2）：断电后笔架弹簧会把 Z 顶高 ~5mm（实测），
    开环位置随之失真 → 下次任务首轮落笔离纸 5mm。升到顶部若被机械限位挡住，
    漂移即被限位吃掉，位置基准保持。
    """
    global _ACTIVE
    up = b"G21\nG90\nG1 F3000 Z-2\n"
    tail = b"$1=25\n$SLP\n"
    try:
        if _ACTIVE is not None and _ACTIVE.ser is not None and _ACTIVE.ser.is_open:
            _ACTIVE.ser.write(up)
            time.sleep(1.2)
            _ACTIVE.ser.write(tail)
            time.sleep(0.2)
            return True
    except Exception:
        pass
    try:
        import serial
        s = serial.Serial("COM5", 115200, timeout=0.2)
        s.write(up)
        time.sleep(1.2)
        s.write(tail)
        time.sleep(0.3)
        s.close()
        return True
    except Exception:
        return False

# 轨道安全边界（机器坐标；需覆盖右上角原点后的负坐标区，仅拦截离谱狂奔值）
SAFE_BOUNDS = {"x": (-400.0, 400.0), "y": (-400.0, 400.0), "z": (-2.0, 15.0)}


def _check_line_bounds(line: str) -> str | None:
    """校验单行坐标；返回违规描述或 None。"""
    import re
    vals = {}
    for axis in "XYZ":
        m = re.search(rf"{axis}([-+0-9.]+)", line)
        if m:
            vals[axis] = float(m.group(1))
    for axis, v in vals.items():
        lo, hi = SAFE_BOUNDS[axis.lower()]
        if not (lo <= v <= hi):
            return f"{axis}={v} 超出安全范围 [{lo},{hi}]"
    return None


@dataclass
class Bounds:
    # 与 SAFE_BOUNDS 一致（仅拦截离谱狂奔值；工作区大小由机器物理行程决定）
    min_x: float = -400.0
    max_x: float = 400.0
    min_y: float = -400.0
    max_y: float = 400.0


def parse_bounds(gcode_lines: list[str]) -> tuple[float, float, float, float]:
    import re

    xs, ys = [], []
    for ln in gcode_lines:
        m = re.search(r"X([-+0-9.]+)", ln)
        if m:
            xs.append(float(m.group(1)))
        m = re.search(r"Y([-+0-9.]+)", ln)
        if m:
            ys.append(float(m.group(1)))
    return (min(xs), max(xs), min(ys), max(ys)) if xs else (0, 0, 0, 0)


class GRBLSender:
    def __init__(self, port: str, baud: int = 115200):
        self.port = port
        self.baud = baud
        self.ser: serial.Serial | None = None

    def open(self) -> None:
        global _ACTIVE
        self.port = resolve_port(self.port)
        self.ser = serial.Serial(self.port, self.baud, timeout=2)
        _ACTIVE = self
        time.sleep(2.0)  # GRBL 复位等待
        self.ser.reset_input_buffer()
        # 唤醒检测：$SLP 睡眠后换行叫不醒，须软复位 0x18（位置保留，G92 清零无关紧要）
        self.ser.write(b"?")
        time.sleep(0.3)
        probe = self._read_all()
        if "Sleep" in probe:
            print(">> 控制器处于休眠，软复位唤醒…")
            self.ser.write(b"\x18")
            time.sleep(1.5)
            self._read_all()
        self.ser.write(b"\r\n\r\n")
        time.sleep(0.5)
        banner = self._read_all()
        if "Grbl" not in banner and "grbl" not in banner:
            print(f"警告：未见 Grbl 横幅，收到：{banner[:100]!r}", file=sys.stderr)
        # 查询坐标偏移（串口打开可能触发控制器复位，G92 偏移会丢，G10/G54 保留）
        try:
            self.ser.write(b"$#\n")
            time.sleep(0.3)
            offsets = self._read_all()
            print(f">> 坐标偏移查询 $#：\n{offsets}")
        except Exception:
            pass

    def close(self) -> None:
        global _ACTIVE
        if self.ser and self.ser.is_open:
            self.ser.close()
        if _ACTIVE is self:
            _ACTIVE = None

    def _read_all(self) -> str:
        out = []
        assert self.ser
        t0 = time.time()
        while time.time() - t0 < 1.0:
            data = self.ser.readline().decode("utf-8", errors="replace").strip()
            if data:
                out.append(data)
            elif out:
                break
        return "\n".join(out)

    def send_line(self, line: str) -> str:
        """发一行并等待 ok/err；超时清缓冲重试一次，仍无响应返回 'timeout'（不挂死）。"""
        assert self.ser
        for attempt in range(2):
            self.ser.write((line.strip() + "\n").encode("ascii", errors="ignore"))
            t0 = time.time()
            limit = 15.0 if attempt == 0 else 5.0
            while time.time() - t0 < limit:
                resp = self.ser.readline().decode("utf-8", errors="replace").strip()
                if resp.startswith("ok") or resp.startswith("error") or resp.startswith("ALARM"):
                    return resp
            self.ser.reset_input_buffer()
        return "timeout"

    def estop(self) -> None:
        """急停：feed hold 后软复位（软复位同时清 hold 状态并松开电机，防卡死锁轴）。"""
        assert self.ser
        self.ser.write(b"!")   # feed hold：立即停
        time.sleep(0.3)
        self.ser.write(b"\x18")  # 软复位：解锁 + 清 hold
        time.sleep(1.0)
        try:
            self.ser.write(b"G21\nG90\nG1 F6000 Z-2\n")  # 升到最高（软复位后坐标重置，保护+顶部绑定）
        except Exception:
            pass
        try:
            self.ser.write(b"$1=25\n")  # 恢复空闲释放，防复位后电机锁死
            time.sleep(0.1)
        except Exception:
            pass
        print(">> 急停：已 feed-hold + 软复位（电机已松开，$X 可解锁）")

    def soft_reset(self) -> None:
        assert self.ser
        self.ser.write(b"\x18")
        time.sleep(1.0)

    def unlock(self) -> None:
        self.send_line("$X")

    def send_raw(self, line: str) -> None:
        """发送不等回执（流水线模式用）。"""
        assert self.ser
        self.ser.write((line.strip() + "\n").encode("ascii", errors="ignore"))

    def read_response(self, timeout: float = 15.0) -> str:
        """等待一条 ok/error/ALARM 回执；超时返回 'timeout'。"""
        assert self.ser
        t0 = time.time()
        while time.time() - t0 < timeout:
            resp = self.ser.readline().decode("utf-8", errors="replace").strip()
            if resp.startswith("ok") or resp.startswith("error") or resp.startswith("ALARM"):
                return resp
        return "timeout"

    def status(self) -> str:
        """实时状态查询 '?'，非阻塞快读（约 50ms 返回，不打断书写节奏）。"""
        assert self.ser
        self.ser.write(b"?")
        deadline = time.time() + 0.4
        buf = b""
        while time.time() < deadline:
            n = self.ser.in_waiting
            if n:
                buf += self.ser.read(n)
                if b"<" in buf and b">" in buf:
                    break
            else:
                time.sleep(0.01)
        return buf.decode("utf-8", errors="replace").strip()


def request_stop() -> None:
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.write_text("stop", encoding="utf-8")


def request_pause() -> None:
    PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE_FILE.write_text("pause", encoding="utf-8")


def request_continue() -> None:
    CONTINUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTINUE_FILE.write_text("go", encoding="utf-8")


def continue_requested() -> bool:
    return CONTINUE_FILE.exists()


def clear_continue() -> None:
    if CONTINUE_FILE.exists():
        CONTINUE_FILE.unlink()


def pause_requested() -> bool:
    return PAUSE_FILE.exists()


def clear_pause() -> None:
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()


def save_stop_state(reason: str, mpos, lines_done: int) -> None:
    """中断状态落盘（断点续写依据）。"""
    import json
    try:
        STOP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_STATE_FILE.write_text(json.dumps({
            "reason": reason, "mpos": list(mpos) if mpos else None,
            "lines_done": lines_done, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def page_done_marks(gcode_lines: list[str]) -> list[int]:
    """按 @swap-page 分页的已发指令边界：[0, 页1尾, 页2尾, ..., 总数]。

    与 stream() 的计数口径一致（空行/圆括号/分号注释不计）。
    """
    marks = [0]
    c = 0
    for ln in gcode_lines:
        s = ln.strip()
        if not s or s.startswith("("):
            continue
        if s.startswith("; @swap-page"):
            marks.append(c)
        if s.startswith(";"):
            continue
        c += 1
    marks.append(c)
    return marks


def wait_signal(timeout: float = 3600.0) -> str:
    """等待继续/停止：回车=继续、s=停止；或 CONTINUE/STOP 信号文件（网页按钮）。

    返回 'continue' | 'stop' | 'timeout'。
    """
    import msvcrt
    print("   （回车或『继续』信号=继续；s 或『停止』信号=停止）")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if stop_requested():
            return "stop"
        if continue_requested():
            clear_continue()
            return "continue"
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    return "continue"
                if ch in ("s", "S"):
                    return "stop"
        except Exception:
            pass
        time.sleep(0.3)
    return "timeout"


def stop_requested() -> bool:
    return STOP_FILE.exists()


def clear_stop() -> None:
    if STOP_FILE.exists():
        STOP_FILE.unlink()


def resolve_port(preferred: str) -> str:
    """解析串口：优先指定口；不存在则自动识别 CH340（防重插变号）；都没有则友好报错。"""
    import serial.tools.list_ports as lp
    ports = list(lp.comports())
    devs = {p.device for p in ports}
    if preferred in devs:
        return preferred
    for p in ports:
        blob = (p.description + " " + p.hwid).upper()
        if "CH34" in blob or "1A86" in blob or "USB-SERIAL" in blob or "USB UART" in blob:
            print(f">> {preferred} 不存在，自动切换到检测到的写字机串口 {p.device}（{p.description}）")
            return p.device
    avail = ", ".join(sorted(devs)) or "无"
    raise RuntimeError(
        f"找不到写字机串口 {preferred}（当前可用：{avail}）。"
        f"请插好写字机 USB（CH340）后重试，或用 --port 指定。")


def _mpos(status: str) -> tuple[float, float] | None:
    import re
    m = re.search(r"MPos:([-0-9.]+),([-0-9.]+)", status)
    return (float(m.group(1)), float(m.group(2))) if m else None


def wait_for_push(snd: "GRBLSender", timeout: float = 900.0) -> str | None:
    """换纸等待：把笔架推回左上角 → 自动复位回停靠点 → 继续。

    触发通道：限位开关被压（Pn 的 X/Y 位消失——$5=1 反相接线，空闲时 Pn:XY 常驻，
    开关压下时对应位清除）/ 位移突变(>10mm，编码器机型) / 回车(后备)。
    返回触发原因，超时返回 None。
    """
    import msvcrt
    import re

    def pn_bits(status: str) -> str:
        m = re.search(r"Pn:([^|]*)", status)
        return m.group(1) if m else ""

    print("   换纸完成按回车继续（不要移动笔架）；若推到左上角触发开关则自动复位。")
    base = None
    baseline = None
    t0 = time.time()
    trigger = None
    while time.time() - t0 < timeout:
        if stop_requested():
            return "停止文件"
        st = snd.status()
        bits = pn_bits(st)
        if baseline is None:
            baseline = bits
            print(f"   （开关基线 Pn:{bits or '空'}）")
        else:
            lost = [b for b in "XY" if b in baseline and b not in bits]
            if lost:
                trigger = f"限位开关 {'/'.join(lost)} 被压"
                break
        pos = _mpos(st)
        if pos is not None:
            if base is None:
                base = pos
            elif abs(pos[0] - base[0]) + abs(pos[1] - base[1]) > 10.0:
                trigger = "位移变化"
                break
        if msvcrt.kbhit() and msvcrt.getwch() in ("\r", "\n"):
            trigger = "手动回车"
            break
        if continue_requested():
            clear_continue()
            trigger = "手动回车"
            break
        time.sleep(0.5)
    if trigger is None:
        return None
    if trigger == "手动回车":
        # 换好纸直接继续：笔架未动，坐标仍有效，不重定原点
        print("   >> 手动继续（坐标保持）")
        return trigger
    # 推回左上角（开关/位移触发）：G92 重新定原点 → 复位回停靠点
    print(f"   >> 检测到[{trigger}]，复位：G92 定原点 → 回停靠点")
    snd.send_line("$X")          # 若触碰限位导致报警则解锁
    snd.send_line("G92 X0 Y0")   # 当前位置（左上角）设为坐标原点
    snd.send_line("G1 F3500 X180.00 Y-3.00")  # 复位回停靠点（纸右上角原点）
    return trigger


def stream(port: str, gcode_lines: list[str], dry_run: bool = True,
           bounds: Bounds | None = None, allow_oob: bool = False,
           baud: int = 115200, park: tuple[float, float] = (180.0, -3.0),
           prelude: list[str] | None = None, z_off: float = 0.0) -> int:
    """prelude：解锁后、任何移动前执行的指令（如 ['G21','G90','G92 X0 Y0'] 定原点）。

    顺序关键：定原点必须在一切移动之前，否则沿用失效坐标会越界狂奔。
    """
    xmin, xmax, ymin, ymax = parse_bounds(gcode_lines)
    print(f"G-code 行数 {len(gcode_lines)}，范围 X[{xmin:.1f},{xmax:.1f}] Y[{ymin:.1f},{ymax:.1f}]")
    b = bounds or Bounds()
    if (xmin < b.min_x or xmax > b.max_x or ymin < b.min_y or ymax > b.max_y):
        msg = f"超出安全范围 X[{b.min_x},{b.max_x}] Y[{b.min_y},{b.max_y}]"
        if not allow_oob:
            print(f"错误：{msg}。加 --allow-out-of-bounds 强制执行。", file=sys.stderr)
            return 1
        print(f"警告：{msg}（已强制）")

    if dry_run:
        print("dry-run：未发送。加 --send 实际执行。")
        return 0

    clear_stop()
    snd = GRBLSender(port, baud)
    try:
        try:
            snd.open()
        except (RuntimeError, serial.SerialException) as e:
            print(f"错误：{e}", file=sys.stderr)
            return 4
        snd.unlock()
        if prelude:
            print(">> 前置指令：", prelude)
            for pl in prelude:
                if pl.strip() and not pl.startswith(";"):
                    resp = snd.send_line(pl)
                    if resp.startswith("error") or resp.startswith("ALARM"):
                        snd.estop()
                        print(f"前置指令失败：{resp}（{pl}）", file=sys.stderr)
                        return 6
        # 书写期间电机持续保持（$1=255）：防笔画间断电再啮合的冲击抖动与噪音
        print(">> 书写模式：电机持续保持（$1=255，换纸时才松开）")
        snd.send_line("$1=255")
        # Z 顶部绑定：升到最高点。若有机械限位，顶住即完成 Z 基准自校（治
        # "松电后弹簧漂移 ~5mm → 首轮落笔离纸"）；无硬限位则仅一次安全抬高。
        snd.send_line("G1 F3000 Z-2")
        # 左移卡顿震动：降 X 加速度（$120 原 3000）；如需恢复发 $120=3000
        snd.send_line("$120=1500")
        # 首冲落笔行程不足与 Z 加速度无关（降速实测无效）——恢复原值 8000
        snd.send_line("$122=8000")
        # （已取消：启动时先移动到停靠点——无意义路径）
        # 运动中碰撞基线（限位开关被压 = 撞到轨道边界/外力，立即急停）
        import re as _re
        _st0 = snd.status()
        _m0 = _re.search(r"Pn:([^|]*)", _st0)
        pn_base = _m0.group(1) if _m0 else ""
        pm = page_done_marks(gcode_lines)  # 页边界（@swap-page 分隔）：进度按页内口径
        total = pm[-1]
        done = 0
        inflight = 0
        MAX_INFLIGHT = 10  # 在途指令窗口：防缓冲断流（走走停停/锁死感）

        def _ack():
            nonlocal inflight
            resp = snd.read_response(15.0)
            if resp == "timeout":
                raise TimeoutError(f"控制器无响应（在途 {inflight} 条）")
            if resp.startswith("error") or resp.startswith("ALARM"):
                raise RuntimeError(resp)
            inflight -= 1

        def _drain():
            while inflight > 0:
                _ack()

        try:
            for ln in gcode_lines:
                ln = ln.strip()
                if not ln or ln.startswith("("):
                    continue
                if ln.startswith("; @swap-page") or ln.startswith("; @pause"):
                    _drain()  # 同步交互前收完在途回执
                if ln.startswith("; @swap-page"):
                    hint = ln[len("; @swap-page"):].strip()
                    print(f"\n== 本页完成（停靠右下角）{' —— ' + hint if hint else ''} ==")
                    snd.send_line("$1=25")  # 换纸时松开电机
                    print("   电机已松开，换纸后按回车继续（不要移动笔架）；若推到左上角触发开关则自动复位。")
                    got = wait_for_push(snd)
                    if got is None:
                        snd.estop()
                        print("等待超时，已急停。", file=sys.stderr)
                        return 2
                    if got == "停止文件":
                        snd.estop()
                        return 2
                    snd.send_line("$1=255")  # 恢复书写模式
                    continue
                if ln.startswith("; @pause"):
                    msg = ln[len("; @pause"):].strip() or "换纸后按回车继续"
                    snd.send_line("$1=25")
                    print(f"\n== 暂停：{msg} ==（电机已松开）")
                    sig = wait_signal()
                    if sig in ("stop", "timeout"):
                        try:
                            save_stop_state("stopped", _mpos(snd.status()), done)
                        except Exception:
                            pass
                        snd.estop()
                        print("已急停。", file=sys.stderr)
                        return 2
                    snd.send_line("$1=255")
                    continue
                if ln.startswith(";"):
                    continue
                # 轨道边界硬防护：超界指令一律拒绝
                bad = _check_line_bounds(ln)
                if bad:
                    snd.estop()
                    print(f"安全拦截：{bad}（指令: {ln}）", file=sys.stderr)
                    return 4
                if stop_requested():
                    try:
                        save_stop_state("stopped", _mpos(snd.status()), done)
                    except Exception:
                        pass
                    snd.estop()
                    print("收到停止文件，已急停（断点已保存）。")
                    return 2
                if done % 20 == 0:
                    try:
                        import msvcrt
                        if msvcrt.kbhit() and msvcrt.getwch() in ("p", "P"):
                            request_pause()
                    except Exception:
                        pass
                if pause_requested():
                    clear_pause()
                    _drain()
                    try:
                        mp = _mpos(snd.status())
                    except Exception:
                        mp = None
                    save_stop_state("paused", mp, done)
                    try:
                        snd.send_line(f"G1 F6000 Z{z_off:.2f}")
                        snd.send_line(f"G1 F3500 X{park[0]:.2f} Y{park[1]:.2f}")
                        release_motors()
                    except Exception:
                        pass
                    print(f"\n>> 已暂停：机器位置 {mp}，已发 {done} 行。断点已保存，用 --resume 继续。")
                    return 7
                # 流水线发送：不等回执，窗口满时才消费
                snd.send_raw(ln)
                inflight += 1
                done += 1
                while inflight >= MAX_INFLIGHT:
                    _ack()
                if done % 500 == 0:
                    seg = 0
                    for _i in range(len(pm) - 1):
                        if done >= pm[_i + 1]:
                            seg = _i + 1
                    seg = min(seg, len(pm) - 2)
                    seg_s, seg_e = pm[seg], pm[seg + 1]
                    ppct_m = (done - seg_s) * 100000 // max(seg_e - seg_s, 1)  # 下取整 0.001%
                    print(f"进度 第{seg + 1}/{len(pm) - 1}页 {done - seg_s}/{seg_e - seg_s}"
                          f"（{ppct_m / 1000:.3f}%）", flush=True)
                    try:
                        st = snd.status()
                        if "Alarm" in st:
                            snd.estop()
                            print(f"安全拦截：控制器报警 {st[:80]}", file=sys.stderr)
                            return 5
                        m2 = _re.search(r"Pn:([^|]*)", st)
                        bits = m2.group(1) if m2 else ""
                        lost = [b for b in "XY" if b in pn_base and b not in bits]
                        if lost:
                            snd.estop()
                            print(f"安全拦截：运动中限位 {'/'.join(lost)} 被压（撞轨/外力阻力）", file=sys.stderr)
                            return 5
                    except Exception:
                        pass
            _drain()
        except (TimeoutError, RuntimeError) as e:
            snd.estop()
            print(f"发送异常：{e}（已急停）", file=sys.stderr)
            return 3
        print(f"完成：{done} 行")
        print("等待控制器缓冲区排空（收尾抬笔/回位）...")
        # 等待真正停稳再释放电机（否则停靠移动可能被断电掐断在半路）
        t0 = time.time()
        while time.time() - t0 < 15:
            try:
                if "Idle" in snd.status():
                    break
            except Exception:
                pass
            time.sleep(0.5)
        time.sleep(1.0)
        # 位置偏差报告：实际位置 vs 期望停靠点（量化漂移）
        try:
            import re
            st = snd.status()
            m = re.search(r"WPos:([-0-9.]+),([-0-9.]+),([-0-9.]+)", st)
            if m:
                wx, wy = float(m.group(1)), float(m.group(2))
            else:
                mp = re.search(r"MPos:([-0-9.]+),([-0-9.]+),([-0-9.]+)", st)
                wc = re.search(r"WCO:([-0-9.]+),([-0-9.]+),([-0-9.]+)", st)
                if mp:
                    wx = float(mp.group(1)) - (float(wc.group(1)) if wc else 0.0)
                    wy = float(mp.group(2)) - (float(wc.group(2)) if wc else 0.0)
                else:
                    wx = wy = None
            if wx is not None:
                print(f">> 位置报告：实际=({wx:.2f},{wy:.2f}) 期望停靠点={park}"
                      f" 偏差=({wx - park[0]:+.2f},{wy - park[1]:+.2f}) mm")
            else:
                print(f">> 状态：{st[:120]}")
        except Exception as e:
            print(f">> 状态查询失败：{e}")
        return 0
    except KeyboardInterrupt:
        print("\nCtrl+C！")
        try:
            save_stop_state("interrupted", _mpos(snd.status()), done)
        except Exception:
            pass
        try:
            snd.estop()
        except Exception:
            pass
        return 130
    finally:
        # 正常完成也释放电机（用户口径）；release 内含升顶——开关机后 Z 漂移由顶部绑定治
        if release_motors():
            print(">> 电机已释放（$1=25）")
        snd.close()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="GRBL 发送器")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("send", help="发送 G-code 文件")
    sp.add_argument("file")
    sp.add_argument("--port", default="COM5")
    sp.add_argument("--baud", type=int, default=115200)
    sp.add_argument("--send", action="store_true", help="真正发送（默认 dry-run）")
    sp.add_argument("--allow-out-of-bounds", action="store_true")
    sub.add_parser("stop", help="请求急停（写停止文件）")
    sub.add_parser("clear-stop", help="清除停止文件")

    args = ap.parse_args()
    if args.cmd == "stop":
        request_stop()
        print(f"已写停止文件：{STOP_FILE}（发送进程会立即 feed-hold）")
        return 0
    if args.cmd == "clear-stop":
        clear_stop()
        print("已清除")
        return 0

    lines = Path(args.file).read_text(encoding="utf-8").splitlines()
    return stream(args.port, lines, dry_run=not args.send,
                  allow_oob=args.allow_out_of_bounds, baud=args.baud)


if __name__ == "__main__":
    sys.exit(main())
