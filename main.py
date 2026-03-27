"""
sACN Playback Controller for Raspberry Pi Pico RP2040
----------------------------------------------------
Reads all runtime configuration from config.json via ConfigManager (web_config.py).
The HTTP configuration server runs on core 1 automatically.

Main Components:
- SceneStorage: Binary storage/retrieval of DMX scenes
- PlaybackController: sACN packet creation, sending, and scene playback
- button_loop_multi_pin: GPIO button loop for scene control
"""

import struct
import _thread
from machine import Pin, SPI
import uos
import utime as time
import network
import usocket as socket

try:
    import ujson as json
except ImportError:
    import json

from web_config import ConfigManager, WebConfigServer

# W5500 Ethernet configuration
MISO = 16
CS = 17
SCK = 18
MOSI = 19
RESET = 20

# --- Storage Configuration ---
SCENES_FILE = 'scenes.bin'
SCENE_COUNT = 8  # maximum number of storable scenes
_SCENE_MAGIC = b'SCNC'  # identifies compressed scene file format


class SceneStorage:
    """
    Store DMX scenes in a compact binary file.

    Format:
      uint16 number_of_universes
      uint16 * universes list
      SCENE_COUNT * (num_universes * 512) bytes scene data
    """
    def __init__(self, path=SCENES_FILE, universes=None):
        self.path = path
        self.universes = sorted(universes) if universes else list(range(1, 21))
        self.scene_bytes = len(self.universes) * 512
        self.scenes = [self._empty_scene() for _ in range(SCENE_COUNT)]
        self.load()
        print('SceneStorage: {} scenes, universes: {}'.format(len(self.scenes), self.universes))

    def _empty_scene(self):
        return {u: bytes(512) for u in self.universes}

    def _write_binary(self, path):
        """Write scenes in compressed format (SCNC): only non-empty universes stored per scene."""
        tmp = path + '.tmp'
        empty = bytes(512)
        try:
            with open(tmp, 'wb') as f:
                f.write(_SCENE_MAGIC)
                f.write(struct.pack('!H', len(self.universes)))
                for u in self.universes:
                    f.write(struct.pack('!H', u))
                for scene in self.scenes:
                    # Collect non-empty universes for this scene
                    non_empty = []
                    for u in self.universes:
                        d = scene.get(u, empty)
                        if not isinstance(d, (bytes, bytearray)):
                            d = bytes(d)
                        if len(d) < 512:
                            d = d + bytes(512 - len(d))
                        elif len(d) > 512:
                            d = d[:512]
                        if d != empty:
                            non_empty.append((u, d))
                    f.write(struct.pack('!H', len(non_empty)))
                    for u, d in non_empty:
                        f.write(struct.pack('!H', u))
                        f.write(d)
            try:
                uos.remove(path)
            except Exception:
                pass
            uos.rename(tmp, path)
        except Exception as e:
            print('SceneStorage: save failed:', e)
            try:
                uos.remove(tmp)
            except Exception:
                pass
        print('SceneStorage: saved to', path)

    def _load_uncompressed(self, f):
        """Load legacy uncompressed format (all universes stored for every scene)."""
        hdr = f.read(2)
        if len(hdr) < 2:
            raise ValueError('file too small')
        n = struct.unpack('!H', hdr)[0]
        univ_bytes = f.read(2 * n)
        if len(univ_bytes) < 2 * n:
            raise ValueError('incomplete universe list')
        universes = [struct.unpack('!H', univ_bytes[2*i:2*i+2])[0] for i in range(n)]
        self.universes = universes
        self.scene_bytes = len(self.universes) * 512
        self.scenes = [self._empty_scene() for _ in range(SCENE_COUNT)]
        for si in range(SCENE_COUNT):
            sc = {}
            truncated = False
            for u in self.universes:
                dmx = f.read(512)
                if len(dmx) < 512:
                    truncated = True
                    break
                sc[u] = dmx
            if truncated:
                break
            self.scenes[si] = sc

    def _load_compressed(self, f):
        """Load compressed SCNC format (only non-empty universes per scene)."""
        hdr = f.read(2)
        if len(hdr) < 2:
            raise ValueError('file too small')
        n = struct.unpack('!H', hdr)[0]
        univ_bytes = f.read(2 * n)
        if len(univ_bytes) < 2 * n:
            raise ValueError('incomplete universe list')
        universes = [struct.unpack('!H', univ_bytes[2*i:2*i+2])[0] for i in range(n)]
        self.universes = universes
        self.scene_bytes = len(self.universes) * 512
        self.scenes = [self._empty_scene() for _ in range(SCENE_COUNT)]
        for si in range(SCENE_COUNT):
            m_bytes = f.read(2)
            if len(m_bytes) < 2:
                break
            m = struct.unpack('!H', m_bytes)[0]
            sc = self._empty_scene()
            for _ in range(m):
                u_bytes = f.read(2)
                if len(u_bytes) < 2:
                    break
                u = struct.unpack('!H', u_bytes)[0]
                dmx = f.read(512)
                if len(dmx) < 512:
                    break
                if u in sc:
                    sc[u] = dmx
            self.scenes[si] = sc

    def load(self):
        """Detect file format (SCNC compressed or legacy) and load accordingly."""
        try:
            try:
                uos.stat(self.path)
            except OSError:
                return
            with open(self.path, 'rb') as f:
                magic = f.read(4)
                if len(magic) < 4:
                    return
                if magic == _SCENE_MAGIC:
                    self._load_compressed(f)
                else:
                    f.seek(0)
                    self._load_uncompressed(f)
        except Exception as e:
            print('SceneStorage: load failed, using empty scenes:', e)

    def save(self):
        self._write_binary(self.path)

    def get(self, index):
        return self.scenes[index]

    def set(self, index, scene):
        self.scenes[index] = scene
        self.save()


class PlaybackController:
    """
    Manages DMX scene playback and sACN (E1.31) packet transmission.
    """
    def __init__(self, universes, playback_priority=0,
                 source_name='PlaybackScript', bind_address='',
                 send_targets=None, multicast_enabled=True):
        self.universes = universes
        self.playback_priority = playback_priority
        self.source_name = source_name
        self.bind_address = bind_address
        self.send_targets = send_targets or []
        self.multicast_enabled = multicast_enabled

        import ubinascii
        import machine
        raw_id = machine.unique_id()
        self.cid = raw_id + bytes(max(0, 16 - len(raw_id)))
        print('CID:', ubinascii.hexlify(self.cid).decode())

        self._seq = {}
        self.e131_sock = None
        self.recv_sock = None
        self._init_socket()

        self.storage = SceneStorage(universes=self.universes)
        self.current_scene = 0
        self.scene_active = []   # bool per scene; set by button loop and web server
        self.scene_expiry = []   # float per scene; >0 means stop playing at that time() (single mode)
        self.capturing = False   # True while capture_current_into_scene is running

    def _init_socket(self):
        try:
            self.e131_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.e131_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            print('Send socket created')

            self.recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.recv_sock.bind(('0.0.0.0', 5568))
                self.recv_sock.settimeout(0.1)
                print('Recv socket listening on 0.0.0.0:5568')
            except OSError as e:
                print('Failed to bind recv socket to 5568:', e)
                self.recv_sock.bind(('0.0.0.0', 5569))
                self.recv_sock.settimeout(0.1)
                print('Recv socket listening on 0.0.0.0:5569')
            try:
                # Attempt to increase receive buffer so rapid bursts don't overflow.
                # SO_RCVBUF = 8; may be a no-op on WIZNET driver.
                self.recv_sock.setsockopt(socket.SOL_SOCKET, 8, 8192)
                print('SO_RCVBUF set to 8192')
            except Exception as e:
                print('SO_RCVBUF not supported:', e)
        except Exception as e:
            print('Failed to create socket:', e)
            self.e131_sock = None
            self.recv_sock = None



    def play_scene(self, index):
        """Send all universes of a stored scene."""
        if self.capturing:
            return
        scene = self.storage.get(index)
        for u in self.universes:
            payload = scene.get(u, bytes(512))
            if not isinstance(payload, bytes):
                payload = bytes(payload)
            if self.e131_sock is not None:
                try:
                    self._send_e131(u, payload, self.playback_priority)
                except Exception as e:
                    print('Failed to send universe {}: {}'.format(u, e))

    def play_scene_universe(self, index, universe):
        """Send a single universe from a stored scene."""
        if universe not in self.universes:
            return False
        scene = self.storage.get(index)
        payload = scene.get(universe, bytes(512))
        if not isinstance(payload, bytes):
            payload = bytes(payload)
        if self.e131_sock is not None:
            try:
                self._send_e131(universe, payload, self.playback_priority)
                return True
            except Exception as e:
                print('Failed to send universe {}: {}'.format(universe, e))
        return False

    def send_zeros(self, universes=None):
        """Send all-zero DMX to the given universes (or all configured universes)."""
        zeros = bytes(512)
        for u in (universes or self.universes):
            try:
                self._send_e131(u, zeros, self.playback_priority)
            except Exception:
                pass

    def _parse_e131_packet(self, data):
        """Parse incoming E1.31 packet.

        Returns (dmx_data, universe, priority) or (None, None, None).

        Framing layer layout (relative to framing_start=38):
          0-1   flags+len
          2-5   vector
          6-69  source name (64 bytes)
          70    priority
          71-72 reserved
          73    sequence
          74    options
          75-76 universe
          77+   DMP layer
        """
        try:
            if len(data) < 126:
                return None, None, None
            if data[4:16] != b'ASC-E1.17\x00\x00\x00':
                return None, None, None
            framing_start = 38
            priority_offset = framing_start + 70
            universe_offset = framing_start + 75
            if universe_offset + 2 > len(data):
                return None, None, None
            priority = data[priority_offset]
            universe = struct.unpack('!H', data[universe_offset:universe_offset + 2])[0]
            dmp_start = framing_start + 77
            if dmp_start + 11 > len(data):
                return None, None, None
            dmx_start = dmp_start + 11  # skip DMP header (10 bytes) + start code (1 byte)
            if dmx_start + 512 > len(data):
                available = len(data) - dmx_start
                if available > 0:
                    dmx_data = data[dmx_start:dmx_start + available] + bytes(512 - available)
                else:
                    return None, None, None
            else:
                dmx_data = data[dmx_start:dmx_start + 512]
            return dmx_data, universe, priority
        except Exception as e:
            print('Parse E1.31 error:', e)
            return None, None, None

    def _build_e131_packet(self, universe, dmx, sequence, priority):
        """Build a complete E1.31/sACN UDP packet."""
        if len(dmx) < 512:
            dmx = dmx + bytes(512 - len(dmx))
        else:
            dmx = dmx[:512]

        prop_values = bytes([0x00]) + dmx
        prop_count = len(prop_values)
        dmp_body = bytes([0x02, 0xA1]) + struct.pack('!HHH', 0x0000, 0x0001, prop_count) + prop_values
        dmp = struct.pack('!H', 0x7000 | (2 + len(dmp_body))) + dmp_body

        src_name = self.source_name.encode('utf-8')[:64]
        src_name = src_name + bytes(64 - len(src_name))
        framing_body = (struct.pack('!I', 0x00000002) + src_name +
                        struct.pack('!BHBBH', int(priority) & 0xFF, 0, int(sequence) & 0xFF, 0, int(universe) & 0xFFFF) +
                        dmp)
        framing = struct.pack('!H', 0x7000 | (2 + len(framing_body))) + framing_body

        root_body = struct.pack('!I', 0x00000004) + self.cid + framing
        pkt = (struct.pack('!HH', 0x0010, 0x0000) + b'ASC-E1.17\x00\x00\x00' +
               struct.pack('!H', 0x7000 | (2 + len(root_body))) + root_body)
        return pkt

    def _sendto(self, pkt, addr):
        """Send one UDP packet with retry on ENOMEM (W5500 TX buffer full).

        The W5500 has a 16 KB shared TX buffer. Blasting many back-to-back
        packets (20 universes × N targets) can exhaust it faster than the
        chip drains it. On ENOMEM we wait for the buffer to drain and retry.
        Returns True on success.
        """
        for wait_ms in (0, 5, 15, 40):
            if wait_ms:
                time.sleep_ms(wait_ms)
            try:
                self.e131_sock.sendto(pkt, addr)
                return True
            except OSError as e:
                if e.args[0] != 12:  # 12 = ENOMEM
                    print('Send error to {}: {}'.format(addr[0], e))
                    return False
                # ENOMEM — loop and retry after the wait
        print('Send failed (TX buffer full): {}'.format(addr[0]))
        return False

    def _send_e131(self, universe, dmx, priority=0):
        """Send an E1.31 packet to multicast and all unicast targets."""
        seq = (self._seq.get(universe, 0) + 1) & 0xFF
        self._seq[universe] = seq
        pkt = self._build_e131_packet(universe, dmx, seq, priority)
        mcast = '239.255.{}.{}'.format((universe >> 8) & 0xFF, universe & 0xFF)
        if self.e131_sock is None:
            return
        if self.multicast_enabled:
            self._sendto(pkt, (mcast, 5568))
        for ip in self.send_targets:
            self._sendto(pkt, (ip, 5568))

    def capture_current_into_scene(self, scene_idx):
        """Capture DMX data for all universes into the given scene slot, only if priority > 0.

        Blocks until every configured universe has been received at least once.
        Returns (universes_captured, universes_total).
        """
        if self.capturing:
            print('Capture already in progress')
            return 0, len(self.universes)
        self.capturing = True
        try:
            captured = {}
            received = set()
            needed = set(self.universes)
            while received < needed:
                try:
                    data, _ = self.recv_sock.recvfrom(1500)
                    dmx, universe, priority = self._parse_e131_packet(data)
                    if (
                        dmx is not None and universe in needed
                        and priority is not None and priority > 0
                    ):
                        if universe not in received:
                            print('Captured universe {} with priority {}'.format(universe, priority))
                            captured[universe] = dmx
                            received.add(universe)
                except OSError as e:
                    if e.args[0] != 110:  # 110 = ETIMEDOUT (timeout)
                        print('Error receiving DMX data:', e)
            self.storage.set(scene_idx, captured)
            n = len(received)
            print('Captured scene {}: {}/{} universes'.format(scene_idx, n, len(self.universes)))
            return n, len(self.universes)
        finally:
            self.capturing = False
    def stop(self):
        try:
            if self.e131_sock:
                self.e131_sock.close()
            if self.recv_sock:
                self.recv_sock.close()
        except Exception as e:
            print('Error closing sockets:', e)


def setup_w5500(net_cfg):
    """
    Initialize WIZNET6K Ethernet interface.
    Args:
        net_cfg: dict with keys ip, subnet, gateway, dns
    Returns w5500 instance or None.
    """
    try:
        w5500 = network.WIZNET6K()  # type: ignore
        w5500.active(True)
        w5500.ifconfig((net_cfg['ip'], net_cfg['subnet'], net_cfg['gateway'], net_cfg['dns']))
        print('W5500 IP:', w5500.ifconfig())
        return w5500
    except Exception as e:
        print('Failed to initialize W5500:', e)
        return None


SINGLE_SHOT_DURATION = 10.0  # seconds


def button_loop_multi_pin(controller, scene_pins, button_modes=None, off_pin=None, verbose=False):
    """
    GPIO button loop for scene playback and capture.

    Args:
        controller: PlaybackController instance
        scene_pins: list of GPIO pin numbers, one per scene (index -> scene index)
        button_modes: list of modes per scene button: 'toggle', 'single', 'active'
        off_pin: optional GPIO pin for all-off button (sends zeros to all universes)
        verbose: enable debug output

    Button modes:
        'toggle'  — press to toggle scene on/off
        'single'  — press plays scene for SINGLE_SHOT_DURATION seconds, then stops
        'active'  — scene plays while button is held, stops on release

    State is written to controller.scene_active[] and controller.scene_expiry[] so the
    web server can read and modify playback state directly.
    """
    if button_modes is None:
        button_modes = ['toggle'] * len(scene_pins)

    n_scenes = len(scene_pins)
    all_pins = list(scene_pins)
    if off_pin is not None:
        all_pins.append(off_pin)

    buttons = [Pin(p, Pin.IN, Pin.PULL_UP) for p in all_pins]
    last_states = [1] * len(all_pins)
    send_interval = 0.5  # 2 Hz
    last_send_time = 0.0

    controller.scene_active = [False] * n_scenes
    controller.scene_expiry = [0.0] * n_scenes  # 0 = no expiry; >0 = stop at this time()

    while True:
        now = time.time()

        # --- Input: update scene_active / scene_expiry based on button presses ---
        for idx, btn in enumerate(buttons):
            state = btn.value()
            is_off_btn = (idx >= n_scenes)

            if is_off_btn:
                if state == 1 and last_states[idx] == 0:
                    # All-off button released: zero all universes and deactivate everything
                    controller.send_zeros()
                    for i in range(n_scenes):
                        controller.scene_active[i] = False
                        controller.scene_expiry[i] = 0.0
                    if verbose:
                        print('All-Off triggered')
                    for i in range(len(last_states)):
                        last_states[i] = 1
                last_states[idx] = state
                continue

            mode = button_modes[idx] if idx < len(button_modes) else 'toggle'

            toggle_locked = any(
                (button_modes[i] if i < len(button_modes) else 'toggle') == 'toggle'
                and controller.scene_active[i]
                for i in range(n_scenes)
            )

            if mode == 'toggle':
                if state == 0 and last_states[idx] == 1:
                    controller.scene_active[idx] = not controller.scene_active[idx]
                    if controller.scene_active[idx]:
                        # Deactivate all other toggle scenes
                        for i in range(n_scenes):
                            if i != idx and (button_modes[i] if i < len(button_modes) else 'toggle') == 'toggle' and controller.scene_active[i]:
                                controller.send_zeros(list(controller.storage.get(i).keys()))
                                controller.scene_active[i] = False
                        controller.play_scene(idx)  # immediate response on activation
                        if verbose:
                            print('Scene {} ON'.format(idx))
                    else:
                        controller.send_zeros(list(controller.storage.get(idx).keys()))
                        if verbose:
                            print('Scene {} OFF'.format(idx))
                last_states[idx] = state

            elif mode == 'single':
                if state == 0 and last_states[idx] == 1:
                    controller.scene_active[idx] = True
                    controller.scene_expiry[idx] = now + SINGLE_SHOT_DURATION
                    controller.play_scene(idx)  # immediate response
                    if verbose:
                        print('Single-shot scene {}'.format(idx))
                last_states[idx] = state

            elif mode == 'active':
                if toggle_locked:
                    # Release any held active scene and block new presses while a toggle is on
                    if controller.scene_active[idx]:
                        controller.send_zeros(list(controller.storage.get(idx).keys()))
                        controller.scene_active[idx] = False
                    last_states[idx] = 1
                    continue
                if state == 0 and last_states[idx] == 1:
                    controller.scene_active[idx] = True
                    controller.play_scene(idx)  # immediate response
                    if verbose:
                        print('Active btn {} ON'.format(idx))
                elif state == 1 and last_states[idx] == 0:
                    if controller.scene_active[idx]:
                        controller.send_zeros(list(controller.storage.get(idx).keys()))
                        controller.scene_active[idx] = False
                        if verbose:
                            print('Active btn {} OFF'.format(idx))
                last_states[idx] = state

        # --- Output: resend active scenes at 2 Hz; expire single-shot timers ---
        if now - last_send_time >= send_interval:
            last_send_time = now
            for si in range(n_scenes):
                if not controller.scene_active[si]:
                    continue
                exp = controller.scene_expiry[si]
                if exp > 0 and now >= exp:
                    controller.send_zeros(list(controller.storage.get(si).keys()))
                    controller.scene_active[si] = False
                    controller.scene_expiry[si] = 0.0
                    if verbose:
                        print('Single-shot scene {} expired'.format(si))
                else:
                    controller.play_scene(si)

        controller.recording_enabled = not any(
            (button_modes[i] if i < len(button_modes) else 'toggle') == 'active'
            and controller.scene_active[i]
            for i in range(n_scenes)
        )

        time.sleep(0.05)


def _configure_w5500_buffers():
    """Write Sn_RXBUF_SIZE / Sn_TXBUF_SIZE registers directly via SPI.

    The W5500 has 16 KB each for RX and TX, split across 8 socket slots
    (2 KB default). A burst of 8 E1.31 packets is ~5.1 KB — larger than
    the 2 KB default, so packets 5-8 are dropped in hardware before
    MicroPython can read them.

    Expected socket slot assignment:
      slot 0 — web server TCP socket  (opened first on core 1)
      slot 1 — e131_sock UDP send     (opened in PlaybackController._init_socket)
      slot 2 — recv_sock UDP receive  (opened in PlaybackController._init_socket)

    Must be called after WIZNET6K() is initialised but before sockets are bound.
    """
    spi = SPI(0, baudrate=42_000_000, polarity=0, phase=0,
              sck=Pin(SCK), mosi=Pin(MOSI), miso=Pin(MISO))
    cs = Pin(CS, Pin.OUT, value=1)

    # RX sizes in KB per socket slot 0-7; sum must be ≤ 16
    # Slot 2 (recv_sock) gets 8 KB to absorb a full 8-universe burst
    rx_kb = [2, 2, 8, 1, 1, 1, 1, 0]  # sum = 16
    # TX sizes: slot 1 (e131_sock) gets more headroom when blasting universes
    tx_kb = [2, 8, 2, 1, 1, 1, 1, 0]  # sum = 16

    for n in range(8):
        bsb = n * 4 + 1            # block-select bits for socket n register block
        ctrl = (bsb << 3) | 0x04  # write, variable-length mode

        cs.value(0)
        spi.write(bytes([0x00, 0x1E, ctrl, rx_kb[n]]))  # Sn_RXBUF_SIZE
        cs.value(1)

        cs.value(0)
        spi.write(bytes([0x00, 0x1F, ctrl, tx_kb[n]]))  # Sn_TXBUF_SIZE
        cs.value(1)

    print('W5500 socket buffers configured: RX={} TX={}'.format(rx_kb, tx_kb))


def main():
    print('sACN Playback Controller — Raspberry Pi Pico RP2040')
    print('=' * 50)

    # Load configuration
    cfg = ConfigManager()

    # Start HTTP config server on core 1
    server = WebConfigServer(cfg)
    _thread.start_new_thread(server.start, ())
    print('Web config server started on core 1')

    # Initialise ethernet
    net_cfg = cfg.get('network')
    w5500 = setup_w5500(net_cfg)
    if w5500 is None:
        print('Error: W5500 init failed')
        return
    _configure_w5500_buffers()

    # Create playback controller from config
    controller = PlaybackController(
        universes=cfg.universes(),
        playback_priority=cfg.get('sacn', 'priority'),
        source_name=cfg.get('sacn', 'source_name'),
        bind_address=net_cfg['ip'],
        send_targets=cfg.target_ips(),
        multicast_enabled=cfg.get('sacn', 'multicast_enabled'),
    )
    print('PlaybackController initialised')
    server.set_controller(controller)

    # Read button configuration
    scene_pins = cfg.button_pins()
    scene_modes = cfg.button_modes()
    off_pin = cfg.button_off_pin()
    print('Buttons: pins={}, modes={}, off_pin={}'.format(scene_pins, scene_modes, off_pin))
    
    button_loop_multi_pin(
            controller,
            scene_pins=scene_pins,
            button_modes=scene_modes,
            off_pin=off_pin,
        verbose=True,
    )


if __name__ == '__main__':
    main()
