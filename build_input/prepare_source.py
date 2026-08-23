from pathlib import Path
import json
import sys

root = Path(sys.argv[1]).resolve()
config_path = root / "config.json"
smart_path = root / "smart_reconnect.py"

cfg = json.loads(config_path.read_text(encoding="utf-8"))
cfg["正常監測間隔秒"] = 2.2
cfg["全域監看擷取最短間隔秒"] = 0.30
cfg["斷線OCR最短間隔秒"] = 2.8
config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

s = smart_path.read_text(encoding="utf-8")
old = '''        if probe_disc:\n            self.last_flow_disconnect_probe = now\n            allow_disc_ocr = primary_disc_state and ((now - self.last_disconnect_ocr_at) >= ocr_gap)\n            if allow_disc_ocr:\n                self.last_disconnect_ocr_at = now\n            disc = self.detect_disconnect_dual(frame, allow_ocr=allow_disc_ocr)\n'''
new = '''        if probe_disc:\n            self.last_flow_disconnect_probe = now\n            if self.state == "監看":\n                # 正常掛機先走純幾何/色彩/模板路徑，避免多視窗輪流做 OCR 把 CPU 吃滿。\n                # 已知三種斷線仍可直接命中；只有真的出現中央斷線對話框形狀時，\n                # 才以低頻 OCR 補未知文字。進入重連後不受這個節流限制。\n                disc = self.detect_disconnect_dual(frame, allow_ocr=False)\n                cheap_dialog = has_disconnect_dialog_shape(frame) or has_central_dialog(frame)\n                if not disc and cheap_dialog and ((now - self.last_disconnect_ocr_at) >= ocr_gap):\n                    self.last_disconnect_ocr_at = now\n                    disc = self.detect_disconnect_dual(frame, allow_ocr=True)\n            else:\n                allow_disc_ocr = primary_disc_state and ((now - self.last_disconnect_ocr_at) >= ocr_gap)\n                if allow_disc_ocr:\n                    self.last_disconnect_ocr_at = now\n                disc = self.detect_disconnect_dual(frame, allow_ocr=allow_disc_ocr)\n'''
if old not in s:
    raise SystemExit("CPU patch target not found")
s = s.replace(old, new, 1)
smart_path.write_text(s, encoding="utf-8")

print("Prepared standalone source")
print("normal_interval", cfg["正常監測間隔秒"])
print("global_capture_gap", cfg["全域監看擷取最短間隔秒"])
print("disconnect_ocr_gap", cfg["斷線OCR最短間隔秒"])
