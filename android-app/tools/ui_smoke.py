"""Exercise the unpaired UI on an explicitly chosen Android test target."""
import argparse
from contextlib import suppress
import os
import re
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET


PACKAGE = "dev.tickdesk.app"
TEMP_XML = "/data/local/tmp/tickdesk-ui-smoke.xml"


class Device:
    def __init__(self, adb, serial):
        self.command = [adb, "-s", serial]

    def run(self, *args):
        return subprocess.check_output([*self.command, *args], timeout=20)

    def shell(self, *args):
        return self.run("shell", *args)

    def nodes(self):
        self.run("wait-for-device")
        self.shell("uiautomator", "dump", TEMP_XML)
        xml = self.run("exec-out", "cat", TEMP_XML)
        return [
            node for node in ET.fromstring(xml).iter("node")
            if node.get("package") == PACKAGE
        ]

    def locate(self, label, scroll=0, bottom=False, contains=False):
        for attempt in range(abs(scroll) + 1):
            nodes = self.nodes()
            matching = [
                n for n in nodes if any(
                    label in value if contains else label in value.splitlines()
                    for value in (n.get("text", ""), n.get("content-desc", ""))
                )
            ]
            if matching:
                return max(matching, key=lambda n: bounds(n)[3]) if bottom else matching[0]
            if attempt < abs(scroll):
                scrolling = [n for n in nodes if n.get("scrollable") == "true"]
                app = max(scrolling, key=node_area) if scrolling else (nodes[0] if nodes else None)
                if app is None:
                    break
                x1, y1, x2, y2 = bounds(app)
                x = (x1 + x2) // 2
                lower = y1 + (y2 - y1) * 3 // 4
                upper = y1 + (y2 - y1) * 2 // 5
                start, end = (lower, upper) if scroll > 0 else (upper, lower)
                self.shell("input", "swipe", str(x), str(start), str(x), str(end), "460")
        raise AssertionError(f"UI label unavailable: {label}")

    def tap(self, label, scroll=0, bottom=False):
        x1, y1, x2, y2 = bounds(self.locate(label, scroll=scroll, bottom=bottom))
        assert x2 > x1 and y2 > y1, f"Empty bounds for {label}"
        self.shell("input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))

    def back(self):
        self.shell("input", "keyevent", "4")

    def chat_field(self):
        edits = [n for n in self.nodes() if n.get("class") == "android.widget.EditText"]
        if len(edits) == 1:
            x1, y1, x2, y2 = bounds(edits[0])
            self.shell("input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
        else:
            self.tap("输入消息", scroll=6)


def bounds(node):
    return [int(value) for value in re.findall(r"\d+", node.get("bounds", ""))]


def node_area(node):
    x1, y1, x2, y2 = bounds(node)
    return (x2 - x1) * (y2 - y1)


def smoke(device, keyboard, screenshots):
    device.shell("am", "start", "-W", "-n", f"{PACKAGE}/.MainActivity")
    device.tap("事项", bottom=True)
    device.locate("事项尚未同步", scroll=3)
    device.tap("对话", bottom=True)
    device.locate("对话历史", scroll=2, contains=True)
    if keyboard:
        device.chat_field()
        device.nodes()
        device.shell("input", "text", "tickdesk_qa")
        device.back()
        device.locate("tickdesk_qa", contains=True)
        print("PASS chat keyboard input and dismiss")
    device.tap("选股", bottom=True)
    device.locate("最近一轮", scroll=3)
    device.tap("持仓", bottom=True)
    device.tap("成交记录 / 独立补录", scroll=5)
    device.locate("成交记录 / 回填")
    if keyboard:
        device.tap("实际成交价", scroll=12)
        device.nodes()
        device.shell("input", "text", "10.75")
        device.back()
        device.locate("10.75", contains=True)
        print("PASS trade keyboard input and first Back")
    if screenshots:
        (screenshots / "execution.png").write_bytes(device.run("exec-out", "screencap", "-p"))
    device.back()
    device.tap("截图导入", scroll=5)
    device.locate("选择图片", scroll=6)
    device.back()
    device.tap("设置", bottom=True)
    device.locate("工作台扫码配对", scroll=-12, contains=True)
    device.tap("打开分析暂停与恢复", scroll=8)
    device.locate("分析状态尚未同步", scroll=4)
    device.back()
    device.tap("打开提醒与系统权限", scroll=12)
    device.locate("系统权限", scroll=4)
    if screenshots:
        (screenshots / "reminders.png").write_bytes(device.run("exec-out", "screencap", "-p"))
    device.back()
    print("PASS all five tabs, trade, import, analysis, reminder settings and Back")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--adb", default=str(Path(os.environ.get("LOCALAPPDATA", ".")) /
                                             "Android/Sdk/platform-tools/adb.exe"))
    parser.add_argument("--keyboard", action="store_true")
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    device = Device(args.adb, args.serial)
    try:
        smoke(device, args.keyboard, args.screenshots)
    finally:
        with suppress(subprocess.CalledProcessError, subprocess.TimeoutExpired):
            device.shell("rm", "-f", TEMP_XML)


if __name__ == "__main__":
    main()
