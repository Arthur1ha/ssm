"""Interactively probe Go2 Air sport actions over the project's WebRTC transport.

Run with ``uv run python scripts/probe_go2_actions.py``. Every motion needs
confirmation at the terminal. This diagnostic does not alter the product FSM.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD


# Only stationary, relatively gentle actions are in the default run.
DEFAULT_ACTIONS = ("WiggleHips", "Sit", "RiseSit", "FingerHeart", "Scrape")
EXTRA_ACTIONS = ("Content", "Wallow", "FrontPounce", "Euler", "Pose")
ALL_ACTIONS = DEFAULT_ACTIONS + EXTRA_ACTIONS
PARAMETERS = {"Euler": {"x": 0.10, "y": 0.0, "z": 0.0},
              "Pose": {"data": True}}
RESET_PARAMETERS = {"Euler": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "Pose": {"data": False}}


def status_code(response: object) -> object:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    header = data.get("header")
    if not isinstance(header, dict):
        return None
    status = header.get("status")
    return status.get("code") if isinstance(status, dict) else None


class SportObserver:
    def __init__(self) -> None:
        self.samples: list[tuple[float, float, object, object]] = []

    def on_state(self, message: dict) -> None:
        data = message.get("data", {})
        inner = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(inner, dict):
            return
        try:
            progress = float(inner.get("progress") or 0.0)
        except (TypeError, ValueError):
            return
        if math.isfinite(progress):
            self.samples.append((time.monotonic(), progress, inner.get("mode"),
                                 inner.get("body_height")))
            if len(self.samples) > 500:
                del self.samples[:250]

    def summary_since(self, start: float) -> dict:
        recent = [(progress, mode, height) for stamp, progress, mode, height in self.samples
                  if stamp >= start]
        heights = [float(height) for _, _, height in recent
                   if isinstance(height, (float, int)) and math.isfinite(height)]
        return {"samples": len(recent),
                "max_progress": round(max((p for p, _, _ in recent), default=0.0), 3),
                "last_mode": recent[-1][1] if recent else None,
                "body_height_range": [round(min(heights), 3), round(max(heights), 3)]
                if heights else None}


async def send(conn: UnitreeWebRTCConnection, action: str,
               parameters: dict | None = None) -> object:
    options: dict = {"api_id": SPORT_CMD[action]}
    if parameters is not None:
        options["parameter"] = parameters
    return await asyncio.wait_for(
        conn.datachannel.pub_sub.publish_request_new(RTC_TOPIC["SPORT_MOD"], options),
        timeout=6.0)


def prompt(question: str, choices: str) -> str:
    while True:
        answer = input(question).strip().lower()
        if answer in choices:
            return answer
        print(f"请输入 {'/'.join(choices)}")


async def probe(conn: UnitreeWebRTCConnection, observer: SportObserver,
                action: str, observe_seconds: float) -> dict:
    print(f"\n{action} (API {SPORT_CMD[action]})")
    choice = prompt("发送这个动作？[y/n/q] ", "ynq")
    if choice != "y":
        return {"action": action, "result": "quit" if choice == "q" else "skipped"}

    started = time.monotonic()
    try:
        response = await send(conn, action, PARAMETERS.get(action))
        code = status_code(response)
    except (Exception, KeyboardInterrupt) as exc:
        return {"action": action, "result": "error", "error": repr(exc)}
    finally:
        if action in RESET_PARAMETERS:
            try:
                await asyncio.sleep(1.0)
                await send(conn, action, RESET_PARAMETERS[action])
            except Exception as exc:
                print(f"姿态复位未确认：{exc}")

    if code in (None, 0):
        print("设备返回码 0：请求已接受。" if code == 0 else "未取得明确返回码。")
        print("正在观察遥测。")
        await asyncio.sleep(observe_seconds)
    else:
        print(f"设备返回码：{code!r}；请观察机器狗是否实际动作。")
    telemetry = observer.summary_since(started)
    print(f"遥测：{telemetry}")
    seen = prompt("目视结果：执行了/没执行/不确定？[y/n/u] ", "ynu")
    result = ("observed" if seen == "y" else
              "rejected" if code not in (None, 0) else
              "not_observed" if seen == "n" else "unconfirmed")
    return {"action": action, "api_id": SPORT_CMD[action], "rpc_code": code,
            "telemetry": telemetry, "visual": seen, "result": result}


async def run(actions: list[str], observe_seconds: float) -> list[dict]:
    load_dotenv(Path(__file__).resolve().parents[1] / "cloud" / ".env")
    email, password, serial = (os.getenv(key, "") for key in
                               ("GO2_EMAIL", "GO2_PASSWORD", "GO2_SERIAL"))
    if not all((email, password, serial)):
        raise RuntimeError("请在 cloud/.env 配置 GO2_EMAIL、GO2_PASSWORD、GO2_SERIAL")
    observer = SportObserver()
    conn = UnitreeWebRTCConnection(
        WebRTCConnectionMethod.Remote, serialNumber=serial, username=email,
        password=password, region=os.getenv("GO2_REGION", "cn"), device_type="Go2")
    results: list[dict] = []
    try:
        print("连接 Go2 Air…")
        await conn.connect()
        conn.datachannel.pub_sub.subscribe(RTC_TOPIC["LF_SPORT_MOD_STATE"], observer.on_state)
        # The production connection does this to activate the Sport endpoint.
        balance = await send(conn, "BalanceStand")
        code = status_code(balance)
        if code not in (None, 0):
            raise RuntimeError(f"BalanceStand 被设备拒绝：{code}")
        await asyncio.sleep(1.0)
        for index, action in enumerate(actions):
            result = await probe(conn, observer, action, observe_seconds)
            results.append(result)
            print(f"记录：{result['result']}")
            if result["result"] in {"error", "quit"}:
                break
            if action == "Sit" and result["result"] in {"observed", "unconfirmed"}:
                if "RiseSit" not in actions[index + 1:]:
                    print("机器狗可能仍处于坐姿，请先手动恢复站立。")
                    break
            if action == "RiseSit" and result["result"] != "observed":
                print("站立姿态未确认，停止后续动作。")
                break
    finally:
        try:
            await conn.disconnect()
        except Exception as exc:
            print(f"断开连接失败：{exc}", file=sys.stderr)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actions", nargs="+", choices=ALL_ACTIONS,
                        default=list(DEFAULT_ACTIONS), help="按指定顺序测试动作")
    parser.add_argument("--observe-seconds", type=float, default=8.0,
                        help="每个已接受动作的遥测观察时间，默认 8 秒")
    parser.add_argument("--list", action="store_true", help="仅列出候选动作，不连接设备")
    args = parser.parse_args()
    if args.list:
        for action in ALL_ACTIONS:
            print(f"{action:14} {SPORT_CMD[action]} {'默认' if action in DEFAULT_ACTIONS else '需显式指定'}")
        return 0
    if not sys.stdin.isatty():
        parser.error("实机动作测试需要交互终端")
    if not 1 <= args.observe_seconds <= 30:
        parser.error("--observe-seconds 须在 1 到 30 秒之间")
    print("请先断开 PWA/API 对 Go2 的连接，并留出动作空间。")
    if prompt("开始连接并测试？[y/n] ", "yn") != "y":
        return 0
    try:
        results = asyncio.run(run(args.actions, args.observe_seconds))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:
        print(f"测试失败：{exc}", file=sys.stderr)
        return 1
    print("\n测试汇总：")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print("observed = 目视确认；not_observed = 未看到动作；"
          "unconfirmed = 尚不能判断；rejected = 设备拒绝。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
