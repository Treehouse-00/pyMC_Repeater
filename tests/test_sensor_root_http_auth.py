"""Actual CherryPy routing: the legacy root sensor URLs must not bypass /api auth."""

import json
import subprocess
import sys


def test_sensor_endpoints_only_exist_under_authenticated_api(tmp_path):
    script = r"""
import json, pathlib, socket, sys, urllib.request, urllib.error
import yaml
from repeater.web import http_server as hs

root = pathlib.Path(sys.argv[1])
(root / "index.html").write_text("<html>UI</html>")
config = {"repeater": {"security": {"jwt_secret": "test-secret-only"}},
          "storage": {"storage_dir": str(root / "storage")},
          "web": {"web_path": str(root)},
          "sensors": {"enabled": True, "definitions": [{"name": "modem", "type": "openhop_modem",
              "settings": {"password": "synthetic-secret"}}]}}
path = root / "config.yaml"
path.write_text(yaml.safe_dump(config))
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
hs.WEBSOCKET_AVAILABLE = False
server = hs.HTTPStatsServer(host="127.0.0.1", port=port, config=config, config_path=str(path))
base = f"http://127.0.0.1:{port}"
def status(route, data=None, headers=None):
    req = urllib.request.Request(base + route, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
try:
    server.start()
    before = path.read_bytes()
    roots = [status("/" + name)[0] for name in
             ("sensors_config", "sensors_types", "sensors_read", "sensors_config_update")]
    roots.append(status("/sensors_config_update", data=b'{"enabled":false}',
                        headers={"Content-Type":"application/json"})[0])
    unauth = status("/api/sensors_config")[0]
    token = server.jwt_handler.create_jwt("admin", "test-client")
    authorized, payload = status("/api/sensors_config", headers={"Authorization": "Bearer " + token})
    parsed = json.loads(payload) if authorized == 200 else {}
    password = parsed.get("data", {}).get("definitions", [{}])[0].get("settings", {}).get("password")
    print(json.dumps({"roots":roots, "unauth":unauth, "authorized":authorized,
                      "masked":password == "*****", "disk_untouched":path.read_bytes() == before}))
finally:
    server.stop()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    facts = json.loads(result.stdout.strip().splitlines()[-1])
    assert facts == {
        "roots": [404, 404, 404, 404, 404],
        "unauth": 401,
        "authorized": 200,
        "masked": True,
        "disk_untouched": True,
    }
