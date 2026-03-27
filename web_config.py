"""
web_config.py — HTTP configuration server for sACN Playback
Runs on core 1 of the RP2040 via _thread.start_new_thread(server.start, ()).
"""
import json
import uos
import _thread
import machine

try:
    import ujson as json
except ImportError:
    pass

try:
    import usocket as socket
except ImportError:
    import socket

CONFIG_FILE = 'config.json'

_DEFAULTS = {
    "network": {
        "ip":      "10.0.0.102",
        "subnet":  "255.255.255.0",
        "gateway": "10.0.0.1",
        "dns":     "8.8.8.8",
    },
    "sacn": {
        "source_name":      "PlaybackScript",
        "priority":         0,
        "universe_start":   1,
        "universe_count":   20,
        "multicast_enabled": True,
    },
    "button": {
        # Per-button list: each entry maps a GPIO pin to a playback mode.
        # Modes: 'toggle' (press on/press off, hold 5s to capture),
        #        'single' (plays for 10 s then stops),
        #        'active' (plays while held).
        "buttons": [
            {"pin": 26, "mode": "active", "label": "Scene 1"},
            {"pin": 1,  "mode": "toggle", "label": "Scene 2"},
            {"pin": 2,  "mode": "toggle", "label": "Scene 3"},
            {"pin": 3,  "mode": "toggle", "label": "Scene 4"},
            {"pin": 4,  "mode": "toggle", "label": "Scene 5"},
            {"pin": 5,  "mode": "toggle", "label": "Scene 6"},
            {"pin": 6,  "mode": "single", "label": "Scene 7"},
            {"pin": 7,  "mode": "single", "label": "Scene 8"},
        ],
        # Optional separate all-off button pin (null = disabled).
        "off_pin": None,
    },
    "targets": [

    ],
}


def _deep_merge(base, overlay):
    """Return a new dict: overlay values override base, recursing into nested dicts."""
    result = {}
    for k, v in base.items():
        if k in overlay:
            if isinstance(v, dict) and isinstance(overlay[k], dict):
                result[k] = _deep_merge(v, overlay[k])
            else:
                result[k] = overlay[k]
        else:
            result[k] = v
    for k, v in overlay.items():
        if k not in result:
            result[k] = v
    return result


def _html_esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _url_decode(s):
    result = []
    i = 0
    while i < len(s):
        if s[i] == '+':
            result.append(' ')
            i += 1
        elif s[i] == '%' and i + 2 < len(s):
            try:
                result.append(chr(int(s[i + 1:i + 3], 16)))
            except ValueError:
                result.append(s[i])
            i += 3
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)


def _parse_form(body_bytes):
    params = {}
    body = body_bytes.decode('utf-8', 'replace') if isinstance(body_bytes, bytes) else body_bytes
    for pair in body.split('&'):
        if '=' in pair:
            k, v = pair.split('=', 1)
            params[_url_decode(k)] = _url_decode(v)
    return params


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------

class ConfigManager:
    """
    Thread-safe configuration store backed by config.json.
    Provides section-level updates and convenience accessors.
    """

    def __init__(self, path=CONFIG_FILE):
        self._path = path
        self._lock = _thread.allocate_lock()
        self._cfg = {}
        self._load()

    def _load(self):
        try:
            with open(self._path, 'r') as f:
                loaded = json.load(f)
            self._cfg = _deep_merge(_DEFAULTS, loaded)
        except Exception:
            print('ConfigManager: using defaults (config.json missing or invalid)')
            self._cfg = _deep_merge(_DEFAULTS, {})
            self._save_nolock()

    def _save_nolock(self):
        tmp = self._path + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(self._cfg, f)
            try:
                uos.remove(self._path)
            except Exception:
                pass
            uos.rename(tmp, self._path)
        except Exception as e:
            print('ConfigManager: save failed:', e)
            try:
                uos.remove(tmp)
            except Exception:
                pass

    # --- Public API ---

    def get(self, *keys):
        """Thread-safe nested read: cfg.get('network', 'ip')"""
        self._lock.acquire()
        try:
            node = self._cfg
            for k in keys:
                node = node[k]
            return node
        finally:
            self._lock.release()

    def get_all(self):
        """Return a deep copy of the full config."""
        self._lock.acquire()
        try:
            return json.loads(json.dumps(self._cfg))
        finally:
            self._lock.release()

    def update_section(self, section, values):
        """Replace keys in one top-level section dict and persist."""
        self._lock.acquire()
        try:
            if section in self._cfg and isinstance(self._cfg[section], dict):
                self._cfg[section].update(values)
            else:
                self._cfg[section] = values
            self._save_nolock()
        finally:
            self._lock.release()

    def update_targets(self, targets_list):
        """Replace the full targets list and persist."""
        self._lock.acquire()
        try:
            self._cfg['targets'] = targets_list
            self._save_nolock()
        finally:
            self._lock.release()

    # --- Convenience accessors ---

    def universes(self):
        s = self.get('sacn', 'universe_start')
        c = self.get('sacn', 'universe_count')
        return list(range(s, s + c))

    def target_ips(self):
        return [t['ip'] for t in self.get('targets') if t.get('ip')]

    def button_pins(self):
        """Return list of GPIO pin numbers, one per scene button."""
        return [b['pin'] for b in self.get('button', 'buttons')]

    def button_modes(self):
        """Return list of mode strings, one per scene button."""
        return [b['mode'] for b in self.get('button', 'buttons')]

    def button_off_pin(self):
        """Return the all-off button GPIO pin, or None if disabled."""
        return self.get('button', 'off_pin')


# ---------------------------------------------------------------------------
# WebConfigServer
# ---------------------------------------------------------------------------

class WebConfigServer:
    """
    Minimal HTTP/1.0 server serving a Bootstrap 5 configuration page.
    Designed to run on core 1: call server.start() from a new thread.
    """

    def __init__(self, config_manager, port=80):
        self._cfg = config_manager
        self._port = port
        self._sock = None
        self._controller = None
        self._reboot_pending = False
        self._flag_lock = _thread.allocate_lock()

    def set_controller(self, controller):
        """Bind the PlaybackController so capture requests can be handled."""
        self._controller = controller

    def start(self):
        """Blocking accept loop. Call via _thread.start_new_thread(server.start, ())."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        self._sock.bind(('', self._port))
        self._sock.listen(2)
        print('Web config: http://{}:{}/'.format(self._cfg.get('network', 'ip'), self._port))
        while True:
            try:
                conn, addr = self._sock.accept()
                conn.settimeout(5)
                try:
                    self._handle(conn)
                except Exception as e:
                    print('WebConfig handler error:', e)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception as e:
                print('WebConfig accept error:', e)

    # --- Socket helpers ---

    def _w(self, conn, s):
        conn.sendall(s.encode('utf-8') if isinstance(s, str) else s)

    def _readline(self, conn):
        buf = b''
        while True:
            try:
                c = conn.recv(1)
            except Exception:
                break
            if not c:
                break
            buf += c
            if buf.endswith(b'\r\n'):
                return buf[:-2].decode('utf-8', 'replace')
        return buf.decode('utf-8', 'replace')

    def _read_body(self, conn, length):
        data = b''
        remaining = length
        while remaining > 0:
            try:
                chunk = conn.recv(min(remaining, 256))
            except Exception:
                break
            if not chunk:
                break
            data += chunk
            remaining -= len(chunk)
        return data

    # --- Request dispatcher ---

    def _handle(self, conn):
        req_line = self._readline(conn)
        if not req_line:
            return
        parts = req_line.split(' ')
        if len(parts) < 2:
            return
        method, raw_path = parts[0], parts[1]
        if '?' in raw_path:
            path, qs = raw_path.split('?', 1)
            query = _parse_form(qs)
        else:
            path, query = raw_path, {}

        headers = {}
        while True:
            line = self._readline(conn)
            if not line:
                break
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip().lower()] = v.strip()

        clen = 0
        if method == 'POST':
            try:
                clen = int(headers.get('content-length', '0'))
            except ValueError:
                clen = 0

        # Streaming import: bypass body buffering to avoid OOM on large files
        if path == '/import/scenes' and method == 'POST':
            self._import_scenes(conn, headers, clen)
            return

        body = b''
        if method == 'POST' and clen > 0:
            body = self._read_body(conn, clen)

        params = _parse_form(body) if body else {}

        if path in ('/', '/index.html'):
            self._serve_index(conn, query)
        elif path == '/export/scenes' and method == 'GET':
            self._export_scenes(conn)
        elif path == '/save/network' and method == 'POST':
            self._save_network(conn, params)
        elif path == '/save/sacn' and method == 'POST':
            self._save_sacn(conn, params)
        elif path == '/save/button' and method == 'POST':
            self._save_button(conn, params)
        elif path == '/save/targets' and method == 'POST':
            self._save_targets(conn, params)
        elif path == '/status' and method == 'GET':
            self._status(conn)
        elif path.startswith('/play/') and method == 'POST':
            try:
                scene_idx = int(path.split('/')[-1])
                self._play(conn, scene_idx)
            except ValueError:
                self._w(conn, 'HTTP/1.0 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nBad scene index')
        elif path.startswith('/capture/') and method == 'POST':
            try:
                scene_idx = int(path.split('/')[-1])
                self._capture(conn, scene_idx)
            except ValueError:
                self._w(conn, 'HTTP/1.0 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nBad scene index')
        elif path == '/release' and method == 'POST':
            self._release(conn)
        elif path == '/toggle/multicast' and method == 'POST':
            self._toggle_multicast(conn)
        elif path == '/reboot' and method == 'POST':
            self._w(conn, 'HTTP/1.0 303 See Other\r\nLocation: /\r\n\r\n')
            machine.reset()
        else:
            self._w(conn, 'HTTP/1.0 404 Not Found\r\nContent-Type: text/plain\r\n\r\nNot found')

    # --- POST handlers ---

    def _status(self, conn):
        states = []
        multicast = self._cfg.get('sacn', 'multicast_enabled')
        if self._controller is not None:
            try:
                states = [bool(s) for s in self._controller.scene_active]
                multicast = self._controller.multicast_enabled
            except Exception:
                pass
        body = json.dumps({'scenes': states, 'multicast': multicast})
        self._w(conn, 'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nCache-Control: no-cache\r\n\r\n')
        self._w(conn, body)

    def _play(self, conn, scene_idx):
        if self._controller is None:
            self._w(conn, 'HTTP/1.0 503 Service Unavailable\r\nContent-Type: text/plain\r\n\r\nController not ready')
            return
        print('Web play: scene {}'.format(scene_idx))
        try:
            active = self._controller.scene_active[scene_idx]
            self._controller.scene_active[scene_idx] = not active
            if active:
                self._controller.send_zeros(list(self._controller.storage.get(scene_idx).keys()))
            else:
                # Deactivate all other scenes before activating this one
                for i in range(len(self._controller.scene_active)):
                    if i != scene_idx and self._controller.scene_active[i]:
                        self._controller.send_zeros(list(self._controller.storage.get(i).keys()))
                        self._controller.scene_active[i] = False
                self._controller.play_scene(scene_idx)
        except Exception as e:
            print('Web play error:', e)
        self._redirect(conn)

    def _release(self, conn):
        if self._controller is not None:
            try:
                for i in range(len(self._controller.scene_active)):
                    self._controller.scene_active[i] = False
                self._controller.send_zeros()
            except Exception as e:
                print('Web release error:', e)
        self._redirect(conn)

    def _toggle_multicast(self, conn):
        current = self._cfg.get('sacn', 'multicast_enabled')
        new_val = not current
        self._cfg.update_section('sacn', {'multicast_enabled': new_val})
        if self._controller is not None:
            self._controller.multicast_enabled = new_val
        print('Multicast {}'.format('enabled' if new_val else 'disabled'))
        self._redirect(conn)

    def _capture(self, conn, scene_idx):
        if self._controller is None:
            self._w(conn, 'HTTP/1.0 503 Service Unavailable\r\nContent-Type: text/plain\r\n\r\nController not ready')
            return
        try:
            conn.settimeout(20)  # capture can take up to 15 s; give the connection room
        except Exception:
            pass
        print('Web capture: scene {}'.format(scene_idx))
        got, total = self._controller.capture_current_into_scene(scene_idx)
        self._w(conn, 'HTTP/1.0 303 See Other\r\nLocation: /?capture={}&got={}&total={}\r\n\r\n'.format(
            scene_idx, got, total))

    def _export_scenes(self, conn):
        path = self._controller.storage.path if self._controller else 'scenes.bin'
        try:
            size = uos.stat(path)[6]
        except OSError:
            self._w(conn, 'HTTP/1.0 404 Not Found\r\nContent-Type: text/plain\r\n\r\nNo scenes file')
            return
        self._w(conn, 'HTTP/1.0 200 OK\r\n')
        self._w(conn, 'Content-Type: application/octet-stream\r\n')
        self._w(conn, 'Content-Disposition: attachment; filename="scenes.bin"\r\n')
        self._w(conn, 'Content-Length: {}\r\n\r\n'.format(size))
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(512)
                if not chunk:
                    break
                conn.sendall(chunk)

    def _import_scenes(self, conn, headers, content_length):
        """Stream a multipart file upload directly to disk without buffering in RAM."""
        ct = headers.get('content-type', '')
        boundary = None
        for part in ct.split(';'):
            part = part.strip()
            if part.startswith('boundary='):
                boundary = part[9:].strip('"')
                break
        if not boundary or not content_length:
            self._w(conn, 'HTTP/1.0 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nMissing boundary or content-length')
            return
        try:
            conn.settimeout(30)
        except Exception:
            pass
        # Read boundary line + part headers to find where file data starts
        consumed = 0
        line = self._readline(conn)
        consumed += len(line) + 2
        while True:
            line = self._readline(conn)
            consumed += len(line) + 2
            if not line:
                break  # blank line terminates part headers
        # End marker: \r\n--{boundary}--\r\n
        end_marker_len = len(boundary) + 8
        file_len = content_length - consumed - end_marker_len
        if file_len <= 0:
            self._w(conn, 'HTTP/1.0 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nEmpty or malformed file upload')
            return
        path = self._controller.storage.path if self._controller else 'scenes.bin'
        tmp = path + '.import.tmp'
        try:
            written = 0
            with open(tmp, 'wb') as f:
                remaining = file_len
                while remaining > 0:
                    chunk = conn.recv(min(remaining, 512))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                    written += len(chunk)
            if written != file_len:
                raise ValueError('Short read: {} of {} bytes'.format(written, file_len))
            try:
                uos.remove(path)
            except Exception:
                pass
            uos.rename(tmp, path)
            if self._controller:
                self._controller.storage.load()
            print('Import: {} bytes -> {}'.format(written, path))
        except Exception as e:
            print('Import error:', e)
            try:
                uos.remove(tmp)
            except Exception:
                pass
            self._w(conn, 'HTTP/1.0 500 Internal Server Error\r\nContent-Type: text/plain\r\n\r\nImport failed: {}'.format(str(e)))
            return
        self._redirect(conn)

    def _save_network(self, conn, params):
        updates = {}
        for f in ('ip', 'subnet', 'gateway', 'dns'):
            if f in params and params[f].strip():
                updates[f] = params[f].strip()
        self._cfg.update_section('network', updates)
        self._flag_lock.acquire()
        try:
            self._reboot_pending = True
        finally:
            self._flag_lock.release()
        self._redirect(conn)

    def _save_sacn(self, conn, params):
        updates = {}
        if 'source_name' in params:
            updates['source_name'] = params['source_name'].strip()[:63]
        for f in ('priority', 'universe_start', 'universe_count'):
            if f in params:
                try:
                    updates[f] = int(params[f])
                except ValueError:
                    pass
        self._cfg.update_section('sacn', updates)
        self._redirect(conn)

    def _save_button(self, conn, params):
        # Rebuild the buttons list from per-row form fields (pin_N, mode_N, label_N, del_btn_N)
        buttons = []
        i = 0
        while 'pin_{}'.format(i) in params:
            if 'del_btn_{}'.format(i) not in params:
                pin_str = params.get('pin_{}'.format(i), '').strip()
                mode = params.get('mode_{}'.format(i), 'toggle').strip()
                if mode not in ('toggle', 'single', 'active'):
                    mode = 'toggle'
                label = params.get('label_{}'.format(i), '').strip()
                if pin_str:
                    try:
                        buttons.append({'pin': int(pin_str), 'mode': mode, 'label': label})
                    except ValueError:
                        pass
            i += 1
        # New button row
        new_pin_str = params.get('pin_new', '').strip()
        if new_pin_str:
            try:
                new_mode = params.get('mode_new', 'toggle').strip()
                if new_mode not in ('toggle', 'single', 'active'):
                    new_mode = 'toggle'
                new_label = params.get('label_new', '').strip()
                buttons.append({'pin': int(new_pin_str), 'mode': new_mode, 'label': new_label})
            except ValueError:
                pass
        # Off-pin (empty string means disabled)
        off_pin_str = params.get('off_pin', '').strip()
        off_pin = None
        if off_pin_str:
            try:
                off_pin = int(off_pin_str)
            except ValueError:
                pass
        self._cfg.update_section('button', {'buttons': buttons, 'off_pin': off_pin})
        self._redirect(conn)

    def _save_targets(self, conn, params):
        new_targets = []
        i = 0
        while 'ip_{}'.format(i) in params:
            if 'del_{}'.format(i) not in params:
                ip = params.get('ip_{}'.format(i), '').strip()
                label = params.get('label_{}'.format(i), '').strip()
                if ip:
                    new_targets.append({'ip': ip, 'label': label})
            i += 1
        self._cfg.update_targets(new_targets)
        self._redirect(conn)

    def _redirect(self, conn):
        self._w(conn, 'HTTP/1.0 303 See Other\r\nLocation: /\r\n\r\n')

    # --- HTML page ---

    def _serve_index(self, conn, query=None):
        if query is None:
            query = {}
        cfg = self._cfg.get_all()
        net = cfg['network']
        sacn = cfg['sacn']
        btn = cfg['button']
        targets = cfg['targets']

        self._flag_lock.acquire()
        try:
            reboot_pending = self._reboot_pending
        finally:
            self._flag_lock.release()

        w = self._w

        w(conn, 'HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n')
        w(conn, '<!DOCTYPE html><html lang="en"><head>')
        w(conn, '<meta charset="UTF-8">')
        w(conn, '<meta name="viewport" content="width=device-width,initial-scale=1">')
        w(conn, '<title>sACN Playback</title>')
        w(conn, '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">')
        w(conn, '<style>body{background:#f8f9fa}</style>')
        w(conn, '</head><body>')

        # Navbar
        w(conn, '<nav class="navbar navbar-dark bg-dark">')
        w(conn, '<div class="container-fluid">')
        w(conn, '<span class="navbar-brand fw-bold">sACN Playback</span>')
        w(conn, '<span class="navbar-text text-light small">Pico RP2040 &bull; {}</span>'.format(_html_esc(net['ip'])))
        w(conn, '</div></nav>')

        w(conn, '<div class="container py-4">')

        # Reboot banner
        if reboot_pending:
            w(conn, '<div class="alert alert-warning d-flex align-items-center gap-3">')
            w(conn, '<span><strong>Network settings changed.</strong> Reboot required.</span>')
            w(conn, '<form method="post" action="/reboot" class="ms-auto">')
            w(conn, '<button class="btn btn-warning btn-sm">Reboot Now</button></form>')
            w(conn, '</div>')

        # Capture result banner
        if 'capture' in query:
            try:
                scene_idx = int(query['capture'])
                got = int(query.get('got', 0))
                total = int(query.get('total', 0))
                buttons_cfg = cfg['button'].get('buttons', [])
                label = 'Scene {}'.format(scene_idx + 1)
                if scene_idx < len(buttons_cfg):
                    label = buttons_cfg[scene_idx].get('label', label)
                if got > 0:
                    w(conn, '<div class="alert alert-success">')
                    w(conn, '<strong>Captured:</strong> {} &mdash; {}/{} universe{} stored.'.format(
                        _html_esc(label), got, total, 's' if total != 1 else ''))
                else:
                    w(conn, '<div class="alert alert-danger">')
                    w(conn, '<strong>Capture failed:</strong> No sACN data received for {}.'.format(_html_esc(label)))
                w(conn, '</div>')
            except Exception:
                pass

        # Nav tabs
        tabs = [
            ('scenes',  'Scenes',  True),
            ('network', 'Network', False),
            ('sacn',    'sACN',    False),
            ('button',  'Buttons', False),
            ('targets', 'Targets', False),
        ]
        w(conn, '<ul class="nav nav-tabs" id="cfgTabs" role="tablist">')
        for tid, tlabel, active in tabs:
            cls = 'nav-link active' if active else 'nav-link'
            w(conn, '<li class="nav-item" role="presentation">')
            w(conn, '<button class="{}" id="{}-tab" data-bs-toggle="tab"'.format(cls, tid))
            w(conn, ' data-bs-target="#{}" type="button" role="tab">{}</button>'.format(tid, tlabel))
            w(conn, '</li>')
        w(conn, '</ul>')
        w(conn, '<div class="tab-content border border-top-0 rounded-bottom bg-white p-4 mb-4">')

        # ---- Scenes tab ----
        w(conn, '<div class="tab-pane fade show active" id="scenes" role="tabpanel">')
        w(conn, '<h5 class="mb-3">Scenes</h5>')
        w(conn, '<p class="text-muted small">Status updates automatically. '
                'Capture listens for incoming sACN for up to 5&nbsp;s and stores it as the selected scene.</p>')
        buttons_cfg = btn.get('buttons', [])
        if buttons_cfg:
            w(conn, '<div class="table-responsive">')
            w(conn, '<table class="table table-sm table-bordered align-middle" id="scenes-table">')
            w(conn, '<thead class="table-dark"><tr>'
                    '<th>#</th><th>Label</th><th>GPIO</th><th>Mode</th>'
                    '<th>Status</th><th>Play</th><th>Capture</th>'
                    '</tr></thead>')
            w(conn, '<tbody>')
            for i, b in enumerate(buttons_cfg):
                lbl = _html_esc(b.get('label', 'Scene {}'.format(i + 1)))
                pin = _html_esc(str(b.get('pin', '?')))
                mode = _html_esc(b.get('mode', ''))
                w(conn, '<tr id="scene-row-{}" >'.format(i))
                w(conn, '<td class="text-center text-muted">{}</td>'.format(i + 1))
                w(conn, '<td>{}</td>'.format(lbl))
                w(conn, '<td class="text-center">{}</td>'.format(pin))
                w(conn, '<td class="text-center">{}</td>'.format(mode))
                w(conn, '<td class="text-center" id="state-{}">'.format(i))
                w(conn, '<span class="badge bg-secondary">Off</span>')
                w(conn, '</td>')
                w(conn, '<td class="text-center">')
                w(conn, '<form method="post" action="/play/{}" style="margin:0">'.format(i))
                w(conn, '<button class="btn btn-sm btn-primary">Play</button>')
                w(conn, '</form>')
                w(conn, '</td>')
                w(conn, '<td class="text-center">')
                w(conn, '<form method="post" action="/capture/{}" style="margin:0">'.format(i))
                w(conn, '<button class="btn btn-sm btn-warning">Capture</button>')
                w(conn, '</form>')
                w(conn, '</td></tr>')
            w(conn, '</tbody></table></div>')
        w(conn, '<form method="post" action="/release" class="mt-3">')
        w(conn, '<button class="btn btn-danger">Release All</button>')
        w(conn, '</form>')
        if not buttons_cfg:
            w(conn, '<p class="text-muted mt-3">No scene buttons configured. Add buttons in the Buttons tab first.</p>')

        # Import / Export
        w(conn, '<div class="d-flex align-items-center gap-3 mt-4 pt-3 border-top flex-wrap">')
        w(conn, '<a href="/export/scenes" class="btn btn-outline-secondary btn-sm">Export scenes.bin</a>')
        w(conn, '<form method="post" action="/import/scenes" enctype="multipart/form-data" '
                'class="d-flex align-items-center gap-2" '
                'onsubmit="return confirm(\'Replace all stored scenes with the imported file?\')">')
        w(conn, '<input type="file" name="file" class="form-control form-control-sm" '
                'accept=".bin" style="max-width:220px" required>')
        w(conn, '<button class="btn btn-outline-secondary btn-sm" type="submit">Import</button>')
        w(conn, '</form>')
        w(conn, '</div>')

        w(conn, '</div>')

        # ---- Network tab ----
        w(conn, '<div class="tab-pane fade" id="network" role="tabpanel">')
        w(conn, '<h5 class="mb-3">Network Configuration</h5>')
        w(conn, '<p class="text-muted small">Changes require a reboot to take effect.</p>')
        w(conn, '<form method="post" action="/save/network">')
        w(conn, '<div class="row g-3">')
        for field, label in (('ip', 'IP Address'), ('subnet', 'Subnet Mask'),
                              ('gateway', 'Gateway'), ('dns', 'DNS Server')):
            val = _html_esc(net.get(field, ''))
            w(conn, '<div class="col-md-6"><label class="form-label fw-semibold">{}</label>'.format(label))
            w(conn, '<input class="form-control" name="{}" value="{}">'.format(field, val))
            w(conn, '</div>')
        w(conn, '</div>')
        w(conn, '<button class="btn btn-primary mt-3">Save Network</button>')
        w(conn, '</form></div>')

        # ---- sACN tab ----
        w(conn, '<div class="tab-pane fade" id="sacn" role="tabpanel">')
        w(conn, '<h5 class="mb-3">sACN Settings</h5>')
        w(conn, '<form method="post" action="/save/sacn">')
        w(conn, '<div class="row g-3">')
        src = _html_esc(sacn.get('source_name', ''))
        w(conn, '<div class="col-md-6"><label class="form-label fw-semibold">Source Name</label>')
        w(conn, '<input class="form-control" name="source_name" maxlength="63" value="{}">'.format(src))
        w(conn, '</div>')
        for field, label, lo, hi in (
            ('priority',       'Priority',       0, 200),
            ('universe_start', 'Universe Start', 1, 63999),
            ('universe_count', 'Universe Count', 1, 512),
        ):
            val = sacn.get(field, 0)
            w(conn, '<div class="col-md-2"><label class="form-label fw-semibold">{}</label>'.format(label))
            w(conn, '<input class="form-control" type="number" name="{}" min="{}" max="{}" value="{}">'.format(
                field, lo, hi, val))
            w(conn, '</div>')
        w(conn, '</div>')
        w(conn, '<button class="btn btn-primary mt-3">Save sACN</button>')
        w(conn, '</form></div>')

        # ---- Button tab ----
        w(conn, '<div class="tab-pane fade" id="button" role="tabpanel">')
        w(conn, '<h5 class="mb-3">Button Configuration</h5>')
        w(conn, '<form method="post" action="/save/button">')

        # Per-button table
        w(conn, '<p class="text-muted small">Each row is one scene button. Scene index = row order (top = scene 1).</p>')
        w(conn, '<div class="table-responsive mb-3">')
        w(conn, '<table class="table table-sm table-bordered align-middle">')
        w(conn, '<thead class="table-dark"><tr>')
        w(conn, '<th>Scene</th><th>Label</th><th>GPIO Pin</th><th>Mode</th><th>Remove</th>')
        w(conn, '</tr></thead><tbody>')

        buttons_cfg = btn.get('buttons', [])
        for i, b in enumerate(buttons_cfg):
            pin_val = _html_esc(str(b.get('pin', '')))
            mode_val = b.get('mode', 'toggle')
            label_val = _html_esc(b.get('label', ''))
            w(conn, '<tr>')
            w(conn, '<td class="text-center text-muted">{}</td>'.format(i + 1))
            w(conn, '<td><input class="form-control form-control-sm" '
                    'name="label_{}" value="{}"></td>'.format(i, label_val))
            w(conn, '<td><input class="form-control form-control-sm" type="number" '
                    'name="pin_{}" min="0" max="28" value="{}"></td>'.format(i, pin_val))
            w(conn, '<td><select class="form-select form-select-sm" name="mode_{}">'
                    .format(i))
            for opt in ('toggle', 'single', 'active'):
                sel = ' selected' if mode_val == opt else ''
                w(conn, '<option value="{}"{}>{}</option>'.format(opt, sel, opt))
            w(conn, '</select></td>')
            w(conn, '<td class="text-center"><input type="checkbox" class="form-check-input" '
                    'name="del_btn_{}"></td>'.format(i))
            w(conn, '</tr>')

        # New button row
        w(conn, '<tr class="table-success">')
        w(conn, '<td class="text-center text-muted small">new</td>')
        w(conn, '<td><input class="form-control form-control-sm" '
                'name="label_new" placeholder="Label"></td>')
        w(conn, '<td><input class="form-control form-control-sm" type="number" '
                'name="pin_new" min="0" max="28" placeholder="GPIO"></td>')
        w(conn, '<td><select class="form-select form-select-sm" name="mode_new">')
        for opt in ('toggle', 'single', 'active'):
            w(conn, '<option value="{}">{}</option>'.format(opt, opt))
        w(conn, '</select></td>')
        w(conn, '<td></td></tr>')
        w(conn, '</tbody></table></div>')

        # All-off button pin
        off_pin = btn.get('off_pin')
        off_pin_val = _html_esc(str(off_pin)) if off_pin is not None else ''
        w(conn, '<div class="row g-3 align-items-end mb-3">')
        w(conn, '<div class="col-md-3"><label class="form-label fw-semibold">All-Off GPIO Pin</label>')
        w(conn, '<input class="form-control" type="number" name="off_pin" min="0" max="28" '
                'value="{}" placeholder="(none)">'.format(off_pin_val))
        w(conn, '<div class="form-text">Leave blank to disable the all-off button.</div>')
        w(conn, '</div></div>')

        w(conn, '<button class="btn btn-primary">Save Buttons</button>')
        w(conn, '</form></div>')

        # ---- Targets tab ----
        multicast_on = cfg['sacn'].get('multicast_enabled', True)
        mcast_btn_cls = 'btn btn-success' if multicast_on else 'btn btn-outline-secondary'
        mcast_btn_txt = 'Multicast ON' if multicast_on else 'Multicast OFF'
        w(conn, '<div class="tab-pane fade" id="targets" role="tabpanel">')
        w(conn, '<h5 class="mb-3">Unicast Target Nodes</h5>')
        w(conn, '<p class="text-muted small">sACN packets are sent unicast to each IP listed here. '
                'Use the toggle to also send multicast.</p>')
        w(conn, '<div class="d-flex align-items-center gap-3 mb-3">')
        w(conn, '<form method="post" action="/toggle/multicast" style="margin:0">')
        w(conn, '<button id="mcast-btn" class="{}">{}</button>'.format(mcast_btn_cls, mcast_btn_txt))
        w(conn, '</form>')
        w(conn, '<span class="text-muted small">Toggle multicast transmission (239.255.x.x:5568)</span>')
        w(conn, '</div>')
        w(conn, '<form method="post" action="/save/targets">')
        w(conn, '<div class="table-responsive">')
        w(conn, '<table class="table table-sm table-bordered align-middle">')
        w(conn, '<thead class="table-dark"><tr>')
        w(conn, '<th>#</th><th>IP Address</th><th>Label</th><th>Remove</th>')
        w(conn, '</tr></thead><tbody>')
        for i, t in enumerate(targets):
            ip_val = _html_esc(t.get('ip', ''))
            lbl_val = _html_esc(t.get('label', ''))
            w(conn, '<tr><td class="text-center text-muted small">{}</td>'.format(i + 1))
            w(conn, '<td><input class="form-control form-control-sm" name="ip_{}" value="{}"></td>'.format(i, ip_val))
            w(conn, '<td><input class="form-control form-control-sm" name="label_{}" value="{}"></td>'.format(i, lbl_val))
            w(conn, '<td class="text-center"><input type="checkbox" class="form-check-input" '
                    'name="del_{}"></td></tr>'.format(i))
        blank = len(targets)
        w(conn, '<tr class="table-success">')
        w(conn, '<td class="text-center text-muted small">new</td>')
        w(conn, '<td><input class="form-control form-control-sm" name="ip_{}" placeholder="10.x.x.x"></td>'.format(blank))
        w(conn, '<td><input class="form-control form-control-sm" name="label_{}" placeholder="Label"></td>'.format(blank))
        w(conn, '<td></td></tr>')
        w(conn, '</tbody></table></div>')
        w(conn, '<button class="btn btn-primary">Save Targets</button>')
        w(conn, '</form></div>')

        w(conn, '</div>')  # tab-content
        w(conn, '<p class="text-muted small text-end">sACN Playback &mdash; Raspberry Pi Pico RP2040</p>')
        w(conn, '</div>')  # container

        w(conn, '<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>')
        w(conn, '<script>')
        w(conn, 'function refreshStates(){')
        w(conn, '  fetch("/status").then(function(r){return r.json();}).then(function(d){')
        w(conn, '    d.scenes.forEach(function(active,i){')
        w(conn, '      var cell=document.getElementById("state-"+i);')
        w(conn, '      var row=document.getElementById("scene-row-"+i);')
        w(conn, '      if(!cell) return;')
        w(conn, '      if(active){')
        w(conn, '        cell.innerHTML=\'<span class="badge bg-success">Active</span>\';')
        w(conn, '        row.classList.add("table-success");')
        w(conn, '        row.classList.remove("table-light");')
        w(conn, '      } else {')
        w(conn, '        cell.innerHTML=\'<span class="badge bg-secondary">Off</span>\';')
        w(conn, '        row.classList.remove("table-success");')
        w(conn, '      }')
        w(conn, '    });')
        w(conn, '    var mb=document.getElementById("mcast-btn");')
        w(conn, '    if(mb){')
        w(conn, '      if(d.multicast){')
        w(conn, '        mb.textContent="Multicast ON";')
        w(conn, '        mb.className="btn btn-success";')
        w(conn, '      } else {')
        w(conn, '        mb.textContent="Multicast OFF";')
        w(conn, '        mb.className="btn btn-outline-secondary";')
        w(conn, '      }')
        w(conn, '    }')
        w(conn, '  }).catch(function(){});')
        w(conn, '}')
        w(conn, 'refreshStates();')
        w(conn, 'setInterval(refreshStates, 1500);')
        w(conn, '</script>')
        w(conn, '</body></html>')
