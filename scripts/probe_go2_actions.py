"""Interactively test Go2 gestures and special moves, recording visual results.

Run ``uv run python scripts/probe_go2_actions.py`` for Sport show moves.
Use ``--catalog mcf`` to try MCF command IDs; the script does not switch modes.
Every command needs confirmation at the terminal. This diagnostic does not
alter the FSM.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD, SPORT_CMD_MCF


# Every installed library entry is assigned exactly once. The order starts
# with stationary commands; movement and flips are confirmed separately.
CATEGORIES = {
    "姿态与恢复": (
        "BalanceStand", "StandUp", "RecoveryStand", "Sit", "RiseSit", "Euler",
        "Pose", "StandOut", "Standup", "StandDown", "Damp", "Handstand",
        "BackStand", "HandStand",
    ),
    "互动与表演": (
        "Hello", "Stretch", "Content", "Dance1", "Dance2", "FingerHeart",
        "WiggleHips", "Scrape", "Wallow", "FrontPounce", "Heart",
    ),
    "跳跃与翻转": (
        "FrontJump", "FrontFlip", "LeftFlip", "RightFlip", "BackFlip", "FreeJump",
    ),
    "行走与步态": (
        "StopMove", "Move", "SwitchGait", "Trigger", "TrajectoryFollow",
        "ContinuousGait", "EconomicGait", "LeadFollow", "Bound", "MoonWalk",
        "OnesidedStep", "CrossStep", "FreeWalk", "CrossWalk", "StaticWalk",
        "TrotRun", "FreeBound", "FreeAvoid", "ClassicWalk",
    ),
    "参数与模式": (
        "BodyHeight", "FootRaiseHeight", "SpeedLevel", "SwitchJoystick",
        "SetAutoRecovery", "SwitchAvoidMode",
    ),
    "状态查询": (
        "GetBodyHeight", "GetFootRaiseHeight", "GetSpeedLevel", "GetState",
        "GetAutoRecovery",
    ),
}
CATALOGS = {"sport": SPORT_CMD, "mcf": SPORT_CMD_MCF}
# Only recognizable gestures and special moves are in the default run.
# Ordinary standing, velocity control, queries and configuration require --all
# or an explicit --actions selection.
ACTION_ONLY = (
    "Hello", "Stretch", "Content", "Dance1", "Dance2", "FingerHeart",
    "Heart", "WiggleHips", "Scrape", "Wallow", "FrontPounce",
    "FrontJump", "FrontFlip", "LeftFlip", "RightFlip", "BackFlip",
    "Handstand", "HandStand", "Bound", "MoonWalk", "OnesidedStep",
    "CrossStep", "CrossWalk", "FreeBound", "FreeJump",
)
PARAMETERIZED = {
    "Euler", "Pose", "Move", "SwitchGait", "Trigger", "BodyHeight",
    "FootRaiseHeight", "SpeedLevel", "TrajectoryFollow", "ContinuousGait",
    "SwitchJoystick", "SetAutoRecovery", "SwitchAvoidMode",
}
HIGH_RISK = {
    "Damp", "StandDown", "StandOut", "Standup", "Handstand", "HandStand",
    "BackStand", "Wallow", "Scrape",
    "FrontJump", "FrontFlip", "LeftFlip", "RightFlip", "BackFlip", "FreeJump",
    "FrontPounce", "Bound", "MoonWalk", "OnesidedStep", "CrossStep",
    "FreeWalk", "CrossWalk", "FreeBound", "FreeAvoid", "ClassicWalk",
    "TrajectoryFollow", "LeadFollow", "Move", "StaticWalk", "TrotRun",
}
TIMED_STOP = {
    "Move", "TrajectoryFollow", "ContinuousGait", "EconomicGait",
    "LeadFollow", "Bound", "MoonWalk", "OnesidedStep", "CrossStep",
    "FreeWalk", "CrossWalk", "StaticWalk", "TrotRun", "FreeBound",
    "FreeAvoid", "ClassicWalk",
}
PARAMETERS = {"Euler": {"x": 0.10, "y": 0.0, "z": 0.0},
              "Pose": {"data": True},
              "Move": {"x": 0.10, "y": 0.0, "z": 0.0}}
MCF_PARAMETERS = {
    action: {"data": True}
    for action in ("FrontFlip", "LeftFlip", "BackFlip", "HandStand",
                   "FreeBound", "FreeJump", "CrossStep")
}
MCF_TOGGLES = {"HandStand", "FreeBound", "FreeJump", "CrossStep"}
RESET_PARAMETERS = {"Euler": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "Pose": {"data": False}}


def category_for(action: str) -> str:
    return next(group for group, names in CATEGORIES.items() if action in names)


def catalog_actions(catalog: dict[str, int]) -> list[str]:
    listed = [action for names in CATEGORIES.values() for action in names if action in catalog]
    if set(listed) != set(catalog) or len(listed) != len(set(listed)):
        raise RuntimeError("动作分类与安装的动作库不一致，请检查脚本分类")
    return listed


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


async def send(conn: UnitreeWebRTCConnection, catalog: dict[str, int], action: str,
               parameters: dict | None = None) -> object:
    options: dict = {"api_id": catalog[action]}
    if parameters is not None:
        options["parameter"] = parameters
    request = asyncio.create_task(
        conn.datachannel.pub_sub.publish_request_new(RTC_TOPIC["SPORT_MOD"], options))
    # A late device reply must still resolve its request after our timeout.
    request.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
    return await asyncio.wait_for(
        asyncio.shield(request), timeout=12.0 if action in {"Dance1", "Dance2"} else 6.0)


def transport_state(conn: UnitreeWebRTCConnection) -> dict:
    pc = getattr(conn, "pc", None)
    datachannel = getattr(conn, "datachannel", None)
    channel = getattr(datachannel, "channel", None)
    return {"peer": getattr(pc, "connectionState", None),
            "channel": getattr(channel, "readyState", None)}


def prompt(question: str, choices: str) -> str:
    while True:
        answer = input(question).strip().lower()
        if answer in choices:
            return answer
        print(f"请输入 {'/'.join(choices)}")


async def async_input(question: str) -> str:
    """Wait for a terminal line without stopping WebRTC heartbeats."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    print(question, end="", flush=True)

    def on_ready() -> None:
        if future.done():
            return
        try:
            line = sys.stdin.readline()
            if not line:
                future.set_exception(EOFError("终端输入已关闭"))
            else:
                future.set_result(line.rstrip("\r\n"))
        except Exception as exc:
            future.set_exception(exc)

    fd = sys.stdin.fileno()
    loop.add_reader(fd, on_ready)
    try:
        return await future
    finally:
        loop.remove_reader(fd)


async def async_prompt(question: str, choices: str) -> str:
    while True:
        answer = (await async_input(question)).strip().lower()
        if answer in choices:
            return answer
        print(f"请输入 {'/'.join(choices)}")


async def ask_parameters(action: str, catalog_name: str) -> tuple[dict | None, bool]:
    sample = (MCF_PARAMETERS.get(action) if catalog_name == "mcf" else None)
    if action not in PARAMETERIZED and sample is None:
        return None, False
    sample = sample or PARAMETERS.get(action)
    hint = json.dumps(sample, ensure_ascii=False) if sample is not None else "无已验证示例"
    print(f"参数示例：{hint}")
    while True:
        raw = (await async_input("参数 JSON（回车采用示例；s 跳过该项）：")).strip()
        if raw.lower() == "s" or (not raw and sample is None):
            if not raw:
                print("缺少可靠参数示例，本项跳过；重跑时可输入 JSON 参数。")
            return None, True
        if not raw:
            return sample.copy(), False
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"JSON 无效：{exc}")
            continue
        if isinstance(parsed, dict):
            return parsed, False
        print("参数必须是 JSON 对象，例如 {\"data\": 1}")


async def stop_after(conn: UnitreeWebRTCConnection, catalog_name: str,
                     catalog: dict[str, int], action: str) -> None:
    await asyncio.sleep(1.5)
    if catalog_name == "mcf" and action in MCF_TOGGLES:
        try:
            off = await send(conn, catalog, action, {"data": False})
            print(f"已关闭 {action}，返回码：{status_code(off)!r}")
        except Exception as exc:
            print(f"关闭 {action} 未确认：{exc}", file=sys.stderr)
    try:
        response = await send(conn, catalog, "StopMove")
        print(f"已定时发送 StopMove，返回码：{status_code(response)!r}")
    except Exception as exc:
        print(f"自动停止未确认：{exc}；请立即使用遥控器停止。", file=sys.stderr)


async def probe(conn: UnitreeWebRTCConnection, observer: SportObserver,
                catalog_name: str, catalog: dict[str, int], action: str,
                observe_seconds: float) -> dict:
    record = {"timestamp": datetime.now(timezone.utc).isoformat(),
              "catalog": catalog_name, "category": category_for(action),
              "action": action, "api_id": catalog[action]}
    print(f"\n[{record['category']}] {action} (API {catalog[action]})")
    if action in HIGH_RISK:
        print("此动作可能摔倒、跳跃或持续移动；确认场地和急停手段。")
    choice = await async_prompt("发送这个动作？[y/n/q] ", "ynq")
    if choice != "y":
        record["result"] = "quit" if choice == "q" else "skipped"
        return record
    if action in HIGH_RISK and (await async_input(
            f"再次确认，请输入动作名 {action}：")).strip() != action:
        record["result"] = "skipped"
        return record
    parameters, skip = await ask_parameters(action, catalog_name)
    if skip:
        record["result"] = "skipped_no_parameters"
        return record
    record["parameters"] = parameters

    started = time.monotonic()
    stop_task = (asyncio.create_task(stop_after(conn, catalog_name, catalog, action))
                 if action in TIMED_STOP or
                 (catalog_name == "mcf" and action in MCF_TOGGLES) else None)
    try:
        response = await send(conn, catalog, action, parameters)
        code = status_code(response)
        record["rpc_code"] = code
        record["request_outcome"] = "replied"
        print(f"设备返回码：{code!r}；0 表示请求被接受，仍需目视确认。")
        if record["category"] == "状态查询":
            excerpt = json.dumps(response, ensure_ascii=False, default=str)[:2000]
            record["response_excerpt"] = excerpt
            print(f"查询返回：{excerpt}")
    except asyncio.TimeoutError:
        record["rpc_code"] = None
        record["request_outcome"] = "timeout"
        record["transport"] = transport_state(conn)
        print(f"回执超时；连接状态：{record['transport']}。请观察是否实际执行。")
        if record["transport"]["peer"] in {"closed", "failed"} or \
                record["transport"]["channel"] in {"closed", "closing"}:
            record["result"] = "error"
            record["error"] = "transport_closed_during_request"
    except Exception as exc:
        record["error"] = repr(exc)
        record["result"] = "error"
        record["transport"] = transport_state(conn)
        print(f"命令发送失败：{type(exc).__name__}: {str(exc) or '等待回执超时或连接已关闭'}")
    finally:
        if stop_task is not None:
            await stop_task
        if action in RESET_PARAMETERS:
            try:
                await send(conn, catalog, action, RESET_PARAMETERS[action])
            except Exception as exc:
                print(f"姿态复位未确认：{exc}")

    if record.get("result") == "error":
        return record
    remaining = observe_seconds - (time.monotonic() - started)
    if remaining > 0:
        await asyncio.sleep(remaining)
    record["telemetry"] = observer.summary_since(started)
    print(f"遥测：{record['telemetry']}")
    question = ("查询结果：返回了预期状态/没有/不确定？[y/n/u] "
                if record["category"] == "状态查询" else
                "实际观察：做到了/没有/不确定？[y/n/u] ")
    seen = await async_prompt(question, "ynu")
    record["visual"] = seen
    record["note"] = (await async_input(
        "备注（可留空，例如动作幅度、错误提示）：")).strip()
    record["result"] = ("observed" if seen == "y" else
                        "rejected" if record.get("rpc_code") not in (None, 0) else
                        "not_observed" if seen == "n" else "unconfirmed")
    return record


def save_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()


def previous_results(path: Path, catalog_name: str) -> list[dict]:
    if not path.exists():
        return []
    results = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if record.get("catalog") == catalog_name:
                results.append(record)
    return results


def print_summary(results: list[dict], actions: list[str]) -> None:
    latest = {r["action"]: r for r in results if r["action"] in actions}
    print("\n分类结果（成功只按你的目视判断统计）：")
    for group, names in CATEGORIES.items():
        selected = [latest[name] for name in names if name in latest]
        if not selected:
            continue
        counts = {kind: sum(r.get("result") == kind for r in selected)
                  for kind in ("observed", "rejected", "not_observed", "unconfirmed")}
        print(f"{group}: 成功 {counts['observed']} / 拒绝 {counts['rejected']} / "
              f"无动作 {counts['not_observed']} / 不确定 {counts['unconfirmed']} / "
              f"已记录 {len(selected)}")
    for kind, label in (("observed", "目视成功"), ("rejected", "设备拒绝"),
                        ("not_observed", "未见动作"), ("unconfirmed", "不确定")):
        names = [name for name in actions if latest.get(name, {}).get("result") == kind]
        print(f"{label}: {', '.join(names) if names else '无'}")
    print("提示：设备拒绝或未见动作不一定表示 Air 不支持，也可能与模式、参数或姿态有关。")


async def run(catalog_name: str, catalog: dict[str, int], actions: list[str],
              observe_seconds: float, output: Path, repeat: bool) -> list[dict]:
    load_dotenv(Path(__file__).resolve().parents[1] / "cloud" / ".env")
    email, password, serial = (os.getenv(key, "") for key in
                               ("GO2_EMAIL", "GO2_PASSWORD", "GO2_SERIAL"))
    if not all((email, password, serial)):
        raise RuntimeError("请在 cloud/.env 配置 GO2_EMAIL、GO2_PASSWORD、GO2_SERIAL")
    observer = SportObserver()
    conn = UnitreeWebRTCConnection(
        WebRTCConnectionMethod.Remote, serialNumber=serial, username=email,
        password=password, region=os.getenv("GO2_REGION", "cn"), device_type="Go2")
    results = previous_results(output, catalog_name)
    finished = {r["action"] for r in results
                if r.get("result") in ("observed", "rejected", "not_observed",
                                       "unconfirmed")}
    pending = [action for action in actions if repeat or action not in finished]
    if not pending:
        print("没有待测动作。使用 --repeat 可重新测试。")
        return results
    try:
        print("连接 Go2 Air…")
        await conn.connect()
        conn.datachannel.pub_sub.subscribe(RTC_TOPIC["LF_SPORT_MOD_STATE"], observer.on_state)
        # The production connection does this to activate the Sport endpoint.
        balance = await send(conn, catalog, "BalanceStand")
        code = status_code(balance)
        if code not in (None, 0):
            raise RuntimeError(f"BalanceStand 被设备拒绝：{code}")
        await asyncio.sleep(1.0)
        for index, action in enumerate(pending, 1):
            print(f"\n进度 {index}/{len(pending)}；结果文件：{output}")
            result = await probe(conn, observer, catalog_name, catalog, action,
                                 observe_seconds)
            save_record(output, result)
            results.append(result)
            print(f"已保存：{result['result']}")
            if result["result"] in {"error", "quit"}:
                break
            state = transport_state(conn)
            if state["peer"] in {"closed", "failed"} or state["channel"] in {"closed", "closing"}:
                print(f"WebRTC 连接已关闭：{state}。停止发送后续动作。")
                break
            if result["result"] not in {"skipped", "skipped_no_parameters"}:
                if (await async_prompt(
                        "上一动作已结束、姿态安全，继续下一项？[y/n] ", "yn")) != "y":
                    break
    finally:
        try:
            await conn.disconnect()
        except Exception as exc:
            print(f"断开连接失败：{exc}", file=sys.stderr)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", choices=tuple(CATALOGS), default="sport",
                        help="动作编号表；MCF 不会自动切换机器狗模式")
    parser.add_argument("--category", choices=tuple(CATEGORIES),
                        help="只测试指定类别，包含该类别的全部命令")
    parser.add_argument("--actions", nargs="+", help="只测试指定命令，按输入顺序")
    parser.add_argument("--all", action="store_true", help="测试完整动作表，包含基础控制与查询")
    parser.add_argument("--observe-seconds", type=float, default=8.0,
                        help="每个已接受动作的遥测观察时间，默认 8 秒")
    parser.add_argument("--output", type=Path, help="JSONL 结果路径；再次指定同一路径可续测")
    parser.add_argument("--repeat", action="store_true", help="重测结果文件中已有的命令")
    parser.add_argument("--list", action="store_true", help="仅列出候选动作，不连接设备")
    args = parser.parse_args()
    catalog = CATALOGS[args.catalog]
    listed = catalog_actions(catalog)
    if args.actions:
        invalid = [action for action in args.actions if action not in catalog]
        if invalid:
            parser.error(f"不在 {args.catalog} 表中的动作：{', '.join(invalid)}")
        actions = list(dict.fromkeys(args.actions))
    elif args.category:
        actions = [action for action in listed if category_for(action) == args.category]
    elif args.all:
        actions = listed
    else:
        actions = [action for action in ACTION_ONLY if action in catalog]
    if args.list:
        for action in actions:
            print(f"{category_for(action):8} {action:20} {catalog[action]:4}"
                  f" {'高风险' if action in HIGH_RISK else ''}")
        print(f"共 {len(actions)} 个命令；这只是库中的命令表，不代表 Air 实机支持。")
        return 0
    if not sys.stdin.isatty():
        parser.error("实机动作测试需要交互终端")
    if not 1 <= args.observe_seconds <= 30:
        parser.error("--observe-seconds 须在 1 到 30 秒之间")
    if args.catalog == "mcf":
        print("MCF 表编号与普通 Sport 表不同；脚本不会切换或确认机器狗当前模式。")
        print("Hello、Dance1/2 等同编号动作成功，也无法单独证明 MCF 模式已启用。")
        if prompt("继续尝试 MCF 编号？[y/n] ", "yn") != "y":
            return 0
    print("请先断开 PWA/API 对 Go2 的连接，留出动作空间并准备遥控器急停。")
    print(f"本次计划逐项测试 {len(actions)} 个命令；每项均可跳过或退出。")
    if prompt("开始连接并测试？[y/n] ", "yn") != "y":
        return 0
    output = args.output or (Path(__file__).resolve().parents[1] / "cloud" / "go2" /
                             "data" / "action_probes" /
                             f"{args.catalog}_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
    try:
        results = asyncio.run(run(args.catalog, catalog, actions,
                                  args.observe_seconds, output, args.repeat))
    except KeyboardInterrupt:
        print(f"\n已中断；此前结果保存在 {output}")
        return 130
    except Exception as exc:
        print(f"测试失败：{exc}；此前结果保存在 {output}", file=sys.stderr)
        return 1
    print_summary(results, actions)
    print(f"完整记录：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
