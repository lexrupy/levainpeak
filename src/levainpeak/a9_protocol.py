"""Protocolo local (pprpc) das câmeras A9/Linklemo, como a AUSEK AS-A8: sessão cifrada → JPEGs.

Portado de a9test/a9_tk_viewer.py (protocolo documentado publicamente: fusetim, zkiiito/A9, cheap-shot).
Para câmeras que você possui. Credenciais (DID e segredo) são do aparelho: ficam em camera.ini, nunca no git.
"""
from __future__ import annotations

import hashlib
import socket
import struct
import threading
import time


# ---------------------------------------------------------------------------
# Prekey compartilhada (hardcoded no firmware/app de todas as unidades).
# Fonte pública: cheap-shot docs/findings.md @ 0x139194; fusetim writeup.
# ---------------------------------------------------------------------------
PREKEY = b"A2r0i1m1a2M0a1x6toriQue"

PORT_DEFAULT = 20190
CMD_HELLO = 106
CMD_PING = 107
CMD_VIDEOPLAY = 2610
CMD_LANAUTH = 2650
HB_CONST = 18446744073709551586
# The camera ends the video after ~350 frames (~36 s) but keeps answering pings; asking again in the same
# session does nothing, only a new session brings it back. So: no video for this long → reconnect.
STALL_SECONDS = 2.5
SESSION_FRAMES = 320  # renew the session a little before that cut-off (it came at 347-348 frames every time)


# ---------------------------------------------------------------------------
# AES-CBC pure-Python (FIPS-197), stdlib only — fallback sem pycryptodome
# ---------------------------------------------------------------------------
_sbox = []
_inv_sbox = [0] * 256


def _init_sbox():
    p = q = 1
    sbox = [0] * 256
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        xformed = (
            q
            ^ ((q << 1) | (q >> 7))
            ^ ((q << 2) | (q >> 6))
            ^ ((q << 3) | (q >> 5))
            ^ ((q << 4) | (q >> 4))
        )
        sbox[p] = (xformed ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_sbox = _init_sbox()
for i, v in enumerate(_sbox):
    _inv_sbox[v] = i
_rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D]


def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1)


def _mul(a, b):
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return r


def _key_expansion(key):
    Nk = len(key) // 4
    Nr = {4: 10, 6: 12, 8: 14}[Nk]
    w = [list(key[4 * i : 4 * i + 4]) for i in range(Nk)]
    for i in range(Nk, 4 * (Nr + 1)):
        t = list(w[i - 1])
        if i % Nk == 0:
            t = t[1:] + t[:1]
            t = [_sbox[b] for b in t]
            t[0] ^= _rcon[i // Nk - 1]
        elif Nk > 6 and i % Nk == 4:
            t = [_sbox[b] for b in t]
        w.append([w[i - Nk][j] ^ t[j] for j in range(4)])
    return w, Nr


def _add_round_key(s, w, rnd):
    for c in range(4):
        for r in range(4):
            s[r][c] ^= w[rnd * 4 + c][r]


def _bytes_to_state(b):
    return [[b[r + 4 * c] for c in range(4)] for r in range(4)]


def _state_to_bytes(s):
    return bytes(s[r][c] for c in range(4) for r in range(4))


def _encrypt_block(block, w, Nr):
    s = _bytes_to_state(block)
    _add_round_key(s, w, 0)
    for rnd in range(1, Nr):
        s = [[_sbox[s[r][c]] for c in range(4)] for r in range(4)]
        s = [s[r][r:] + s[r][:r] for r in range(4)]
        for c in range(4):
            a = [s[r][c] for r in range(4)]
            s[0][c] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
            s[1][c] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
            s[2][c] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
            s[3][c] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)
        _add_round_key(s, w, rnd)
    s = [[_sbox[s[r][c]] for c in range(4)] for r in range(4)]
    s = [s[r][r:] + s[r][:r] for r in range(4)]
    _add_round_key(s, w, Nr)
    return _state_to_bytes(s)


def _decrypt_block(block, w, Nr):
    s = _bytes_to_state(block)
    _add_round_key(s, w, Nr)
    for rnd in range(Nr - 1, 0, -1):
        s = [s[r][-r:] + s[r][:-r] if r else s[r] for r in range(4)]  # InvShiftRows
        s = [[_inv_sbox[s[r][c]] for c in range(4)] for r in range(4)]
        _add_round_key(s, w, rnd)
        for c in range(4):  # InvMixColumns
            a = [s[r][c] for r in range(4)]
            s[0][c] = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
            s[1][c] = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
            s[2][c] = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
            s[3][c] = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)
    s = [s[r][-r:] + s[r][:-r] if r else s[r] for r in range(4)]
    s = [[_inv_sbox[s[r][c]] for c in range(4)] for r in range(4)]
    _add_round_key(s, w, 0)
    return _state_to_bytes(s)


class AESCBC:
    def __init__(self, key, iv):
        self.w, self.Nr = _key_expansion(key)
        self.iv = bytes(iv)

    def encrypt(self, data):
        assert len(data) % 16 == 0
        out = bytearray()
        prev = self.iv
        for i in range(0, len(data), 16):
            blk = bytes(a ^ b for a, b in zip(data[i : i + 16], prev))
            prev = _encrypt_block(blk, self.w, self.Nr)
            out += prev
        return bytes(out)

    def decrypt(self, data):
        assert len(data) % 16 == 0
        out = bytearray()
        prev = self.iv
        for i in range(0, len(data), 16):
            ct = data[i : i + 16]
            dec = _decrypt_block(ct, self.w, self.Nr)
            out += bytes(a ^ b for a, b in zip(dec, prev))
            prev = ct
        return bytes(out)


def aes_cbc_encrypt(key, iv, data):
    try:
        from Crypto.Cipher import AES  # type: ignore

        return AES.new(key, AES.MODE_CBC, iv).encrypt(data)
    except Exception:
        return AESCBC(key, iv).encrypt(data)


def aes_cbc_decrypt(key, iv, data):
    try:
        from Crypto.Cipher import AES  # type: ignore

        return AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    except Exception:
        return AESCBC(key, iv).decrypt(data)


# ---------------------------------------------------------------------------
# varint / protobuf / CTRL / MEDIA
# ---------------------------------------------------------------------------
def enc_varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def dec_varint(buf, i):
    shift = val = 0
    while i < len(buf):
        x = buf[i]
        i += 1
        val |= (x & 0x7F) << shift
        if not (x & 0x80):
            return val, i
        shift += 7
    raise ValueError("truncated varint")


def pb_parse(buf):
    out = {}
    i = 0
    n = len(buf)
    while i < n:
        try:
            tag, i = dec_varint(buf, i)
        except ValueError:
            break
        f, wt = tag >> 3, tag & 7
        try:
            if wt == 0:
                v, i = dec_varint(buf, i)
            elif wt == 2:
                ln, i = dec_varint(buf, i)
                if i + ln > n:
                    break
                v = buf[i : i + ln]
                i += ln
            elif wt == 5:
                if i + 4 > n:
                    break
                v = struct.unpack("<I", buf[i : i + 4])[0]
                i += 4
            elif wt == 1:
                if i + 8 > n:
                    break
                v = struct.unpack("<Q", buf[i : i + 8])[0]
                i += 8
            else:
                break
        except Exception:
            break
        out.setdefault(f, []).append(v)
    return out


def pb_field_bytes(field, data):
    return enc_varint((field << 3) | 2) + enc_varint(len(data)) + data


def pb_varint_field(field, value):
    return enc_varint((field << 3) | 0) + enc_varint(value)


def ctrl_key(cmd, seq, rpc):
    return hashlib.md5(b"%s,ID:%d-SEQ:%d-RPC:%d" % (PREKEY, cmd, seq, rpc)).hexdigest().encode()


def ctrl_encrypt(cmd, seq, rpc, plaintext):
    if not plaintext:
        return b""
    key = ctrl_key(cmd, seq, rpc)
    pad = 16 - (len(plaintext) % 16)
    return aes_cbc_encrypt(key, key[:16], plaintext + bytes([pad]) * pad)


def ctrl_decrypt(cmd, seq, rpc, ct):
    if len(ct) == 0 or len(ct) % 16:
        return None
    key = ctrl_key(cmd, seq, rpc)
    pt = aes_cbc_decrypt(key, key[:16], ct)
    pad = pt[-1]
    return pt[:-pad] if 1 <= pad <= 16 and pt.endswith(bytes([pad]) * pad) else None


def build_ctrl(seq, cmd, plaintext=b"", flag=0x0C):
    rpc = flag & 3
    payload = ctrl_encrypt(cmd, seq, rpc, plaintext)
    exhdr = enc_varint(seq) + enc_varint(cmd) + bytes([flag])
    if rpc == 1:
        exhdr += enc_varint(0)
    body = exhdr + payload
    return bytes([0x48]) + enc_varint(len(body)) + body


def ctrl_exhdr(body):
    p = 0
    seq, p = dec_varint(body, p)
    cmd, p = dec_varint(body, p)
    flag = body[p]
    p += 1
    rpc = flag & 3
    if rpc == 1:
        _, p = dec_varint(body, p)
    return cmd, seq, rpc, body[p:]


def media_exhdr(body):
    if len(body) < 2:
        raise ValueError("truncated MEDIA header")
    flag = body[0] >> 7
    p = 2
    vs = []
    for _ in range(5):
        v, p = dec_varint(body, p)
        vs.append(v)
    if p + 2 > len(body):
        raise ValueError("truncated MEDIA fragment marker")
    p += 2
    return flag, vs[0], vs[1], vs[2], vs[3], body[p:]


def decrypt_media_frame(frags, session_key):
    blob = b""
    for body in frags:
        flag, avch, avseq, tt, enclen, data = media_exhdr(body)
        if enclen > 0:
            key = hashlib.md5(
                b"%s,AVSeq:%d-TT:%d-AVChannel:%d" % (session_key, avseq, tt, avch)
            ).hexdigest().encode()
            n = min(enclen - (enclen % 16), len(data) - (len(data) % 16))
            blob += aes_cbc_decrypt(key, key[16:32], data[:n]) + data[n:]
        else:
            blob += data
    soi = blob.find(b"\xff\xd8")
    eoi = blob.find(b"\xff\xd9", soi + 2) if soi >= 0 else -1
    return blob[soi : eoi + 2] if (soi >= 0 and eoi >= 0) else None


class MediaFragments(list):
    MAX_FRAGS = 4096
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self):
        super().__init__()
        self.byte_count = 0

    def append(self, body):
        if len(self) >= self.MAX_FRAGS or self.byte_count + len(body) > self.MAX_BYTES:
            self.clear()
            raise ValueError("MEDIA frame exceeded limit")
        super().append(body)
        self.byte_count += len(body)

    def clear(self):
        super().clear()
        self.byte_count = 0


class FrameReader:
    MAX_FRAME = 8 * 1024 * 1024
    MAX_BUF = 24 * 1024 * 1024

    def __init__(self, sock):
        self.s = sock
        self.buf = b""

    def _fill(self, timeout=5.0):
        self.s.settimeout(timeout)
        chunk = self.s.recv(65536)
        if not chunk:
            raise ConnectionError("camera closed connection")
        self.buf += chunk
        if len(self.buf) > self.MAX_BUF:
            raise ConnectionError("receive buffer exceeded limit")

    def next_frame(self, timeout=5.0):
        deadline = time.time() + timeout
        while True:
            b = self.buf
            if b:
                ctrl = b[0]
                msgtype = (ctrl >> 4) & 0xF
                if msgtype in (2, 3, 4, 5, 6, 7, 8):
                    try:
                        remain, j = dec_varint(b, 1)
                        if remain > self.MAX_FRAME:
                            raise ConnectionError("frame length over limit")
                        if j + remain <= len(b):
                            body = b[j : j + remain]
                            self.buf = b[j + remain :]
                            return msgtype, ctrl & 0xF, body
                    except ValueError:
                        pass
                else:
                    self.buf = b[1:]
                    continue
            if time.time() > deadline:
                raise TimeoutError("no complete frame within timeout")
            self._fill(max(0.2, deadline - time.time()))


# ---------------------------------------------------------------------------
# Config (.env)
# ---------------------------------------------------------------------------
# Worker: sessão pprpc contínua → último JPEG
# ---------------------------------------------------------------------------
class CameraWorker(threading.Thread):
    def __init__(self, ip, port, did, secret, state: dict, lock: threading.Lock):
        super().__init__(daemon=True)
        self.ip = ip
        self.port = port
        self.did = did.encode() if isinstance(did, str) else did
        self.secret = secret.encode() if isinstance(secret, str) else secret
        self.state = state
        self.lock = lock
        self._stop_event = threading.Event()
        self._fast_retry = False
        self._sock = None

    def stop(self):
        self._stop_event.set()
        sock = self._sock
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def run(self):
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as e:
                self._set(connected=False, status=f"erro: {e}")
            if self._stop_event.is_set():
                break
            fast, self._fast_retry = self._fast_retry, False
            wait = 0.2 if fast else backoff
            self._set(connected=False, status="reconectando…" if fast else f"reconectando em {wait:.0f}s…")
            if self._stop_event.wait(wait):
                break
            backoff = 1.0 if fast else min(backoff * 1.5, 8.0)

    def _session(self):
        self._set(connected=False, status="conectando…")
        s = socket.create_connection((self.ip, self.port), timeout=8.0)
        self._sock = s
        try:
            rdr = FrameReader(s)
            seq = 3

            # LanAuth
            s.sendall(
                build_ctrl(
                    seq,
                    CMD_LANAUTH,
                    pb_field_bytes(2, self.did) + pb_field_bytes(3, self.secret),
                )
            )
            seq += 1
            session_key = None
            t_end = time.time() + 8.0
            while time.time() < t_end and session_key is None:
                mt, mf, body = rdr.next_frame(timeout=8.0)
                if mt == 4:
                    cmd, rseq, rpc, ct = ctrl_exhdr(body)
                    if cmd == CMD_LANAUTH:
                        pt = ctrl_decrypt(cmd, rseq, rpc, ct)
                        if pt:
                            f = pb_parse(pt)
                            if 1 in f and isinstance(f[1][0], bytes):
                                session_key = f[1][0]
            if not session_key:
                raise RuntimeError("LanAuth sem session key (credenciais?)")

            self._set(connected=True, status="autenticado, iniciando vídeo…", did=self.did.decode())

            def answer(cmd, rseq, rpc, ct):
                if rpc != 0:
                    return
                if cmd == CMD_PING:
                    pt = ctrl_decrypt(cmd, rseq, rpc, ct)
                    echo = pb_parse(pt).get(1, [0])[0] if pt else 0
                    pong = (
                        pb_varint_field(1, echo)
                        + pb_varint_field(2, HB_CONST)
                        + pb_varint_field(3, int(time.time() * 1000))
                    )
                    s.sendall(build_ctrl(rseq, CMD_PING, pong, flag=0x0D))
                elif cmd == CMD_HELLO:
                    s.sendall(build_ctrl(rseq, CMD_HELLO, pb_varint_field(1, 1), flag=0x0D))

            def pump(dur):
                media = MediaFragments()
                end = time.time() + dur
                while time.time() < end and not self._stop_event.is_set():
                    try:
                        mt, mf, body = rdr.next_frame(timeout=max(0.05, end - time.time()))
                    except TimeoutError:
                        break
                    if mt == 4:
                        cmd, rseq, rpc, ct = ctrl_exhdr(body)
                        answer(cmd, rseq, rpc, ct)
                    elif mt == 6:
                        try:
                            media.append(body)
                        except ValueError:
                            media.clear()
                return media

            def start_video():
                nonlocal seq
                for cmd in (2653, 2645, CMD_VIDEOPLAY):
                    s.sendall(build_ctrl(seq, cmd, b""))
                    seq += 1
                    pump(0.12)

            s.sendall(build_ctrl(seq, CMD_HELLO, pb_varint_field(1, 1)))
            seq += 1
            pump(0.2)
            start_video()

            self._set(status="streaming…", connected=True)

            frags = MediaFragments()
            started = False
            frames = 0
            t_fps = time.time()

            def feed(body):
                # só subtype 4 (vídeo); bit7 = início de frame
                subtype = body[0] & 0x7F
                frame_start = bool(body[0] & 0x80)
                if subtype != 4:
                    return False
                nonlocal frags, started, frames, t_fps
                got_frame = False
                if frame_start:
                    if started and frags:
                        jpg = decrypt_media_frame(frags, session_key)
                        if jpg:
                            got_frame = True
                            now = time.time()
                            with self.lock:
                                self.state["jpeg"] = jpg
                                self.state["seq"] = self.state.get("seq", 0) + 1
                                self.state["last_frame_ts"] = now
                                frames += 1
                                if now - t_fps >= 1.0:
                                    self.state["fps"] = frames / (now - t_fps)
                                    frames = 0
                                    t_fps = now
                                self.state["status"] = "streaming"
                                self.state["connected"] = True
                    frags = MediaFragments()
                    frags.append(body)
                    started = True
                elif started:
                    try:
                        frags.append(body)
                    except ValueError:
                        frags.clear()
                        started = False
                return got_frame

            session_frames = 0
            last_video = time.time()  # the camera ends the video after a while but keeps answering pings
            while not self._stop_event.is_set():
                silent = time.time() - last_video
                if silent > STALL_SECONDS:
                    self._fast_retry = True
                    raise ConnectionError(f"sem vídeo há {silent:.0f}s")
                try:
                    mt, mf, body = rdr.next_frame(timeout=2.0)
                except TimeoutError:
                    continue
                if mt == 4:
                    cmd, rseq, rpc, ct = ctrl_exhdr(body)
                    answer(cmd, rseq, rpc, ct)
                elif mt == 6:
                    if feed(body):
                        last_video = time.time()
                        session_frames += 1
                        if session_frames >= SESSION_FRAMES:
                            self._fast_retry = True
                            return
        finally:
            try:
                s.close()
            except OSError:
                pass
            self._sock = None
            self._set(connected=False)


# ---------------------------------------------------------------------------
# Discovery UDP mínima (fallback se faltar .env)
# ---------------------------------------------------------------------------
def discover_once(ip=None, wait=2.5):
    body = b"\x01" + b"\xa8\x14" + b"\x0c"
    pkt = b"\x51\x70" + b"\x48" + bytes([len(body)]) + body
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0))
    s.settimeout(0.3)
    found = None
    try:
        for dst in (("255.255.255.255", PORT_DEFAULT),) + (((ip, PORT_DEFAULT),) if ip else ()):
            try:
                s.sendto(pkt, dst)
            except OSError:
                pass
        end = time.time() + wait
        while time.time() < end and found is None:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            if len(data) < 6 or data[:2] != b"\x51\x70" or (data[2] >> 4) != 4:
                continue
            try:
                ln, i = dec_varint(data, 3)
                cmd, rseq, rpc, ct = ctrl_exhdr(data[i : i + ln])
                pt = ctrl_decrypt(cmd, rseq, rpc, ct)
                if not pt:
                    continue
                f = pb_parse(pt)

                def sv(k):
                    v = f.get(k, [None])[0]
                    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v

                did = sv(1)
                if did:
                    found = {
                        "did": did,
                        "ip": addr[0],
                        "port": f.get(4, [PORT_DEFAULT])[0],
                        "secret": sv(8),
                        "fw": sv(5),
                    }
            except Exception:
                continue
    finally:
        s.close()
    return found


# ---------------------------------------------------------------------------
# UI Tk
