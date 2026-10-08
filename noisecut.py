#!/usr/bin/env python3
"""NoiseCut Studio: editor de video de escritorio con reducción de ruido de fondo.
PySide6 (interfaz) + FFmpeg (procesamiento y exportación)."""
import sys, os, re, json, shutil, subprocess, tempfile, copy, wave, struct, math, hashlib, importlib.util
import urllib.request, urllib.error
from datetime import datetime
from dataclasses import dataclass, field
from PySide6.QtCore import Qt, QUrl, QThread, Signal, QSize, QPointF, QRectF, QTimer, QSettings
from PySide6.QtGui import QColor, QAction, QIcon, QBrush, QPen, QPainter, QPixmap, QFont, QFontMetrics, QShortcut, QKeySequence
from PySide6.QtWidgets import *
from PySide6.QtMultimedia import (QMediaPlayer, QAudioOutput, QMediaCaptureSession,
                                  QAudioInput, QMediaRecorder, QMediaFormat, QMediaDevices)
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtSvg import QSvgRenderer

# Con --noconsole (PyInstaller) sys.stdout y sys.stderr son None; tqdm/huggingface_hub
# fallan al descargar modelos si no hay donde escribir. Se les da un destino nulo.
if sys.stdout is None or sys.stderr is None:
    for _stream in ('stdout', 'stderr'):
        if getattr(sys, _stream) is None:
            setattr(sys, _stream, open(os.devnull, 'w', encoding='utf-8'))
    os.environ.setdefault('HF_HUB_DISABLE_PROGRESS_BARS', '1')

W, H, FPS = 1920, 1080, 30
VID = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}
IMG = {'.png', '.jpg', '.jpeg', '.bmp', '.webp'}
AUD = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg'}
NOWIN = 0x08000000 if os.name == 'nt' else 0


def resource_path(*parts):
    root = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)


def network_hint(error):
    """True si el texto del error parece un fallo de red (sin internet, DNS, tiempo agotado)."""
    text = str(error).lower()
    return any(k in text for k in ('connectionerror', 'urlerror', 'max retries', 'getaddrinfo',
        'timed out', 'timeout', 'localentrynotfound', 'offline', 'name or service', 'ssl',
        'connection', 'httperror', 'unreachable', 'proxy', '11001', '10060', '10061'))


def ffbin(name):
    # En el .exe de PyInstaller, ffmpeg\ va junto al ejecutable (no dentro de _internal).
    base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(sys.argv[0]))
    here = os.path.join(base, 'ffmpeg', name)
    for p in (here, here + '.exe'):
        if os.path.exists(p):
            return p
    return shutil.which(name)


def ffmpeg_has_filter(name):
    binary = ffbin('ffmpeg')
    if not binary:
        return False
    try:
        result = subprocess.run([binary, '-hide_banner', '-filters'], capture_output=True,
                                 text=True, creationflags=NOWIN, timeout=12)
        return result.returncode == 0 and any(len(parts := line.split()) > 1 and parts[1] == name
                                               for line in result.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        return False


def available_encoders():
    binary = ffbin('ffmpeg')
    if not binary:
        return None
    try:
        result = subprocess.run([binary, '-hide_banner', '-encoders'], capture_output=True,
            text=True, creationflags=NOWIN, timeout=20)
        return set(re.findall(r'^\s*[VAS\.]{6}\s+(\S+)', result.stdout, flags=re.M)) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def probe(path):
    r = subprocess.run([ffbin('ffprobe'), '-v', 'error', '-show_entries', 'format=duration:stream=codec_type',
                        '-of', 'json', path], capture_output=True, text=True, creationflags=NOWIN)
    j = json.loads(r.stdout or '{}')
    dur = float(j.get('format', {}).get('duration') or 0)
    return dur, any(s.get('codec_type') == 'audio' for s in j.get('streams', []))


@dataclass
class Clip:
    path: str
    kind: str
    src: float
    audio: bool
    start: float = 0.0
    end: float = 0.0
    speed: float = 1.0
    bright: float = 0.0
    contrast: float = 1.0
    sat: float = 1.0
    blur: float = 0.0
    denoise: int = 0
    fade: float = 0.0
    scale: float = 100.0
    pos_x: float = 50.0
    pos_y: float = 50.0
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    crop_left: float = 0.0
    crop_right: float = 0.0
    crop_top: float = 0.0
    crop_bottom: float = 0.0
    opacity: float = 100.0
    reverse: bool = False
    volume: float = 100.0
    audio_fade_in: float = 0.0
    audio_fade_out: float = 0.0
    denoise_mode: str = 'standard'
    noise_profile: dict = field(default_factory=dict)
    noise_preset: str = 'Voz en interior'
    hum_removal: bool = False
    wind_reduction: bool = False
    clean_mix: float = 1.0
    normalize: bool = False
    voice_enhance: bool = False
    pitch: int = 0
    effects: dict = field(default_factory=dict)
    chroma_color: str = '#00ff00'
    chroma_similarity: float = 0.25
    chroma_enabled: bool = False
    lut_path: str = ''
    transition: str = 'cut'
    transition_duration: float = 0.5
    video_locked: bool = False
    video_hidden: bool = False

    @property
    def out(self):
        return max(0.1, (self.end - self.start) / self.speed)


def esc(p):
    return p.replace('\\', '/').replace(':', '\\:').replace("'", "\\'")


def sysfont():
    for p in ('C:/Windows/Fonts/arial.ttf', '/System/Library/Fonts/Supplemental/Arial.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if os.path.exists(p):
            return p


def atempo(s):
    f = []
    while s > 2:
        f.append('atempo=2'); s /= 2
    while s < 0.5:
        f.append('atempo=0.5'); s *= 2
    f.append(f'atempo={s:.4f}')
    return f


def db_to_linear(db):
    """Convert a track fader in dB to its linear FFmpeg volume multiplier."""
    return round(10 ** (max(-60.0, min(12.0, float(db))) / 20.0), 8)


def loudnorm_filter(target_lufs=-23, stats=None, peak=-1.5):
    """Return a linear two-pass loudnorm filter when measurements are available."""
    target = max(-30, min(-5, float(target_lufs)))
    peak = max(-9, min(-1.0, float(peak)))
    measured = stats or {}
    keys = ('input_i', 'input_lra', 'input_tp', 'input_thresh', 'target_offset')
    if all(key in measured and math.isfinite(float(measured[key])) for key in keys):
        args = (('measured_I', 'input_i'), ('measured_LRA', 'input_lra'),
                ('measured_TP', 'input_tp'), ('measured_thresh', 'input_thresh'),
                ('offset', 'target_offset'))
        suffix = ':' + ':'.join(f'{name}={float(measured[key]):.2f}' for name, key in args)
        return f'loudnorm=I={target:g}:TP={peak:g}:LRA=11:linear=true' + suffix
    return f'loudnorm=I={target:g}:TP={peak:g}:LRA=11:linear=true'


def voice_effect_filters(effect='none'):
    """Built-in optional voice colors that do not need external plugins."""
    if effect in ('grave', 'agudo'):
        ratio = 0.85 if effect == 'grave' else 1.18
        return [f'asetrate=48000*{ratio:.4f}', 'aresample=48000'] + atempo(1 / ratio)
    if effect == 'robot':
        return ['aecho=0.8:0.88:8|16:0.35|0.18']
    if effect == 'eco':
        return ['aecho=0.8:0.75:70|140:0.25|0.12']
    return []


def audio_track_filters(track, project_duration=None, duck=False, duck_level='normal', normalize_override=False):
    """Build one independent audio lane, trimmed to the project and ready to mix."""
    f = []
    source_start = max(0.0, float(track.get('source_start', 0) or 0))
    source_end = track.get('source_end')
    if source_end is not None:
        f += [f'atrim=start={source_start:.3f}:end={max(source_start + 0.05, float(source_end)):.3f}',
              'asetpts=PTS-STARTPTS']
    elif source_start:
        f += [f'atrim=start={source_start:.3f}', 'asetpts=PTS-STARTPTS']
    speed = max(0.25, min(4.0, float(track.get('speed', 1.0) or 1.0)))
    if track.get('preserve_pitch', True):
        f += atempo(speed)
    else:
        f += [f'asetrate=48000*{speed:.5f}', 'aresample=48000']
    if track.get('remove_leading_silence'):
        f.append('silenceremove=start_periods=1:start_duration=0.10:start_threshold=-45dB:detection=rms')
    f += audio_fx_filters(track.get('denoise', 0), track.get('denoise_mode', 'standard'),
        track.get('model'), False, track.get('voice_enhance', False),
        track.get('pitch', 0), track.get('noise_profile'), track.get('noise_preset', 'Voz en interior'),
        track.get('hum_removal', False), track.get('wind_reduction', False), track.get('clean_mix', 1.0))
    f += voice_effect_filters(track.get('voice_effect', 'none'))
    role = track.get('role', 'music')
    if track.get('normalize') or normalize_override:
        measured = track.get('loudnorm_stats')
        if not measured and role == 'voice':
            measured = (track.get('noise_profile') or {}).get('loudnorm')
        f.append(loudnorm_filter(-16 if role == 'voice' else -23,
                                 measured, -1.5))
    volume = db_to_linear(track.get('volume_db', 20 * math.log10(max(1e-8, float(track.get('vol', 1.0))))))
    if track.get('muted'):
        volume = 0.0
    f.append(f'volume={volume:.8f}')
    if track.get('fade_in', 0) > 0:
        f.append(f"afade=t=in:st=0:d={max(0, float(track['fade_in'])):.3f}:curve=desi")
    effective_duration = track.get('clip_duration', track.get('duration', 0))
    if project_duration is not None:
        effective_duration = max(0.0, min(float(effective_duration or project_duration),
                                           project_duration - float(track.get('offset', 0))))
    fade_out = max(0.0, float(track.get('fade_out', 0)))
    if fade_out and effective_duration:
        fade_out = min(fade_out, effective_duration)
        f.append(f'afade=t=out:st={max(0, effective_duration - fade_out):.3f}:d={fade_out:.3f}:curve=desi')
    if project_duration is not None:
        remaining = max(0.0, project_duration - float(track.get('offset', 0)))
        target_duration = min(float(effective_duration or remaining), remaining)
        if track.get('loop'):
            target_duration = remaining
        if target_duration > 0:
            f.append(f'atrim=duration={target_duration:.3f}')
    elif effective_duration:
        f.append(f'atrim=duration={float(effective_duration):.3f}')
    return f


def duck_filter(level='normal'):
    ratio = {'soft': 3, 'normal': 8, 'strong': 14}.get(level, 8)
    return f'sidechaincompress=threshold=0.02:ratio={ratio}:attack=20:release=300'


def audible_audio_tracks(tracks):
    tracks = list(tracks or [])
    solo = any(track.get('solo') and not track.get('muted') for track in tracks)
    return [track for track in tracks if not track.get('muted') and
            (not solo or track.get('solo'))]


def append_audio_track(a, fc, track, next_input, project_length, label,
                       normalize_override=False, settings=None):
    """Append a track source; short tracks repeat with a small overlapping crossfade."""
    settings = settings or {}
    offset = max(0.0, float(track.get('offset', 0)))
    remaining = max(0.0, project_length - offset)
    speed = max(0.25, min(4.0, float(track.get('speed', 1) or 1)))
    source_start = max(0.0, float(track.get('source_start', 0) or 0))
    source_end = track.get('source_end')
    source_duration = (float(source_end) - source_start if source_end is not None else
                       float(track.get('duration', 0) or 0))
    loop = bool(track.get('loop')) and source_duration > 0 and source_duration / speed < remaining
    if loop:
        crossfade_source = min(0.2 * speed, source_duration / 4)
        count = max(2, int(math.ceil((remaining * speed - crossfade_source) /
                                     max(0.05, source_duration - crossfade_source))))
        count = min(count, 128)
        parts = []
        for part_index in range(count):
            a += ['-i', track['path']]
            part = f'{label}part{part_index}'
            trim = f'atrim=start={source_start:.3f}'
            if source_end is not None:
                trim += f':end={float(source_end):.3f}'
            fc.append(f'[{next_input}:a]{trim},asetpts=PTS-STARTPTS[{part}]')
            parts.append(part); next_input += 1
        current = parts[0]
        for part_index, part in enumerate(parts[1:], 1):
            merged = f'{label}loop{part_index}'
            fc.append(f'[{current}][{part}]acrossfade=d={crossfade_source:.3f}:c1=exp:c2=exp[{merged}]')
            current = merged
        prepared = dict(track, loop=False, source_start=0, source_end=None,
                        duration=remaining * speed, clip_duration=remaining,
                        model=track.get('model') or settings.get('rnnoise_model'))
        filters = audio_track_filters(prepared, project_length,
                                      normalize_override=normalize_override)
        filters += [f'adelay={int(offset * 1000)}|{int(offset * 1000)}',
                    'aformat=sample_rates=48000:channel_layouts=stereo']
        fc.append(f'[{current}]{",".join(filters)}[{label}]')
        return next_input, label
    a += ['-i', track['path']]
    prepared = dict(track, model=track.get('model') or settings.get('rnnoise_model'))
    filters = audio_track_filters(prepared, project_length,
                                  normalize_override=normalize_override)
    filters += [f'adelay={int(offset * 1000)}|{int(offset * 1000)}',
                'aformat=sample_rates=48000:channel_layouts=stereo']
    fc.append(f'[{next_input}:a]{",".join(filters)}[{label}]')
    return next_input + 1, label


def denoise_f(nr, mode='standard', model=None):
    # Reducción espectral (FFmpeg afftdn) + filtro de graves (retumbos, ventiladores)
    if nr <= 0:
        return None
    if mode == 'ai':
        if not model:
            # Sin modelo local, nunca bloquea el proyecto: vuelve al filtro estándar.
            mode = 'standard'
        else:
            return f"highpass=f=80,arnndn=m='{esc(model)}'"
    return f'highpass=f=80,afftdn=nr={nr}:nf=-30:tn=1' if nr > 0 else None


def audio_fx_filters(nr=0, mode='standard', model=None, normalize=False,
                     voice_enhance=False, pitch=0, profile=None, preset='Voz en interior',
                     hum=False, wind=False, mix=1.0):
    filters = []
    profile = profile or {}
    floor_db = max(-80.0, min(-20.0, float(profile.get('noise_floor_db', -50.0))))
    highpass = {'Exterior con viento': 150, 'Carro / ruido de carretera': 120,
                'Llamada / audio de WhatsApp': 120, 'Podcast / locucion': 80}.get(preset, 80)
    if wind:
        highpass = max(highpass, 150)
    if nr > 0 or voice_enhance or hum or wind:
        filters.append(f'highpass=f={highpass}')
    if hum:
        mains = int(profile.get('mains_hz', 60))
        for frequency in range(mains, 240, mains):
            filters.append(f'equalizer=f={frequency}:t=q:w=4:g=-18')
    if nr > 0 and mode == 'ai' and model:
        filters.append(f"arnndn=m='{esc(model)}':mix={max(0.0, min(1.0, float(mix))):.3f}")
    elif nr > 0:
        preset_boost = 8 if preset == 'Exterior con viento' else (5 if preset == 'Carro / ruido de carretera' else 0)
        strength = max(1, min(40, int(nr) + preset_boost))
        # El primer pase elimina el ruido principal; el segundo reduce el residuo.
        filters.extend([f'afftdn=nr={strength}:nf={floor_db:.1f}:tn=1',
                        f'afftdn=nr={max(1, strength // 3)}:nf={max(-80, floor_db - 6):.1f}:tn=1',
                        'anlmdn=s=0.0001:p=0.002:r=0.006'])
    noise_db = float(profile.get('noise_floor_db', -50.0))
    gate_db = max(-72.0, min(-20.0, noise_db + 8.0))
    gate_threshold = max(0.00025, min(0.2, 10 ** (gate_db / 20.0)))
    if nr > 0 or voice_enhance:
        filters.append(f'agate=threshold={gate_threshold:.6f}:ratio=4.0:attack=10:release=120:makeup=1.5:range=0.001')
    if voice_enhance:
        if preset == 'Exterior con viento':
            filters.append('equalizer=f=250:t=q:w=1:g=-2')
        else:
            filters.append('equalizer=f=250:t=q:w=1:g=-2')
        filters.extend(['equalizer=f=3000:t=q:w=1:g=4', 'equalizer=f=8000:t=q:w=1:g=2',
                        'deesser=i=0.25:m=0.35:f=0.5',
                        'acompressor=threshold=-18dB:ratio=3.0:attack=5:release=150:makeup=1.2'])
    if normalize:
        stats = profile.get('loudnorm') or {}
        stat_keys = ('input_i', 'input_lra', 'input_tp', 'input_thresh', 'target_offset')
        if all(k in stats and math.isfinite(float(stats[k])) for k in stat_keys):
            filters.append('loudnorm=I=-16:TP=-1.5:LRA=11:linear=true:' + ':'.join(
                f'{key}={float(stats[source]):.2f}' for key, source in
                (('measured_I', 'input_i'), ('measured_LRA', 'input_lra'),
                 ('measured_TP', 'input_tp'), ('measured_thresh', 'input_thresh'),
                 ('offset', 'target_offset'))))
        else:
            filters.append('loudnorm=I=-16:TP=-1.5:LRA=11')
        filters.append('alimiter=limit=0.841395:attack=5:release=50:level=false')
    if pitch:
        ratio = 2 ** (pitch / 12)
        filters += [f'asetrate=48000*{ratio:.6f}', 'aresample=48000'] + atempo(1 / ratio)
    return filters


def noise_gate_threshold(noise_db):
    """Return an agate threshold about 6 dB above the measured noise floor."""
    level = max(-72.0, min(-20.0, float(noise_db) + 6.0))
    return round(max(0.00025, min(0.2, 10 ** (level / 20.0))), 6)


def _rms_level(ffmpeg, path, start, duration):
    result = subprocess.run([ffmpeg, '-hide_banner', '-ss', f'{max(0, start):.3f}', '-i', path,
        '-t', f'{max(0.1, duration):.3f}', '-af', 'astats=metadata=0:reset=0', '-f', 'null', '-'],
        capture_output=True, text=True, creationflags=NOWIN, timeout=90)
    matches = re.findall(r'RMS level dB:\s*(-?\d+(?:\.\d+)?)', result.stderr)
    if result.returncode or not matches:
        return -80.0
    return max(-80.0, min(0.0, float(matches[-1])))


def analyze_noise_profile(path, start=0, duration=0, settings=None):
    """Measure quiet and active windows, then cache first-pass loudnorm stats."""
    ffmpeg = ffbin('ffmpeg')
    if not ffmpeg or not os.path.isfile(path):
        return {'noise_floor_db': -50.0, 'voice_db': -30.0, 'gate_threshold': noise_gate_threshold(-50),
                'margin_db': 20.0, 'loudnorm': {}, 'measured': False}
    settings = settings or {}
    stat = os.stat(path)
    key_data = json.dumps([os.path.abspath(path), stat.st_size, stat.st_mtime_ns, start, duration, settings],
                          sort_keys=True, default=str)
    key = hashlib.sha256(key_data.encode('utf-8')).hexdigest()
    cache = os.path.join(tempfile.gettempdir(), 'noisecutstudio_audio_profiles', key + '.json')
    try:
        with open(cache, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        pass
    args = [ffmpeg, '-hide_banner', '-ss', f'{max(0, start):.3f}', '-i', path]
    if duration > 0:
        args += ['-t', f'{duration:.3f}']
    args += ['-af', 'silencedetect=noise=-38dB:d=0.18', '-f', 'null', '-']
    try:
        scan = subprocess.run(args, capture_output=True, text=True, creationflags=NOWIN, timeout=120)
        silence_starts = [float(v) for v in re.findall(r'silence_start:\s*(-?\d+(?:\.\d+)?)', scan.stderr)]
        silence_ends = [float(v) for v in re.findall(r'silence_end:\s*(-?\d+(?:\.\d+)?)', scan.stderr)]
        silences = list(zip(silence_starts, silence_ends))
        if len(silence_starts) > len(silence_ends):
            silences.append((silence_starts[-1], silence_starts[-1] + 0.5))
        quiet = max(silences, key=lambda pair: pair[1] - pair[0]) if silences else (0.0, 0.5)
        noise_db = _rms_level(ffmpeg, path, start + quiet[0], min(1.0, quiet[1] - quiet[0]))
        active = next((end for _, end in silences if end + 0.1 < (duration or 3600)), 0.0)
        voice_db = _rms_level(ffmpeg, path, start + active, min(1.0, max(0.1, duration - active if duration else 1.0)))
        cfg = dict(settings)
        chain = audio_fx_filters(cfg.get('denoise', 15), cfg.get('mode', 'standard'),
            cfg.get('model'), False, True, 0,
            {'noise_floor_db': noise_db}, cfg.get('preset', 'Voz en interior'),
            cfg.get('hum', False), cfg.get('wind', False), cfg.get('mix', 1.0))
        loud = subprocess.run([ffmpeg, '-hide_banner', '-ss', f'{max(0, start):.3f}', '-i', path,
            '-t', f'{max(0.1, duration or 10):.3f}', '-af', ','.join(chain) +
            ',loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json', '-f', 'null', '-'],
            capture_output=True, text=True, creationflags=NOWIN, timeout=180)
        json_blocks = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', loud.stderr, flags=re.S)
        loudnorm = json.loads(json_blocks[-1]) if json_blocks else {}
        profile = {'noise_floor_db': noise_db, 'voice_db': voice_db,
                   'gate_threshold': noise_gate_threshold(noise_db),
                   'margin_db': voice_db - noise_db, 'loudnorm': loudnorm,
                   'measured': bool(scan.returncode == 0 and loud.returncode == 0)}
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        profile = {'noise_floor_db': -50.0, 'voice_db': -30.0,
                   'gate_threshold': noise_gate_threshold(-50), 'margin_db': 20.0,
                   'loudnorm': {}, 'measured': False}
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    try:
        with open(cache, 'w', encoding='utf-8') as fh:
            json.dump(profile, fh)
    except OSError:
        pass
    return profile


def video_fx_filters(effects=None, width=1920, height=1080, chroma=False,
                     chroma_color='#00ff00', chroma_similarity=0.25, lut_path=''):
    effects = effects or {}
    filters = []
    vignette = float(effects.get('vignette', 0))
    grain = int(effects.get('grain', 0))
    sharp = float(effects.get('sharpness', 0))
    pixel = int(effects.get('pixelate', 0))
    if vignette > 0:
        filters.append(f'vignette=angle={min(math.pi / 2, vignette * math.pi / 200):.4f}')
    if grain > 0:
        filters.append(f'noise=alls={min(100, grain)}:allf=t')
    if sharp > 0:
        filters.append(f'unsharp=5:5:{min(5, sharp):.2f}:5:5:0')
    denoise_video = float(effects.get('hqdn3d', 0))
    if denoise_video > 0:
        filters.append(f'hqdn3d={denoise_video:.2f}:{denoise_video:.2f}:6:6')
    if effects.get('auto_color'):
        filters.append('normalize=blackpt=black:whitept=white:smoothing=50:independence=0:strength=0.5')
    if effects.get('mirror'):
        filters.append('hflip')
    if effects.get('glitch'):
        filters.append('rgbashift=rh=4:bh=-4')
    if effects.get('black_white'):
        filters.append('hue=s=0')
    if effects.get('sepia'):
        filters.append('colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131')
    if pixel > 0:
        factor = max(2, pixel)
        filters.append(f'scale=trunc(iw/{factor}):trunc(ih/{factor}):flags=neighbor,scale={width}:{height}:flags=neighbor')
    if lut_path:
        filters.append(f"lut3d=file='{esc(lut_path)}'")
    if chroma:
        filters.append('format=rgba')
        color = chroma_color.lstrip('#')
        filters.append(f'colorkey=0x{color}:{max(0,min(1,chroma_similarity)):.3f}:0.10')
    return filters


def subtitle_records(segments):
    """Convierte segmentos Whisper en elementos editables de la pista de texto."""
    return [{'text': str(text).strip(), 'start': float(start), 'end': max(float(start) + 0.1, float(end)),
             'x': 50, 'y': 85, 'size': 48, 'color': '#ffffff', 'bold': True,
             'border': True, 'shadow': True, 'background': False, 'animation': 'fade',
             'is_subtitle': True}
            for start, end, text in segments if str(text).strip()]


def srt_time(seconds):
    ms = max(0, int(round(float(seconds) * 1000)))
    hours, ms = divmod(ms, 3600000); minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}'


def subtitles_srt(items):
    rows = sorted((item for item in items if item.get('is_subtitle', True)),
                  key=lambda item: float(item.get('start', 0)))
    return '\n\n'.join(f'{index}\n{srt_time(item["start"])} --> {srt_time(item["end"])}\n'
                       f'{item.get("text", "")}' for index, item in enumerate(rows, 1)) + ('\n' if rows else '')


def stabilization_commands(ffmpeg, source, transform_file, output):
    detect = [ffmpeg, '-y', '-hide_banner', '-nostats', '-i', source,
              '-vf', f'vidstabdetect=shakiness=5:accuracy=15:result={esc(transform_file)}',
              '-f', 'null', '-']
    apply = [ffmpeg, '-y', '-hide_banner', '-nostats', '-i', source,
             '-vf', f'vidstabtransform=input={esc(transform_file)}:smoothing=10:optzoom=1',
             '-map', '0:v:0', '-map', '0:a?', '-c:v', 'libx264', '-preset', 'medium',
             '-crf', '18', '-c:a', 'aac', '-b:a', '192k', output]
    return detect, apply


def canvas_size(settings):
    resolution = int(settings.get('resolution', 1080))
    aspect = settings.get('aspect', '16:9')
    ratios = {'16:9': (16, 9), '9:16': (9, 16), '1:1': (1, 1), '4:5': (4, 5)}
    rw, rh = ratios.get(aspect, ratios['16:9'])
    if rw >= rh:
        height = resolution
        width = int(resolution * rw / rh)
    else:
        width = resolution
        height = int(resolution * rh / rw)
    # H.264/H.265 con yuv420p exigen ancho y alto pares (p. ej. 853 -> 854).
    return width + width % 2, height + height % 2


def project_duration(clips, music=None, overlays=None, texts=None, stickers=None):
    clips = list(clips or [])
    active = [i for i in range(1, len(clips))
              if getattr(clips[i], 'transition', 'cut') != 'cut'
              and getattr(clips[i], 'transition_duration', 0) > 0]
    total = sum(c.out for c in clips) - sum(min(clips[i].transition_duration,
                clips[i - 1].out, clips[i].out) for i in active)
    end_times = [] if clips else [m.get('offset', 0) + m.get('clip_duration', m.get('duration', 0)) for m in music or []]
    if not clips:
        end_times += [item.get('end', 0) for collection in (overlays, texts, stickers)
                      for item in collection or []]
    return max(total, max(end_times, default=0), 0)


def split_around_silence(start, end, silences, minimum=0.6, keep=0.25):
    cuts = sorted((max(start, float(a)), min(end, float(b))) for a, b in silences
                  if float(b) - float(a) >= minimum and float(b) > start and float(a) < end)
    merged = []
    for left, right in cuts:
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    pieces, cursor = [], start
    for left, right in merged:
        if left - cursor >= keep:
            pieces.append((cursor, left))
        cursor = max(cursor, right)
    if end - cursor >= keep:
        pieces.append((cursor, end))
    return pieces


def detect_silences(path, start=0, duration=0, threshold_db=-35, minimum=0.6):
    ffmpeg = ffbin('ffmpeg')
    if not ffmpeg:
        raise FileNotFoundError('No se encuentra FFmpeg.')
    command = [ffmpeg, '-hide_banner', '-ss', str(max(0, start)), '-i', path]
    if duration > 0:
        command += ['-t', str(duration)]
    command += ['-af', f'silencedetect=noise={threshold_db}dB:d={minimum}', '-f', 'null', '-']
    result = subprocess.run(command, capture_output=True, text=True, creationflags=NOWIN, timeout=900)
    if result.returncode:
        raise RuntimeError(result.stderr[-1200:])
    starts = [float(v) for v in re.findall(r'silence_start:\s*(-?\d+(?:\.\d+)?)', result.stderr)]
    ends = [float(v) for v in re.findall(r'silence_end:\s*(-?\d+(?:\.\d+)?)', result.stderr)]
    intervals = list(zip(starts, ends))
    if len(starts) > len(ends):
        intervals.append((starts[-1], duration or starts[-1]))
    return [(start + a, start + b) for a, b in intervals]


def preview_fingerprint(state, media_paths=(), audio_mode='after'):
    media = []
    for path in sorted(set(p for p in media_paths if p)):
        try:
            stat = os.stat(path)
            media.append((path, stat.st_size, stat.st_mtime_ns))
        except OSError:
            media.append((path, None, None))
    payload = json.dumps({'state': state, 'media': media, 'audio_mode': audio_mode},
                         ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def resolve_project_paths(data, project_directory):
    media_collections = ('clips', 'music', 'stickers', 'overlays')
    for collection in media_collections:
        for item in data.get(collection, []) or []:
            path = item.get('path')
            if path and not os.path.isabs(path):
                item['path'] = os.path.normpath(os.path.join(project_directory, path))
    return data


def make_project_relative(path, project_directory):
    if not path or not os.path.isabs(path):
        return path
    try:
        if os.path.normcase(os.path.commonpath((os.path.abspath(path), os.path.abspath(project_directory)))) == os.path.normcase(os.path.abspath(project_directory)):
            return os.path.relpath(path, project_directory)
    except ValueError:
        pass
    return path


def build_mp3(clips, music, out, settings):
    a = [ffbin('ffmpeg'), '-y', '-hide_banner', '-nostats', '-progress', 'pipe:1']
    fc, n = [], 0
    total = max(project_duration(clips, music), 0.1)
    labels = ['ac']
    voice_labels = []
    for c in clips:
        if c.kind != 'video' or not c.audio:
            continue
        a += ['-i', c.path]
        f = [f'atrim=start={c.start}:end={c.end}', 'asetpts=PTS-STARTPTS']
        if c.reverse:
            f.append('areverse')
        f += atempo(c.speed)
        f += audio_fx_filters(c.denoise, c.denoise_mode, settings.get('rnnoise_model'),
                              c.normalize, c.voice_enhance, c.pitch, c.noise_profile,
                              c.noise_preset, c.hum_removal, c.wind_reduction, c.clean_mix)
        f.append(f'volume={c.volume / 100:.3f}')
        if c.audio_fade_in > 0:
            f.append(f'afade=t=in:st=0:d={c.audio_fade_in}')
        if c.audio_fade_out > 0:
            f.append(f'afade=t=out:st={max(0, c.out - c.audio_fade_out):.3f}:d={c.audio_fade_out}')
        f.append('aformat=sample_rates=48000:channel_layouts=stereo')
        fc.append(f'[{n}:a]{",".join(f)}[clip{n}]'); labels.append(f'clip{n}'); n += 1
    fc.append(f'anullsrc=r=48000:cl=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS[ac]')
    voice_labels = [label for label in labels[1:]]
    audible = audible_audio_tracks(music)
    has_solo = any(m.get('solo') and not m.get('muted') for m in music)
    has_video_voice = any(c.kind == 'video' and c.audio for c in clips)

    def wants_duck(track):
        return (track.get('ducking', False) or settings.get('duck_music', False)) and \
               track.get('role', 'music') == 'music' and voice_labels
    duck_count = sum(1 for m in music if m in audible and wants_duck(m))
    control_labels = []
    labels = ['ac']
    if voice_labels:
        # Cada etiqueta de filtro solo se puede usar una vez: la mezcla de voz se reparte con asplit.
        fc.append(''.join(f'[{label}]' for label in voice_labels) +
                  f'amix=inputs={len(voice_labels)}:duration=longest:normalize=0[voicemix]')
        if duck_count:
            control_labels = [f'voicecontrol{i}' for i in range(duck_count)]
            fc.append('[voicemix]asplit=%d[voicefinal]' % (duck_count + 1) +
                      ''.join(f'[{x}]' for x in control_labels))
            labels.append('voicefinal')
        else:
            labels.append('voicemix')
    if has_solo:
        if voice_labels:
            fc.append(f'[{labels.pop()}]anullsink')
        labels = []
    for track_index, m in enumerate(music):
        if m not in audible:
            continue
        source_label = f'music{n}'
        n, source_label = append_audio_track(a, fc, m, n, total, source_label,
            normalize_override=has_video_voice and m.get('role', 'music') == 'music', settings=settings)
        if wants_duck(m):
            duck_label = f'ducked{n}'
            control = control_labels.pop(0)
            fc.append(f'[{source_label}][{control}]{duck_filter(m.get("duck_level", settings.get("duck_level", "normal")))}[{duck_label}]')
            source_label = duck_label
        labels.append(source_label)  # append_audio_track ya avanzo el indice de entrada
    if labels:
        fc.append(''.join(f'[{label}]' for label in labels) +
                  f'amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.841395:attack=5:release=50:level=false[aout]')
    else:
        fc.append(f'[ac]alimiter=limit=0.841395:attack=5:release=50:level=false[aout]')
    quality = settings.get('quality', 'alta')
    a += ['-filter_complex', ';'.join(fc), '-map', '[aout]', '-vn']
    if settings.get('format', 'MP3').upper() == 'WAV':
        a += ['-c:a', 'pcm_s16le', '-f', 'wav']
    elif settings.get('format', '').upper() == 'FLAC':
        a += ['-c:a', 'flac']
    elif settings.get('format', '').upper() in ('AAC', 'M4A'):
        a += ['-c:a', 'aac', '-b:a', '192k']
        if settings.get('format', '').upper() == 'AAC':
            a += ['-f', 'adts']
        else:
            a += ['-f', 'ipod']
    else:
        a += ['-c:a', 'libmp3lame', '-q:a', str({'baja': 6, 'media': 4, 'alta': 2}.get(quality, 2))]
    a.append(out)
    return a, total


def build(clips, texts, stickers, music, out, tmp, size=None, overlays=None, settings=None):
    settings = settings or {}
    overlays = overlays or []
    width, height = size or canvas_size(settings)
    fps = int(settings.get('fps', FPS))
    quality = settings.get('quality', 'alta')
    background = settings.get('background', 'negro')
    file_format = settings.get('format', 'MP4').upper()
    crf = {'baja': 28, 'media': 23, 'alta': 18}.get(quality, 18)
    preset = 'ultrafast' if settings.get('preview') else 'medium'
    if file_format in ('MP3', 'WAV', 'AAC', 'M4A', 'FLAC'):
        return build_mp3(clips, music, out, settings)
    a = [ffbin('ffmpeg'), '-y', '-hide_banner', '-nostats', '-progress', 'pipe:1']
    fc, n = [], 0
    for i, c in enumerate(clips):
        if c.kind == 'image':
            a += ['-loop', '1', '-framerate', str(fps), '-t', f'{c.out:.3f}', '-i', c.path]
        else:
            a += ['-i', c.path]
        n += 1
        v = [] if c.kind == 'image' else [f'trim=start={c.start}:end={c.end}']
        if any((c.crop_left, c.crop_right, c.crop_top, c.crop_bottom)):
            cw = max(0.1, 1 - (c.crop_left + c.crop_right) / 100)
            ch = max(0.1, 1 - (c.crop_top + c.crop_bottom) / 100)
            v.append(f'crop=iw*{cw:.4f}:ih*{ch:.4f}:iw*{c.crop_left / 100:.4f}:ih*{c.crop_top / 100:.4f}')
        if c.flip_h:
            v.append('hflip')
        if c.flip_v:
            v.append('vflip')
        if c.rotation:
            v.append(f'rotate={c.rotation}*PI/180:ow=rotw(iw):oh=roth(ih):c=black@0')
        if c.reverse:
            v += ['reverse', f'setpts=(PTS-STARTPTS)/{c.speed}']
        else:
            v.append(f'setpts=(PTS-STARTPTS)/{c.speed}')
        target_w = max(2, int(width * c.scale / 100))
        target_h = max(2, int(height * c.scale / 100))
        v += [f'fps={fps}', f'scale={target_w}:{target_h}:force_original_aspect_ratio=decrease',
              'setsar=1', f'eq=brightness={c.bright}:contrast={c.contrast}:saturation={c.sat}']
        if c.blur > 0:
            v.append(f'gblur=sigma={c.blur}')
        v += video_fx_filters(c.effects, width, height, c.chroma_enabled,
                              c.chroma_color, c.chroma_similarity, c.lut_path)
        fo = max(0, c.out - c.fade)
        if c.fade > 0:
            v += [f'fade=t=in:st=0:d={c.fade}', f'fade=t=out:st={fo:.3f}:d={c.fade}']
        v += ['format=rgba', f'colorchannelmixer=aa={max(0, min(100, c.opacity)) / 100:.3f}']
        fc.append(f'[{i}:v]{",".join(v)}[fg{i}]')
        if c.video_hidden:
            fc.append(f'color=c=black:s={width}x{height}:r={fps}:d={c.out:.3f}[v{i}]')
        elif background == 'desenfocado':
            back = [f'trim=start={c.start}:end={c.end}', f'setpts=(PTS-STARTPTS)/{c.speed}',
                    f'fps={fps}', f'scale={width}:{height}:force_original_aspect_ratio=increase',
                    f'crop={width}:{height}', 'gblur=sigma=24']
            fc.append(f'[{i}:v]{",".join(back)}[bg{i}]')
        else:
            fc.append(f'color=c=black:s={width}x{height}:r={fps}:d={c.out:.3f}[bg{i}]')
        fc.append(f"[bg{i}][fg{i}]overlay=x=(main_w-overlay_w)*{c.pos_x}/100:y=(main_h-overlay_h)*{c.pos_y}/100:shortest=1[v{i}]")
        if c.kind == 'video' and c.audio:
            f = [f'atrim=start={c.start}:end={c.end}']
            if c.reverse:
                f += ['areverse', 'asetpts=PTS-STARTPTS']
            else:
                f.append('asetpts=PTS-STARTPTS')
            f += atempo(c.speed)
            f += audio_fx_filters(c.denoise, c.denoise_mode, settings.get('rnnoise_model'),
                                  c.normalize, c.voice_enhance, c.pitch, c.noise_profile,
                                  c.noise_preset, c.hum_removal, c.wind_reduction, c.clean_mix)
            f.append(f'volume={c.volume / 100:.3f}')
            if c.audio_fade_in > 0:
                f.append(f'afade=t=in:st=0:d={c.audio_fade_in}')
            if c.audio_fade_out > 0:
                f.append(f'afade=t=out:st={max(0, c.out - c.audio_fade_out):.3f}:d={c.audio_fade_out}')
            f.append('aformat=sample_rates=48000:channel_layouts=stereo')
            fc.append(f'[{i}:a]{",".join(f)}[a{i}]')
        else:
            fc.append(f'anullsrc=r=48000:cl=stereo,atrim=0:{c.out:.3f},asetpts=PTS-STARTPTS[a{i}]')
    transition_specs = {'fade', 'dissolve', 'wipeleft', 'wiperight', 'wipeup', 'wipedown',
                        'slideleft', 'slideright', 'slideup', 'slidedown', 'circleopen',
                        'circleclose', 'zoomin', 'radial', 'smoothleft', 'smoothright',
                        'smoothup', 'smoothdown', 'pixelize'}
    active_transitions = [i for i in range(1, len(clips))
                          if clips[i].transition in transition_specs and clips[i].transition_duration > 0]
    T = sum(c.out for c in clips) - sum(min(clips[i].transition_duration,
            clips[i - 1].out, clips[i].out) for i in active_transitions)
    if not clips:
        T = max((m.get('offset', 0) + m.get('duration', 0) for m in music), default=0.1)
    T = max(T, 0.1)
    if clips:
        if active_transitions:
            # xfade combina los clips secuencialmente; cada clip trae su transición de entrada.
            for i in range(len(clips)):
                fc.append(f'[v{i}]settb=AVTB,format=yuv420p[xf{i}]')
            current, elapsed = 'xf0', clips[0].out
            for i in range(1, len(clips)):
                c = clips[i]
                if c.transition in transition_specs and c.transition_duration > 0:
                    d = min(c.transition_duration, clips[i - 1].out, c.out)
                    offset = max(0, elapsed - d)
                    outlabel = f'xfade{i}'
                    fc.append(f'[{current}][xf{i}]xfade=transition={c.transition}:duration={d:.3f}:offset={offset:.3f}[{outlabel}]')
                    elapsed = offset + c.out
                else:
                    outlabel = f'xfade{i}'
                    fc.append(f'[{current}][xf{i}]concat=n=2:v=1:a=0[{outlabel}]')
                    elapsed += c.out
                current = outlabel
            audio_cur = 'ac0'
            fc.append('[a0]anull[ac0]')
            for i in range(1, len(clips)):
                d = min(clips[i].transition_duration, clips[i - 1].out, clips[i].out) if i in active_transitions else 0
                label = f'across{i}'
                if d > 0:
                    fc.append(f'[{audio_cur}][a{i}]acrossfade=d={d:.3f}:c1=tri:c2=tri[{label}]')
                else:
                    fc.append(f'[{audio_cur}][a{i}]concat=n=2:v=0:a=1[{label}]')
                audio_cur = label
            fc.append(f'[{audio_cur}]anull[ac]')
            cur = current
        else:
            cat = ''.join(f'[v{i}][a{i}]' for i in range(len(clips)))
            fc.append(f'{cat}concat=n={len(clips)}:v=1:a=1[vc][ac]')
            cur = 'vc'
    else:
        if file_format not in ('MP3', 'WAV', 'AAC', 'M4A', 'FLAC'):
            raise ValueError('Añade un video o selecciona el formato MP3 para exportar solo audio.')
        fc.append(f'anullsrc=r=48000:cl=stereo,atrim=0:{T:.3f},asetpts=PTS-STARTPTS[ac]')
        cur = ''
    font = sysfont()
    for overlay in overlays if clips else []:
        path, start, end = overlay['path'], overlay['start'], overlay['end']
        if os.path.splitext(path)[1].lower() in IMG:
            a += ['-loop', '1', '-framerate', str(fps), '-t', f'{max(0.1, end - start):.3f}', '-i', path]
        else:
            a += ['-i', path]
        j, k = n, len(fc); n += 1
        source_start = overlay.get('source_start', 0)
        source_end = overlay.get('source_end')
        trim = f'trim=start={source_start}' + (f':end={source_end}' if source_end is not None else '')
        scale = max(1, int(width * overlay.get('scale', 25) / 100))
        fc.append(f'[{j}:v]{trim},setpts=PTS-STARTPTS+{start}/TB,scale={scale}:-1,format=rgba[ov{k}]')
        fc.append(f"[{cur}][ov{k}]overlay=x=(main_w-overlay_w)*{overlay.get('x', 75)}/100:y=(main_h-overlay_h)*{overlay.get('y', 15)}/100:enable='between(t,{start},{end})':eof_action=pass[x{k}]")
        cur = f'x{k}'
    for t in (texts if clips else []):
        if t.get('is_subtitle') and not settings.get('burn_subtitles', True):
            continue
        s0, e0 = t['start'], t['end']
        text_font = font
        if t.get('bold') and font and os.path.basename(font).lower() == 'arial.ttf':
            bold_font = os.path.join(os.path.dirname(font), 'arialbd.ttf')
            if os.path.exists(bold_font):
                text_font = bold_font
        style = []
        if text_font:
            style.append(f"fontfile='{esc(text_font)}'")
        style += [f"fontsize={t['size']}", f"fontcolor={t['color']}",
                  f"borderw={2 if t.get('border', True) else 0}", 'bordercolor=black@0.8']
        if t.get('shadow'):
            style += ['shadowx=3', 'shadowy=3', 'shadowcolor=black@0.8']
        if t.get('background'):
            style += ['box=1', f"boxcolor=0x{t.get('background_color', '#000000').lstrip('#')}@0.75", 'boxborderw=10']
        style = ':'.join(style)
        animation = t.get('animation', 'fade')
        parts = list(enumerate(t['text'], start=1)) if animation == 'write' else [(0, t['text'])]
        for idx, _ in parts:
            k = len(fc)
            fp = os.path.join(tmp, f't{k}.txt')
            with open(fp, 'w', encoding='utf-8') as fh:
                fh.write(t['text'][:idx] if animation == 'write' else t['text'])
            enable_start, enable_end = s0, e0
            xexpr = f'(w-text_w)*{t["x"]}/100'
            alpha = ''
            if animation == 'fade':
                alpha = f":alpha='if(lt(t,{s0}),0,min(1,(t-{s0})/0.4))'"
            elif animation == 'slide':
                xexpr = f'(w-text_w)*{t["x"]}/100-(1-min(1,max(0,(t-{s0})/0.5)))*w'
            elif animation == 'write':
                step = min(0.08, 1.2 / max(1, len(parts)))
                enable_start = s0 + (idx - 1) * step
                enable_end = e0 if idx == len(parts) else s0 + idx * step
            fc.append(f"[{cur}]drawtext=textfile='{esc(fp)}':{style}:x={xexpr}:y=(h-text_h)*{t['y']}/100"
                      f"{alpha}:enable='between(t,{enable_start:.3f},{enable_end:.3f})'[x{k}]")
            cur = f'x{k}'
    for s in stickers if clips else []:
        a += ['-loop', '1', '-framerate', str(fps), '-t', f'{T:.3f}', '-i', s['path']]
        j, k = n, len(fc)
        n += 1
        fc.append(f"[{j}:v]scale={int(width * s['w'] / 100)}:-1,format=rgba[s{k}]")
        fc.append(f"[{cur}][s{k}]overlay=x=(main_w-overlay_w)*{s['x']}/100:y=(main_h-overlay_h)*{s['y']}/100"
                  f":enable='between(t,{s['start']},{s['end']})'[y{k}]")
        cur = f'y{k}'
    labels = ['ac']
    audible = audible_audio_tracks(music)
    has_solo = any(m.get('solo') and not m.get('muted') for m in music)
    has_video_voice = any(c.kind == 'video' and c.audio for c in clips)

    def wants_duck(track):
        return (track.get('ducking', False) or settings.get('duck_music', False)) and \
               track.get('role', 'music') == 'music' and clips
    duck_count = sum(1 for m in music if m in audible and wants_duck(m))
    sidechain_labels = []
    voice_label = 'ac'
    if duck_count:
        # Una etiqueta de filtro solo se puede usar una vez: se reparte la voz con asplit.
        voice_label = 'acmix'
        sidechain_labels = [f'acsc{i}' for i in range(duck_count)]
        fc.append('[ac]asplit=%d' % (duck_count + 1) + ''.join(f'[{x}]' for x in [voice_label] + sidechain_labels))
    labels = [voice_label]
    if has_solo:
        if duck_count:
            fc.append(f'[{voice_label}]anullsink')
        labels = []
    for track_index, m in enumerate(music):
        if m not in audible:
            continue
        music_label = f'm{track_index}'
        n, music_label = append_audio_track(a, fc, m, n, T, music_label,
            normalize_override=has_video_voice and m.get('role', 'music') == 'music', settings=settings)
        if wants_duck(m):
            duck_label = f'duck{track_index}'
            sidechain = sidechain_labels.pop(0)
            fc.append(f'[{music_label}][{sidechain}]{duck_filter(m.get("duck_level", settings.get("duck_level", "normal")))}[{duck_label}]')
            music_label = duck_label
        labels.append(music_label)
    if len(labels) > 1:
        fc.append(''.join(f'[{l}]' for l in labels) +
                  f'amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.841395:attack=5:release=50:level=false[aout]')
    else:
        fc.append(f'[{labels[0] if labels else voice_label}]alimiter=limit=0.841395:attack=5:release=50:level=false[aout]')
    if file_format in ('GIF', 'PNG', 'JPG', 'JPEG') and cur:
        fc.append('[aout]anullsink')  # sin audio: FFmpeg rechaza salidas sin conectar
    elif file_format in ('MP3', 'WAV', 'AAC', 'M4A', 'FLAC') and cur:
        fc.append(f'[{cur}]nullsink')  # solo audio: descarta el video del filtergraph
    if file_format == 'GIF' and cur:
        # fps y escala dentro del mismo filtergraph: FFmpeg no permite mezclar -vf con -filter_complex.
        fc.append(f'[{cur}]fps={fps},scale={width}:-1:flags=lanczos[gifv]')
        cur = 'gifv'
    a += ['-filter_complex', ';'.join(fc)]
    if file_format == 'MP3':
        a += ['-map', '[aout]', '-vn', '-c:a', 'libmp3lame', '-q:a', str({'baja': 6, 'media': 4, 'alta': 2}.get(quality, 2)), out]
    elif file_format == 'GIF':
        a += ['-map', f'[{cur}]', '-an', '-loop', '0', out]
    elif file_format in ('PNG', 'JPG', 'JPEG'):
        a += ['-map', f'[{cur}]', '-an', '-frames:v', '1']
        if file_format != 'PNG':
            a += ['-q:v', '2']
        a.append(out)
    elif file_format == 'WEBM':
        a += ['-map', f'[{cur}]', '-map', '[aout]', '-c:v', 'libvpx-vp9', '-b:v', '0',
              '-crf', str(crf), '-pix_fmt', 'yuv420p', '-c:a', 'libopus', '-b:a', '160k', out]
    elif file_format in ('MP4_HEVC', 'HEVC'):
        a += ['-map', f'[{cur}]', '-map', '[aout]', '-c:v', 'libx265', '-crf', str(crf),
              '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', out]
    elif file_format == 'AVI':
        a += ['-map', f'[{cur}]', '-map', '[aout]', '-c:v', 'mpeg4', '-q:v', '4', '-pix_fmt', 'yuv420p',
              '-c:a', 'libmp3lame', '-q:a', '3', out]
    elif file_format == 'MOV':
        a += ['-map', f'[{cur}]', '-map', '[aout]', '-c:v', 'libx264', '-preset', preset,
              '-crf', str(crf), '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', out]
    elif file_format == 'MKV':
        a += ['-map', f'[{cur}]', '-map', '[aout]', '-c:v', 'libx264', '-preset', preset,
              '-crf', str(crf), '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', out]
    else:
        a += ['-map', f'[{cur}]', '-map', '[aout]', '-c:v', 'libx264', '-preset', preset, '-crf', str(crf),
              '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', out]
    return a, T


class Worker(QThread):
    prog = Signal(int)
    done = Signal(str)

    def __init__(s, cmd, total):
        super().__init__()
        s.cmd, s.total = cmd, max(total, 0.1)
        s.process = None; s.cancel_requested = False

    def cancel(s):
        s.cancel_requested = True
        if s.process and s.process.poll() is None:
            s.process.terminate()

    def run(s):
        p = subprocess.Popen(s.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding='utf-8', errors='replace', creationflags=NOWIN)
        s.process = p
        if s.cancel_requested:
            p.terminate()
        tail = []
        for line in p.stdout:
            if line.startswith('out_time_us='):
                try:
                    s.prog.emit(min(99, int(int(line.split('=')[1]) / 1e6 / s.total * 100)))
                except ValueError:
                    pass
            else:
                tail = (tail + [line])[-15:]
        p.wait()
        if s.cancel_requested:
            s.done.emit('__CANCELLED__')
        else:
            s.done.emit('' if p.returncode == 0 else ''.join(tail))


class AudioProfileWorker(QThread):
    progress = Signal(int)
    completed = Signal(object)

    def __init__(self, jobs):
        super().__init__(); self.jobs = jobs; self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        results = []
        for index, job in enumerate(self.jobs):
            if self.cancelled:
                break
            profile = analyze_noise_profile(job['path'], job['start'], job['duration'], job['settings'])
            results.append((index, profile))
            self.progress.emit(int((index + 1) * 100 / max(1, len(self.jobs))))
        self.completed.emit(results)


class LoudnormWorker(QThread):
    progress = Signal(int)
    completed = Signal(object)

    def __init__(self, jobs):
        super().__init__(); self.jobs = jobs

    def run(self):
        results = []
        ffmpeg = ffbin('ffmpeg')
        for index, job in enumerate(self.jobs):
            try:
                command = [ffmpeg, '-hide_banner', '-ss', f"{job['start']:.3f}", '-i', job['path']]
                if job.get('duration', 0) > 0:
                    command += ['-t', f"{job['duration']:.3f}"]
                command += ['-af', f"loudnorm=I={job['target']}:TP=-1.5:LRA=11:print_format=json", '-f', 'null', '-']
                run = subprocess.run(command, capture_output=True, text=True, creationflags=NOWIN, timeout=900)
                blocks = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', run.stderr, flags=re.S)
                stats = json.loads(blocks[-1]) if run.returncode == 0 and blocks else {}
            except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
                stats = {}
            results.append((job['index'], stats))
            self.progress.emit(int((index + 1) * 100 / max(1, len(self.jobs))))
        self.completed.emit(results)


class WhisperWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(object, str)

    def __init__(self, path, model_size, language, cache_dir):
        super().__init__(); self.path, self.model_size = path, model_size
        self.language, self.cache_dir, self.cancelled = language, cache_dir, False

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            from faster_whisper import WhisperModel
            self.progress.emit(3, 'Descargando o preparando Whisper en segundo plano…')
            model = WhisperModel(self.model_size, device='cpu', compute_type='int8', download_root=self.cache_dir)
            if self.cancelled:
                raise InterruptedError('Proceso cancelado.')
            self.progress.emit(10, 'Reconociendo la voz…')
            language = None if self.language == 'auto' else self.language
            segments, info = model.transcribe(self.path, language=language, vad_filter=True)
            found = []
            total = max(1.0, float(getattr(info, 'duration', 1.0) or 1.0))
            for segment in segments:
                if self.cancelled:
                    raise InterruptedError('Proceso cancelado.')
                found.append((segment.start, segment.end, segment.text))
                self.progress.emit(min(99, 10 + int(segment.end / total * 89)), 'Creando subtítulos…')
            self.completed.emit(found, '')
        except InterruptedError as exc:
            self.completed.emit(None, str(exc))
        except Exception as exc:
            self.completed.emit(None, f'{type(exc).__name__}: {exc}')


class BackgroundWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(str, str)

    def __init__(self, source, destination, fps=30):
        super().__init__(); self.source, self.destination = source, destination
        self.fps, self.cancelled = max(1, min(60, int(fps))), False

    def cancel(self):
        self.cancelled = True

    def run(self):
        folder = tempfile.mkdtemp(prefix='noisecut_remove_bg_')
        frames, transparent = os.path.join(folder, 'frames'), os.path.join(folder, 'alpha')
        try:
            from rembg import remove, new_session
            from PIL import Image
            os.makedirs(frames); os.makedirs(transparent)
            ffmpeg = ffbin('ffmpeg')
            if not ffmpeg:
                raise RuntimeError('No se encuentra FFmpeg.')
            result = subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', self.source,
                '-vsync', '0', os.path.join(frames, '%08d.png')], capture_output=True,
                text=True, creationflags=NOWIN)
            if result.returncode:
                raise RuntimeError(result.stderr[-1000:])
            paths = sorted(os.path.join(frames, name) for name in os.listdir(frames) if name.endswith('.png'))
            if not paths:
                raise RuntimeError('No se pudieron extraer cuadros del video.')
            self.progress.emit(15, 'Preparando el modelo ligero de segmentación…')
            session = new_session('u2netp')
            for i, path in enumerate(paths):
                if self.cancelled:
                    raise InterruptedError('Proceso cancelado.')
                with Image.open(path) as image:
                    result_png = remove(image.convert('RGBA'), session=session)
                with open(os.path.join(transparent, os.path.basename(path)), 'wb') as output:
                    output.write(result_png)
                self.progress.emit(15 + int((i + 1) * 75 / len(paths)), f'Procesando cuadro {i + 1} de {len(paths)}…')
            silent = os.path.join(folder, 'transparente.mov')
            result = subprocess.run([ffmpeg, '-y', '-v', 'error', '-framerate', str(self.fps),
                '-i', os.path.join(transparent, '%08d.png'), '-c:v', 'qtrle', '-pix_fmt', 'argb', silent],
                capture_output=True, text=True, creationflags=NOWIN)
            if result.returncode:
                raise RuntimeError(result.stderr[-1000:])
            self.progress.emit(95, 'Añadiendo el audio original…')
            result = subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', silent, '-i', self.source,
                '-map', '0:v:0', '-map', '1:a?', '-c:v', 'copy', '-c:a', 'aac', '-shortest', self.destination],
                capture_output=True, text=True, creationflags=NOWIN)
            if result.returncode:
                raise RuntimeError(result.stderr[-1000:])
            self.completed.emit(self.destination, '')
        except Exception as exc:
            self.completed.emit('', f'{type(exc).__name__}: {exc}')
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class StabilizeWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(str, str)

    def __init__(self, source, output):
        super().__init__(); self.source, self.output = source, output

    def run(self):
        folder = tempfile.mkdtemp(prefix='noisecut_stabilize_')
        trf = os.path.join(folder, 'movimiento.trf')
        try:
            detect, apply = stabilization_commands(ffbin('ffmpeg'), self.source, trf, self.output)
            self.progress.emit(10, 'Analizando el movimiento del video (paso 1 de 2)…')
            result = subprocess.run(detect, capture_output=True, text=True, creationflags=NOWIN)
            if result.returncode:
                raise RuntimeError(result.stderr[-1200:])
            self.progress.emit(55, 'Corrigiendo el movimiento (paso 2 de 2)…')
            result = subprocess.run(apply, capture_output=True, text=True, creationflags=NOWIN)
            if result.returncode:
                raise RuntimeError(result.stderr[-1200:])
            self.completed.emit(self.output, '')
        except Exception as exc:
            self.completed.emit('', f'{type(exc).__name__}: {exc}')
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def pick(b):
    c = QColorDialog.getColor(QColor(b.text()))
    if c.isValid():
        b.setText(c.name()); b.setStyleSheet(f'background:{c.name()};color:#888')


def ask(parent, title, spec, vals=None):
    d = QDialog(parent); d.setWindowTitle(title)
    f, w, vals = QFormLayout(d), {}, vals or {}
    for key, label, typ, dflt, *rng in spec:
        v = vals.get(key, dflt)
        if typ == 'text':
            e = QLineEdit(str(v))
        elif typ == 'color':
            e = QPushButton(v); e.setStyleSheet(f'background:{v};color:#888')
            e.clicked.connect(lambda _=0, b=e: pick(b))
        elif typ == 'bool':
            e = QCheckBox(); e.setChecked(bool(v))
        elif typ == 'choice':
            e = QComboBox()
            for caption, value in rng[0]:
                e.addItem(caption, value)
            e.setCurrentIndex(max(0, e.findData(v)))
        else:
            e = QDoubleSpinBox(); e.setRange(*rng); e.setDecimals(2 if typ == 'float' else 0); e.setValue(v)
        w[key] = (typ, e); f.addRow(label, e)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(d.accept); bb.rejected.connect(d.reject); f.addRow(bb)
    if not d.exec():
        return None
    out = {}
    for k, (typ, e) in w.items():
        if typ in ('text', 'color'):
            out[k] = e.text()
        elif typ == 'bool':
            out[k] = e.isChecked()
        elif typ == 'choice':
            out[k] = e.currentData()
        else:
            out[k] = int(e.value()) if typ == 'int' else e.value()
    return out


TEXT = [('text', 'Texto', 'text', 'Hola'), ('start', 'Inicio (s)', 'float', 0, 0, 3600),
        ('end', 'Fin (s)', 'float', 5, 0, 3600), ('x', 'Posición X %', 'int', 50, 0, 100),
        ('y', 'Posición Y %', 'int', 85, 0, 100), ('size', 'Tamaño', 'int', 64, 8, 400),
        ('color', 'Color', 'color', '#ffffff'), ('bold', 'Negrita', 'bool', False),
        ('border', 'Borde', 'bool', True), ('shadow', 'Sombra', 'bool', False),
        ('background', 'Fondo de color', 'bool', False),
        ('background_color', 'Color de fondo', 'color', '#000000'),
        ('animation', 'Animación', 'choice', 'fade',
         [('Aparecer', 'fade'), ('Deslizar', 'slide'), ('Escribir', 'write'), ('Sin animación', 'none')])]
STICK = [('start', 'Inicio (s)', 'float', 0, 0, 3600), ('end', 'Fin (s)', 'float', 5, 0, 3600),
         ('x', 'Posición X %', 'int', 85, 0, 100), ('y', 'Posición Y %', 'int', 15, 0, 100),
         ('w', 'Ancho (% del video)', 'int', 20, 1, 100)]
MUSIC = [('role', 'Tipo de pista', 'choice', 'music',
          [('Musica', 'music'), ('Efectos', 'effect'), ('Voz en off', 'voice')]),
         ('offset', 'Empieza en (s)', 'float', 0, 0, 3600),
         ('volume_db', 'Volumen (dB)', 'float', 0, -60, 12),
         ('clip_duration', 'Duracion en la linea (s)', 'float', 10, 0.1, 3600),
         ('fade_in', 'Aparecer (s)', 'float', 0, 0, 10),
         ('fade_out', 'Desaparecer (s)', 'float', 0, 0, 10),
         ('speed', 'Velocidad', 'float', 1, 0.25, 4),
         ('preserve_pitch', 'Conservar tono al cambiar velocidad', 'bool', True),
         ('normalize', 'Normalizar volumen', 'bool', False),
         ('voice_effect', 'Efecto de voz', 'choice', 'none',
          [('Sin efecto', 'none'), ('Grave', 'grave'), ('Agudo', 'agudo'),
           ('Robot', 'robot'), ('Eco', 'eco')]),
         ('loop', 'Repetir en bucle', 'bool', False),
         ('remove_leading_silence', 'Quitar silencio inicial', 'bool', False),
         ('ducking', 'Bajar con voz', 'bool', False),
         ('duck_level', 'Intensidad del ducking', 'choice', 'normal',
          [('Suave', 'soft'), ('Normal', 'normal'), ('Fuerte', 'strong')]),
         ('muted', 'Silenciar', 'bool', False), ('solo', 'Solo esta pista', 'bool', False),
         ('locked', 'Bloquear pista', 'bool', False), ('vol', 'Volumen anterior', 'float', 1.0, 0, 4),
         ('denoise', 'Reducir ruido (dB, 0 = off)', 'int', 0, 0, 40),
         ('denoise_mode', 'Modo de reducción', 'choice', 'standard',
          [('Estándar', 'standard'), ('IA · RNNoise', 'ai')]),
         ('voice_enhance', 'Mejorar voz', 'bool', False),
         ('pitch', 'Tono (semitonos)', 'int', 0, -12, 12)]
FIELDS = [('start', 'Inicio (s)', 0, 36000, 0.1, 2), ('end', 'Fin / duración (s)', 0.1, 36000, 0.1, 2),
          ('speed', 'Velocidad ×', 0.25, 4, 0.05, 2), ('bright', 'Brillo', -1, 1, 0.05, 2),
          ('contrast', 'Contraste', 0, 3, 0.05, 2), ('sat', 'Saturación', 0, 3, 0.05, 2),
          ('blur', 'Desenfoque', 0, 30, 0.5, 1), ('fade', 'Transición: fundido (s)', 0, 3, 0.1, 1)]
DESIGN = {'bg': '#000000', 'panel': '#111111', 'card': '#1C1C1F', 'accent': '#4381FF',
          'accent2': '#739FFF', 'text': '#E5E5E5', 'muted': '#888888', 'radius': '6px'}
STYLE = """QWidget{background:#000000;color:#E5E5E5;font-size:13px;font-family:'Segoe UI',sans-serif}
QMainWindow,QDialog{background:#000000}
QListWidget,QLineEdit,QDoubleSpinBox,QSpinBox,QComboBox{background:#111111;border:1px solid #262629;border-radius:6px;padding:6px;color:#E5E5E5}
QPushButton{background:#1C1C1F;border:1px solid #262629;border-radius:6px;padding:8px 16px;color:#E5E5E5}
QPushButton:hover{background:#262629;border-color:#4381FF}
QPushButton#primary{background:#4381FF;color:#FFFFFF;border:0;font-weight:600;border-radius:6px;padding:8px 24px}
QPushButton#primary:hover{background:#538CFF}
QListWidget::item:selected{background:#192744;border:1px solid #4381FF;border-radius:6px}
QListWidget#leftNavigation{background:#000000;border:0;border-right:1px solid #262629;border-radius:0px}
QGroupBox{background:#111111;border:1px solid #262629;border-radius:6px;margin-top:10px;padding:12px}
QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 6px;color:#888888;font-weight:600}
QTabWidget::pane{border:1px solid #262629;border-radius:6px;background:#111111;top:-1px}
QTabBar::tab{padding:8px 16px;background:#000000;border:1px solid transparent;border-bottom:1px solid #262629;color:#888888;font-weight:500}
QTabBar::tab:selected{background:#111111;color:#FFFFFF;border:1px solid #262629;border-bottom:none;border-top:2px solid #4381FF;border-top-left-radius:6px;border-top-right-radius:6px}
QProgressBar{background:#111111;border:1px solid #262629;border-radius:4px;text-align:center;color:#E5E5E5}
QProgressBar::chunk{background:#4381FF;border-radius:3px}"""
STYLE_LIGHT = """QWidget{background:#F7F8FA;color:#111111;font-size:13px;font-family:'Segoe UI',sans-serif}
QMainWindow,QDialog{background:#F7F8FA} 
QListWidget,QLineEdit,QDoubleSpinBox,QSpinBox,QComboBox{background:#FFFFFF;border:1px solid #E5E5E5;border-radius:6px;padding:6px}
QPushButton{background:#FFFFFF;border:1px solid #E5E5E5;border-radius:6px;padding:8px 16px;color:#111111}
QPushButton:hover{background:#F0F0F0;border-color:#4381FF} 
QPushButton#primary{background:#4381FF;color:#FFFFFF;border:0;font-weight:600;border-radius:6px}
QGroupBox{background:#FFFFFF;border:1px solid #E5E5E5;border-radius:6px;margin-top:10px;padding:12px}
QListWidget::item:selected{background:#EBF1FF;border:1px solid #4381FF;border-radius:6px}"""


class MediaGrid(QListWidget):
    hoverChanged = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent); self.owner = None; self.setAcceptDrops(True)

    def mimeData(self, items):
        mime = super().mimeData(items)
        urls = []
        for item in items:
            path = item.data(Qt.ItemDataRole.UserRole)
            if path:
                urls.append(QUrl.fromLocalFile(path))
        if urls:
            mime.setUrls(urls)
        return mime

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        if self.owner and event.mimeData().hasUrls():
            self.owner.add_media_paths([url.toLocalFile() for url in event.mimeData().urls()
                                        if url.isLocalFile()])
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        item = self.itemAt(event.position().toPoint())
        self.hoverChanged.emit(item, self.visualItemRect(item) if item else QRectF())

    def leaveEvent(self, event):
        self.hoverChanged.emit(None, QRectF())
        super().leaveEvent(event)


class TimelineBlock(QGraphicsRectItem):
    def __init__(self, rect, index, color, view):
        super().__init__(rect)
        self.index, self.view = index, view
        self.setBrush(QBrush(QColor(color)))
        self.setPen(QPen(QColor('#8bd5ff'), 2 if index == view.selected else 1))
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
        self.edge = None
        self.original_x = 0.0
        self.original_width = rect.width()
        self.press_x = 0.0

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            event.accept(); return
        x, width = event.pos().x(), self.rect().width()
        self.edge = 'left' if x < 8 else ('right' if x > width - 8 else None)
        self.original_x = self.pos().x()
        self.original_width = width
        self.press_x = event.scenePos().x()
        self.view.select_block(self.index)
        if self.view.owner.tl.item(self.index).data(Qt.UserRole).video_locked:
            self.edge = 'locked'; event.accept(); return
        if self.edge:
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.edge == 'locked':
            return
        if self.edge == 'right':
            delta = event.scenePos().x() - self.press_x
            self.setRect(0, 0, max(36, self.original_width + delta), self.rect().height())
            event.accept()
        elif self.edge == 'left':
            delta = max(-self.original_x, min(self.original_width - 36, event.scenePos().x() - self.press_x))
            self.setPos(self.original_x + delta, self.pos().y())
            self.setRect(0, 0, self.original_width - delta, self.rect().height())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.edge == 'locked':
            self.edge = None; event.accept(); return
        if self.edge:
            event.accept()
        else:
            super().mouseReleaseEvent(event)
        self.view.block_changed(self.index, self.pos().x(), self.rect().width(), self.edge,
                                self.original_x, self.original_width)
        self.edge = None

    def contextMenuEvent(self, event):
        self.view.owner.tl.setCurrentRow(self.index)
        menu = QMenu()
        for title, callback in (('Dividir', self.view.owner.split),
                                ('Duplicar', self.view.owner.dup),
                                ('Separar audio', self.view.owner.separate_audio),
                                ('Eliminar', self.view.owner.rm)):
            action = menu.addAction(title); action.triggered.connect(lambda _checked=False, fn=callback: fn())
        menu.exec(event.screenPos()); event.accept()


class AudioTimelineBlock(QGraphicsRectItem):
    def __init__(self, rect, index, color, view):
        super().__init__(rect)
        self.index, self.view = index, view
        self.setBrush(QBrush(QColor(color)))
        self.setPen(QPen(QColor('#55e5a0'), 2))
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)
        self.edge = None; self.press_x = 0.0; self.original_x = 0.0
        self.original_width = rect.width()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            event.accept(); return
        self.edge = 'left' if event.pos().x() < 8 else ('right' if event.pos().x() > self.rect().width() - 8 else None)
        track = self.view.owner.music[self.index]
        if track.get('locked'):
            event.accept(); self.edge = 'locked'; return
        self.original_x = self.pos().x(); self.original_width = self.rect().width()
        self.press_x = event.scenePos().x()
        self.view.owner.select_audio_track(self.index)
        event.accept()

    def mouseMoveEvent(self, event):
        if self.edge in ('locked', None):
            return
        delta = event.scenePos().x() - self.press_x
        if self.edge == 'left':
            delta = max(-self.original_x, min(self.original_width - 16, delta))
            self.setPos(self.original_x + delta, self.pos().y())
            self.setRect(0, 0, self.original_width - delta, self.rect().height())
        elif self.edge == 'right':
            self.setRect(0, 0, max(16, self.original_width + delta), self.rect().height())
        else:
            self.setPos(max(76, self.original_x + delta), self.pos().y())
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.edge not in ('locked', None):
            self.view.owner.audio_block_changed(self.index, self.pos().x(), self.rect().width(),
                self.edge, self.original_x, self.original_width)
        self.edge = None; event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(); actions = {}
        for key, title in (('split', 'Dividir en el cabezal'), ('duplicate', 'Duplicar'),
                           ('delete', 'Eliminar'), ('close', 'Eliminar y cerrar hueco'),
                           ('fit', 'Ajustar al video'), ('normalize', 'Alternar normalizacion'),
                           ('clean', 'Limpiar voz de la pista'), ('separate', 'Separar audio del video')):
            actions[key] = menu.addAction(title)
        duck_action = menu.addAction('Bajando con voz: ' + ('Activado' if self.view.owner.music[self.index].get('ducking') else 'Desactivado'))
        actions['duck'] = duck_action
        selected = menu.exec(event.screenPos())
        key = next((name for name, action in actions.items() if action == selected), None)
        if key:
            self.view.owner.audio_track_action(self.index, key)
        event.accept()


class TimelinePlayhead(QGraphicsLineItem):
    def __init__(self, view):
        super().__init__(0, 26, 0, 254)
        self.view = view
        self.setPen(QPen(QColor('#38bdf8'), 2))
        self.setFlag(QGraphicsLineItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsLineItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)

    def itemChange(self, change, value):
        if change == QGraphicsLineItem.GraphicsItemChange.ItemPositionChange:
            return QPointF(max(76, value.x()), 0)
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.view.seek_to(self.pos().x())


class TimelineView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setMinimumHeight(220)
        self.setMaximumHeight(400)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QBrush(QColor('#121316')))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setAcceptDrops(True)
        self.owner = None
        self.selected = -1
        self.scale = 72.0
        self.waveform_cache = {}
        self._items = []
        self.current_time = 0.0
        self.playhead = None

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        if not event.mimeData().hasUrls() or not self.owner:
            return super().dropEvent(event)
        scene_pos = self.mapToScene(event.pos())
        seconds = max(0.0, scene_pos.x() / self.scale)
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.splitext(path)[1].lower() in AUD:
                track = {k: default for k, title, typ, default, *_ in MUSIC}
                track['path'] = path
                track['offset'] = seconds
                try:
                    dur, _ = probe(path)
                except Exception:
                    dur = 5.0
                track['duration'] = track['clip_duration'] = max(0.1, dur)
                self.owner.music.append(track)
                for redraw in self.owner.track_refresh:
                    redraw()
                self.refresh()
                self.owner.refresh_media_used()
        event.acceptProposedAction()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier and self.owner:
            self.owner.zoom_timeline(1.15 if event.angleDelta().y() > 0 else 0.87)
            event.accept(); return
        super().wheelEvent(event)

    def refresh(self):
        self.scene().clear()
        self._items = []
        self.playhead = None
        if not self.owner:
            return
        clips = [self.owner.tl.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.owner.tl.count())]
        total = project_duration(clips, self.owner.music, self.owner.overlays,
                                 self.owner.texts, self.owner.stickers)
        total = max(total, 1)
        audio_end = max((m.get('offset', 0) + m.get('clip_duration', m.get('duration', 0))
                         for m in self.owner.music), default=0)
        display_total = max(total, audio_end)
        width = max(self.viewport().width(), int(76 + (display_total + 4) * self.scale))
        self.scene().setSceneRect(0, 0, width, 390)
        scene, font = self.scene(), self.font()
        font.setPointSize(8)
        for sec in range(int(width / self.scale) + 1):
            x = 76 + sec * self.scale
            scene.addLine(x, 24, x, 244, QPen(QColor('#2a2c33'), 1))
            label = scene.addText(f'{sec}s', font)
            label.setDefaultTextColor(QColor('#9ca3af')); label.setPos(x + 3, 2)
        for name, y in (('VIDEO', 36), ('VIDEO 2', 92), ('MUSICA', 137), ('EFECTOS', 187),
                        ('VOZ', 237), ('TEXTO', 307), ('STICKERS', 357)):
            label = scene.addText(name, font); label.setDefaultTextColor(QColor('#a3aab8')); label.setPos(4, y)
        x = 76.0
        for i, c in enumerate(clips):
            w = max(36.0, c.out * self.scale)
            block = TimelineBlock(QRectF(0, 0, w, 42), i, '#2563eb' if c.kind == 'video' else '#0f766e', self)
            block.setPos(x, 50)
            scene.addItem(block)
            clean_tag = '🔇 ' if c.denoise > 0 or c.voice_enhance else ''
            text_x = 7
            if w >= 112:
                icon = self.owner.media_icon(c.path)
                thumb = icon.pixmap(44, 34)
                if not thumb.isNull():
                    thumb_item = scene.addPixmap(thumb); thumb_item.setParentItem(block); thumb_item.setPos(4, 4)
                    text_x = 53
            available = max(8, int(w - text_x - 8))
            name = ('🔒 ' if c.video_locked else '') + ('OCULTO  ' if c.video_hidden else '') + clean_tag + os.path.basename(c.path)
            label = scene.addText(QFontMetrics(font).elidedText(name, Qt.TextElideMode.ElideRight, available), font)
            label.setDefaultTextColor(QColor('white')); label.setPos(text_x, 13); label.setParentItem(block)
            self._items.append(block)
            x += w + 4
        if not self.owner.overlays:
            hint = scene.addText('Arrastra aquí', font); hint.setDefaultTextColor(QColor('#505661')); hint.setPos(86, 103)
        for j, overlay in enumerate(self.owner.overlays):
            bx = 76 + overlay['start'] * self.scale
            bw = max(16, (overlay['end'] - overlay['start']) * self.scale)
            scene.addRect(bx, 98 + (j % 2) * 12, bw, 10, QPen(Qt.PenStyle.NoPen), QBrush(QColor('#38bdf8')))
        role_offsets = {'music': 0, 'effect': 50, 'voice': 100}
        role_counts = {'music': 0, 'effect': 0, 'voice': 0}
        for j, track in enumerate(self.owner.music):
            bx = 76 + track['offset'] * self.scale
            role = track.get('role', 'music')
            bw = max(24, min(width - bx, track.get('clip_duration', track.get('duration', 8)) * self.scale))
            y = 137 + role_offsets.get(role, 0) + role_counts.get(role, 0) * 30
            role_counts[role] = role_counts.get(role, 0) + 1
            block = AudioTimelineBlock(QRectF(0, 0, bw, 42), j,
                '#374151' if track.get('muted') else '#14594f', self)
            block.setPos(bx, y); scene.addItem(block)
            waveform = self.waveform_for(track.get('path', ''), int(max(100, min(1200, bw))))
            if waveform and not waveform.isNull():
                pix = waveform.scaled(max(1, int(bw - 4)), 36, Qt.AspectRatioMode.IgnoreAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
                item = scene.addPixmap(pix); item.setPos(bx + 2, y + 3)
            tag = {'music': 'MUSICA', 'effect': 'EFECTO', 'voice': 'VOZ'}.get(role, 'MUSICA')
            caption = tag + '  ' + os.path.basename(track.get('path', ''))
            if track.get('locked'):
                caption = 'BLOQUEADA  ' + caption
            if track.get('muted'):
                caption = 'SILENCIADA  ' + caption
            if track.get('solo'):
                caption = 'SOLO  ' + caption
            if track.get('ducking'):
                caption += '  |  BAJANDO CON VOZ'
            label = scene.addText(QFontMetrics(font).elidedText(caption, Qt.TextElideMode.ElideRight, max(16, int(bw - 12))), font)
            label.setDefaultTextColor(QColor('white')); label.setPos(bx + 6, y + 12); label.setZValue(3)
        end_x = 76 + total * self.scale
        if display_total > total:
            scene.addRect(end_x, 135, (display_total - total) * self.scale, 150,
                QPen(Qt.PenStyle.NoPen), QBrush(QColor(90, 90, 100, 60))).setZValue(4)
        scene.addLine(end_x, 22, end_x, 385,
                      QPen(QColor('#f87171'), 2, Qt.PenStyle.DashLine))
        end_label = scene.addText('Fin del video', font); end_label.setPos(79 + total * self.scale, 22)
        if not self.owner.music:
            hint = scene.addText('Arrastra aquí', font); hint.setDefaultTextColor(QColor('#505661')); hint.setPos(86, 151)
        if not self.owner.texts:
            hint = scene.addText('Arrastra aquí', font); hint.setDefaultTextColor(QColor('#505661')); hint.setPos(86, 307)
        for j, text in enumerate(self.owner.texts):
            bx = 76 + text['start'] * self.scale
            bw = max(16, (text['end'] - text['start']) * self.scale)
            scene.addRect(bx, 321 + (j % 2) * 12, bw, 10, QPen(Qt.PenStyle.NoPen), QBrush(QColor('#a855f7')))
        for j, sticker in enumerate(self.owner.stickers):
            bx = 76 + sticker['start'] * self.scale
            bw = max(16, (sticker['end'] - sticker['start']) * self.scale)
            scene.addRect(bx, 371 + (j % 2) * 12, bw, 10, QPen(Qt.PenStyle.NoPen), QBrush(QColor('#f59e0b')))
        for marker in getattr(self.owner, 'markers', []):
            mx = 76 + marker.get('time', 0) * self.scale
            scene.addLine(mx, 22, mx, 385, QPen(QColor('#fbbf24'), 2, Qt.PenStyle.DashLine))
            marker_label = scene.addText(marker.get('name', 'Marcador'), font)
            marker_label.setDefaultTextColor(QColor('#fbbf24')); marker_label.setPos(mx + 3, 18)
        self.playhead = TimelinePlayhead(self)
        scene.addItem(self.playhead)
        self.set_global_time(self.current_time)

    def fit_project(self):
        if not self.owner:
            return
        clips = [self.owner.tl.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.owner.tl.count())]
        total = max(1, project_duration(clips, self.owner.music, self.owner.overlays,
                                        self.owner.texts, self.owner.stickers))
        total = max(total, max((m.get('offset', 0) + m.get('clip_duration', m.get('duration', 0))
                                for m in self.owner.music), default=0))
        available = max(120, self.viewport().width() - 100)
        self.scale = max(2.0, min(240.0, available / (total + 4)))
        self.refresh()

    def waveform_for(self, path, width):
        if not path or not os.path.isfile(path):
            return QPixmap()
        key = (os.path.abspath(path), width, os.path.getmtime(path))
        if key in self.waveform_cache:
            return self.waveform_cache[key]
        pix = QPixmap()
        if os.path.splitext(path)[1].lower() == '.wav':
            try:
                with wave.open(path, 'rb') as audio:
                    count, channels = audio.getnframes(), audio.getnchannels()
                    raw = audio.readframes(count)
                    sample_width = audio.getsampwidth()
                if sample_width == 2 and count:
                    samples = struct.unpack('<' + 'h' * (len(raw) // 2), raw)
                    pix = QPixmap(width, 36); pix.fill(QColor('#14594f'))
                    painter = QPainter(pix); painter.setPen(QPen(QColor('#99f6e4'), 1))
                    per_pixel = max(1, count // width)
                    for x in range(width):
                        start, end = x * per_pixel * channels, min(len(samples), (x + 1) * per_pixel * channels)
                        peak = max((abs(v) for v in samples[start:end]), default=0) / 32768
                        half = max(1, int(peak * 17)); painter.drawLine(x, 18 - half, x, 18 + half)
                    painter.end()
            except (OSError, EOFError, wave.Error, struct.error):
                pass
        if pix.isNull() and ffbin('ffmpeg'):
            cache = os.path.join(tempfile.gettempdir(), 'noisecutstudio_waveforms')
            os.makedirs(cache, exist_ok=True)
            target = os.path.join(cache, f'{abs(hash(key))}.png')
            if not os.path.exists(target):
                try:
                    subprocess.run([ffbin('ffmpeg'), '-y', '-v', 'error', '-i', path,
                                    '-filter_complex', f'showwavespic=s={width}x36:colors=0x99f6e4',
                                    '-frames:v', '1', target], capture_output=True,
                                   creationflags=NOWIN, timeout=12)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if os.path.exists(target):
                pix.load(target)
        self.waveform_cache[key] = pix
        return pix

    def select_block(self, index):
        if self.owner and 0 <= index < self.owner.tl.count():
            self.selected = index
            self.owner.tl.setCurrentRow(index)
            self.set_selected(index)

    def set_selected(self, index):
        self.selected = index
        for block in self._items:
            block.setPen(QPen(QColor('#8bd5fc'), 2 if block.index == index else 1))

    def set_global_time(self, seconds):
        self.current_time = max(0, seconds)
        if self.playhead:
            self.playhead.setPos(76 + self.current_time * self.scale, 0)

    def seek_to(self, x):
        if not self.owner:
            return
        target = max(0, (x - 76) / self.scale)
        elapsed = 0.0
        for i in range(self.owner.tl.count()):
            clip = self.owner.tl.item(i).data(Qt.ItemDataRole.UserRole)
            if target <= elapsed + clip.out or i == self.owner.tl.count() - 1:
                self.owner.tl.setCurrentRow(i)
                local = max(0, min(clip.out, target - elapsed))
                self.owner.player.setPosition(int((clip.start + local * clip.speed) * 1000))
                self.owner.player.pause()
                self.set_global_time(target)
                return
            elapsed += clip.out

    def block_changed(self, index, x, width, edge, original_x, original_width):
        if not self.owner or not 0 <= index < self.owner.tl.count():
            return
        if edge:
            clip = self.owner.tl.item(index).data(Qt.ItemDataRole.UserRole)
            if edge == 'right':
                delta = (width - original_width) / self.scale * clip.speed
                clip.end = max(clip.start + 0.1, min(clip.src, clip.end + delta))
            else:
                delta = (x - original_x) / self.scale * clip.speed
                clip.start = max(0, min(clip.end - 0.1, clip.start + delta))
            self.owner.renumber()
        else:
            ordered = sorted(self._items, key=lambda item: item.pos().x())
            new_index = ordered.index(next(item for item in ordered if item.index == index))
            if new_index != index:
                item = self.owner.tl.takeItem(index)
                self.owner.tl.insertItem(new_index, item)
                self.owner.tl.setCurrentRow(new_index)
                for n, block in enumerate(ordered):
                    block.index = n
            self.owner.renumber()
        self.refresh()


class Main(QMainWindow):
    def __init__(s):
        super().__init__()
        s.setWindowTitle('NoiseCut Studio'); s.resize(1400, 860)
        s.setMinimumSize(1100, 720)
        s.app_settings = QSettings('NoiseCutStudio', 'NoiseCutStudio')
        s.light_theme = s.app_settings.value('light_theme', False, type=bool)
        QApplication.instance().setStyleSheet(STYLE_LIGHT if s.light_theme else STYLE)
        edition_file = resource_path('edition.txt')
        try:
            s.app_edition = open(edition_file, encoding='ascii').read().strip()
        except OSError:
            s.app_edition = 'Source'
        icon = resource_path('assets', 'icon.png')
        if os.path.exists(icon):
            s.setWindowIcon(QIcon(icon))
        s.texts, s.stickers, s.music, s.cur = [], [], [], None
        s.selected_audio_index = None
        s.overlays, s.markers = [], []
        s.export_settings = {'aspect': '16:9', 'resolution': 1080, 'fps': 30,
                             'quality': 'alta', 'format': 'MP4', 'background': 'negro',
                             'burn_subtitles': True, 'duck_music': False}
        s.track_refresh = []
        s.project_path = None
        s.history, s.history_index = [], -1
        s.player, s.aout, s.video = QMediaPlayer(), QAudioOutput(), QVideoWidget()
        s.player.setAudioOutput(s.aout); s.player.setVideoOutput(s.video)
        s.timeline_preview_path = None; s.timeline_preview_hash = None
        s.preview_cache_dir = os.path.join(tempfile.gettempdir(), 'noisecutstudio_timeline_previews')
        os.makedirs(s.preview_cache_dir, exist_ok=True)
        s.timeline_preview_worker = None; s.pending_preview = False; s.fallback_playback = False
        appdata = os.environ.get('LOCALAPPDATA', tempfile.gettempdir())
        s.app_data_dir = os.path.join(appdata, 'NoiseCutStudio')
        os.makedirs(s.app_data_dir, exist_ok=True)
        s.recovery_file = os.path.join(s.app_data_dir, 'recovery.ncs')
        s.ai_model_dir = os.path.join(appdata, 'NoiseCutStudio', 'models')
        bundled_rnnoise = resource_path('assets', 'rnnoise-conjoined-burgers.rnnn')
        s.ai_model_path = bundled_rnnoise if os.path.isfile(bundled_rnnoise) else os.path.join(
            s.ai_model_dir, 'rnnoise-conjoined-burgers.rnnn')
        s.whisper_model_dir = os.path.join(s.ai_model_dir, 'whisper')
        os.environ['U2NET_HOME'] = s.ai_model_dir
        s.has_vidstab = ffmpeg_has_filter('vidstabtransform')
        s.capture_session = QMediaCaptureSession(s)
        s.audio_input = QAudioInput()
        s.recorder = QMediaRecorder()
        s.capture_session.setAudioInput(s.audio_input); s.capture_session.setRecorder(s.recorder)
        s.recording_path = None; s.was_recording = False
        s.recorder.recorderStateChanged.connect(s.on_record_state)
        s.tl = QListWidget(s); s.tl.hide()
        s.tl.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        s.tl.currentItemChanged.connect(s.select)

        # Panel izquierdo: biblioteca y pistas auxiliares.
        left_tabs = QWidget(); left_tabs_l = QHBoxLayout(left_tabs); left_tabs_l.setContentsMargins(0, 0, 0, 0)
        left_nav = QListWidget(); left_nav.setFixedWidth(118); left_nav.setSpacing(3)
        left_nav.setObjectName('leftNavigation')
        left_pages = QStackedWidget()
        left_tabs_l.addWidget(left_nav); left_tabs_l.addWidget(left_pages, 1)
        def add_left_page(page, glyph, label):
            item = QListWidgetItem(s.label_icon(glyph), label); item.setSizeHint(QSize(108, 48))
            left_nav.addItem(item); left_pages.addWidget(page)
        left_nav.currentRowChanged.connect(left_pages.setCurrentIndex)
        media = QWidget(); media_l = QVBoxLayout(media)
        imp = QPushButton('＋  Importar'); imp.clicked.connect(s.import_media)
        add_media = QPushButton('Añadir seleccionado a la línea'); add_media.clicked.connect(s.add_selected_media)
        s.bin = MediaGrid(); s.bin.setViewMode(QListWidget.ViewMode.IconMode)
        s.bin.owner = s
        s.bin.setIconSize(QSize(88, 56)); s.bin.setGridSize(QSize(108, 88))
        s.bin.setResizeMode(QListWidget.ResizeMode.Adjust); s.bin.setMovement(QListWidget.Movement.Static)
        s.bin.setWordWrap(True); s.bin.setMouseTracking(True); s.bin.setDragEnabled(True); s.bin.itemDoubleClicked.connect(s.add_to_timeline)
        s.hovered_media = None
        s.media_plus = QToolButton(s.bin.viewport()); s.media_plus.setText('+')
        s.media_plus.setStyleSheet('background:#2563eb;color:white;border:0;border-radius:14px;font-size:18px;font-weight:bold')
        s.media_plus.resize(28, 28); s.media_plus.hide()
        s.media_plus.clicked.connect(lambda: s.add_to_timeline(s.hovered_media) if s.hovered_media else None)
        s.bin.hoverChanged.connect(s.media_hover)
        s.media_search = QLineEdit(); s.media_search.setPlaceholderText('Buscar en la biblioteca')
        s.media_filter = QComboBox()
        for caption, value in (('Todos los medios', 'all'), ('Videos', 'video'),
                               ('Audio', 'audio'), ('Imagenes', 'image')):
            s.media_filter.addItem(caption, value)
        s.media_search.textChanged.connect(s.filter_media)
        s.media_filter.currentIndexChanged.connect(s.filter_media)
        media_l.addWidget(imp); media_l.addWidget(add_media)
        media_l.addWidget(s.media_search); media_l.addWidget(s.media_filter)
        media_l.addWidget(QLabel('Doble clic o arrastra archivos aqui para importarlos.'))
        media_l.addWidget(s.bin, 1)
        add_left_page(media, 'M', 'Medios')
        s.audio_track = s.track(s.music, MUSIC, 'Audio', lambda x: f"{os.path.basename(x['path'])}  |  {x.get('role', 'music')}  |  {x.get('volume_db', 0)} dB" + ('  |  Bajando con voz' if x.get('ducking') else ''),
                                'Audio (*.mp3 *.wav *.m4a *.aac *.flac *.ogg)')
        s.text_track = s.track(s.texts, TEXT, 'Texto', lambda x: f"{x['text']}  ·  {x['start']}–{x['end']} s")
        s.sticker_track = s.track(s.stickers, STICK, 'Sticker', lambda x: f"{os.path.basename(x['path'])}  ·  {x['start']}–{x['end']} s",
                                  'Imágenes (*.png *.webp *.jpg *.jpeg)')
        audio_tools = QWidget(); audio_tools_l = QVBoxLayout(audio_tools)
        audio_tools_l.addWidget(s.audio_track, 1)
        s.duck_music = QCheckBox('Bajar música cuando hay voz');
        s.duck_music.setChecked(s.export_settings.get('duck_music', False))
        s.duck_music.toggled.connect(lambda value: s.set_export_setting('duck_music', value))
        audio_tools_l.addWidget(s.duck_music)
        s.record_button = QPushButton('●  Grabar voz en off'); s.record_button.clicked.connect(s.record_voiceover)
        s.record_status = QLabel('Graba con el micrófono y añade la toma como pista de audio.')
        audio_tools_l.addWidget(s.record_button); audio_tools_l.addWidget(s.record_status)
        add_left_page(audio_tools, '♫', 'Audio')
        add_left_page(s.make_ai_panel(), 'IA', 'IA')
        text_page = QWidget(); text_l = QVBoxLayout(text_page)
        auto_subtitles = QPushButton('Generar subtítulos con IA local'); auto_subtitles.clicked.connect(s.auto_subtitles)
        s.srt_button = QPushButton('Exportar subtitulos .srt'); s.srt_button.clicked.connect(s.export_srt)
        s.burn_subtitles = QCheckBox('Grabar los subtítulos en el video al exportar'); s.burn_subtitles.setChecked(True)
        s.burn_subtitles.toggled.connect(lambda value: s.set_export_setting('burn_subtitles', value))
        text_l.addWidget(auto_subtitles); text_l.addWidget(s.srt_button)
        text_l.addWidget(s.burn_subtitles); text_l.addWidget(s.text_track, 1)
        add_left_page(text_page, 'T', 'Texto')
        add_left_page(s.sticker_track, '◇', 'Stickers')
        add_left_page(s.make_filter_panel(), '✦', 'Filtros')
        settings_page = QWidget(); settings_l = QVBoxLayout(settings_page); settings_form = QFormLayout()
        s.setting_boxes = {}
        format_options = [('MP4 H.264', 'MP4'), ('MP4 H.265', 'MP4_HEVC'), ('MOV H.264', 'MOV'),
            ('MKV', 'MKV'), ('WebM VP9', 'WEBM'), ('AVI', 'AVI'), ('GIF', 'GIF'),
            ('Solo audio MP3', 'MP3'), ('Solo audio WAV', 'WAV'), ('Solo audio AAC', 'AAC'),
            ('Solo audio M4A', 'M4A'), ('Solo audio FLAC', 'FLAC'), ('Fotograma PNG', 'PNG'),
            ('Fotograma JPG', 'JPG')]
        encoders = available_encoders()
        required_encoders = {'MP4': ('libx264',), 'MP4_HEVC': ('libx265',), 'MOV': ('libx264',),
            'MKV': ('libx264',), 'WEBM': ('libvpx-vp9', 'libopus'), 'AVI': ('mpeg4', 'libmp3lame'),
            'GIF': ('gif',), 'MP3': ('libmp3lame',), 'WAV': ('pcm_s16le',), 'AAC': ('aac',),
            'M4A': ('aac',), 'FLAC': ('flac',), 'PNG': ('png',), 'JPG': ('mjpeg',)}
        if encoders is not None:
            format_options = [(label, value) for label, value in format_options
                              if all(name in encoders for name in required_encoders[value])]
        for key, label, options in (
            ('aspect', 'Lienzo', [('16:9', '16:9'), ('9:16', '9:16'), ('1:1', '1:1'), ('4:5', '4:5')]),
            ('resolution', 'Resolución', [('480p', 480), ('720p', 720), ('1080p', 1080), ('1440p', 1440), ('4K', 2160)]),
            ('fps', 'Fotogramas por segundo', [('24 fps', 24), ('25 fps', 25), ('30 fps', 30), ('50 fps', 50), ('60 fps', 60)]),
            ('quality', 'Calidad', [('Alta', 'alta'), ('Media', 'media'), ('Baja', 'baja')]),
            ('format', 'Formato', format_options),
            ('background', 'Fondo del lienzo', [('Negro', 'negro'), ('Desenfocado', 'desenfocado')])):
            combo = QComboBox()
            for caption, value in options:
                combo.addItem(caption, value)
            current = s.export_settings[key]
            combo.setCurrentIndex(max(0, combo.findData(current)))
            combo.currentIndexChanged.connect(lambda _i, k=key, box=combo: s.set_export_setting(k, box.currentData()))
            s.setting_boxes[key] = combo; settings_form.addRow(label, combo)
        settings_l.addLayout(settings_form); settings_l.addStretch()
        s.theme_toggle = QCheckBox('Usar tema claro'); s.theme_toggle.setChecked(s.light_theme)
        s.theme_toggle.toggled.connect(s.toggle_theme); settings_l.addWidget(s.theme_toggle)
        add_left_page(settings_page, '⚙', 'Ajustes')
        add_left_page(s.make_transition_panel(), '⇢', 'Transiciones')
        left_nav.setCurrentRow(0)

        # Centro: reproducción y vista previa de efectos.
        mid = QWidget(); ml = QVBoxLayout(mid)
        s.video.setMinimumHeight(360); s.video.setStyleSheet('background:#000;border-radius:6px')
        ml.addWidget(s.video, 1)
        ctl = QHBoxLayout(); s.play = QPushButton('▶  Reproducir todo'); s.play.clicked.connect(s.toggle)
        s.time_label = QLabel('00:00 / 00:00'); s.time_label.setMinimumWidth(100)
        s.seek = QSlider(Qt.Orientation.Horizontal); s.seek.sliderMoved.connect(s.seek_project_slider)
        s.player.positionChanged.connect(s.on_pos); s.player.durationChanged.connect(lambda d: s.seek.setRange(0, d))
        s.player.mediaStatusChanged.connect(s.on_media_status)
        first = QPushButton('|◀'); first.setToolTip('Ir al inicio'); first.clicked.connect(lambda: s.seek_project(0))
        back = QPushButton('−5 s'); back.clicked.connect(lambda: s.seek_project(s.timeline.current_time - 5))
        forward = QPushButton('+5 s'); forward.clicked.connect(lambda: s.seek_project(s.timeline.current_time + 5))
        last = QPushButton('▶|'); last.setToolTip('Ir al final'); last.clicked.connect(lambda: s.seek_project(s.project_total()))
        fullscreen = QPushButton('Pantalla completa'); fullscreen.clicked.connect(s.video.showFullScreen)
        s.preview_effects = QPushButton('Vista previa con efectos'); s.preview_effects.clicked.connect(s.render_preview)
        ctl.addWidget(first); ctl.addWidget(back); ctl.addWidget(s.play); ctl.addWidget(forward); ctl.addWidget(last)
        ctl.addWidget(s.seek, 1); ctl.addWidget(s.time_label); ctl.addWidget(fullscreen); ctl.addWidget(s.preview_effects)
        ml.addLayout(ctl)
        cleanbar = QVBoxLayout(); clean_row1 = QHBoxLayout(); clean_row2 = QHBoxLayout()
        s.clean_button = QPushButton('✦  Limpiar voz'); s.clean_button.setObjectName('primary')
        s.clean_button.setMinimumHeight(42); s.clean_button.clicked.connect(lambda: s.clean_voice())
        s.clean_scope = QComboBox(); s.clean_scope.addItem('Todos los clips', 'all'); s.clean_scope.addItem('Solo el seleccionado', 'selected')
        s.clean_level = QComboBox()
        for label, value in (('Suave · 8 dB', 8), ('Normal · 15 dB', 15), ('Fuerte · 25 dB', 25), ('Maximo · 30 dB', 30)):
            s.clean_level.addItem(label, value)
        s.clean_level.setCurrentIndex(1)
        s.clean_db = QSpinBox(); s.clean_db.setRange(0, 40); s.clean_db.setSuffix(' dB')
        s.clean_db.setValue(15); s.clean_db.setToolTip('Intensidad avanzada de reducción')
        s.clean_level.currentIndexChanged.connect(lambda _i: s.clean_db.setValue(int(s.clean_level.currentData())))
        s.clean_method = QComboBox()
        for label, value in (('Estandar', 'standard'), ('IA RNNoise', 'ai'),
                             ('DeepFilterNet no incluida', 'deep')):
            s.clean_method.addItem(label, value)
        s.clean_preset = QComboBox()
        for label in ('Voz en interior', 'Exterior con viento', 'Carro / ruido de carretera',
                      'Llamada / audio de WhatsApp', 'Podcast / locucion'):
            s.clean_preset.addItem(label, label)
        s.clean_hum = QCheckBox('Quitar zumbido'); s.clean_wind = QCheckBox('Reducir viento')
        s.clean_mix = QSlider(Qt.Orientation.Horizontal); s.clean_mix.setRange(0, 100); s.clean_mix.setValue(100)
        s.clean_mix.setFixedWidth(90); s.clean_mix.setToolTip('Mezcla entre audio original y procesado')
        s.preview_mode = QComboBox(); s.preview_mode.addItem('Escuchar: Después', 'after'); s.preview_mode.addItem('Escuchar: Antes', 'before')
        s.preview_mode.currentIndexChanged.connect(lambda _i: s.render_timeline_preview() if s.tl.count() else None)
        clean_row1.addWidget(s.clean_button); clean_row1.addWidget(s.clean_scope)
        clean_row1.addWidget(s.clean_method); clean_row1.addWidget(s.clean_level)
        clean_row1.addWidget(QLabel('Ajuste avanzado')); clean_row1.addWidget(s.clean_db); clean_row1.addStretch(1)
        clean_row2.addWidget(s.clean_preset); clean_row2.addWidget(s.clean_hum); clean_row2.addWidget(s.clean_wind)
        clean_row2.addWidget(QLabel('Mezcla IA')); clean_row2.addWidget(s.clean_mix)
        clean_row2.addStretch(1); clean_row2.addWidget(QLabel('Vista de audio')); clean_row2.addWidget(s.preview_mode)
        cleanbar.addLayout(clean_row1); cleanbar.addLayout(clean_row2)
        ml.addLayout(cleanbar)
        s.clean_report = QLabel('Ruido antes y despues: pendiente de medir al limpiar una voz.')
        s.clean_report.setStyleSheet('color:#8b93a5;padding-left:8px')
        ml.addWidget(s.clean_report)

        # Inspector derecho: solo muestra ajustes ya soportados por el motor.
        right_tabs = QTabWidget(); s.sp = {}
        video_page = QWidget(); video_l = QVBoxLayout(video_page); g = QGroupBox('Ajustes de imagen'); f = QFormLayout(g)
        for k, lab, lo, hi, st, dec in FIELDS:
            if k == 'speed':
                continue
            spn = QDoubleSpinBox(); spn.setRange(lo, hi); spn.setSingleStep(st); spn.setDecimals(dec)
            spn.valueChanged.connect(lambda v, k=k: s.setf(k, v)); s.sp[k] = spn; f.addRow(lab, spn)
        for key, label in (('video_locked', 'Bloquear clip de video'), ('video_hidden', 'Ocultar video')):
            control = QCheckBox(label); control.toggled.connect(lambda v, k=key: s.setf(k, v))
            s.sp[key] = control; f.addRow(control)
        for key, label, low, high, step, decimals in (
            ('scale', 'Escala (%)', 10, 200, 5, 0), ('pos_x', 'Posición X (%)', 0, 100, 2, 0),
            ('pos_y', 'Posición Y (%)', 0, 100, 2, 0), ('rotation', 'Rotación (°)', -180, 180, 5, 0),
            ('crop_left', 'Recorte izquierda (%)', 0, 45, 1, 0), ('crop_right', 'Recorte derecha (%)', 0, 45, 1, 0),
            ('crop_top', 'Recorte arriba (%)', 0, 45, 1, 0), ('crop_bottom', 'Recorte abajo (%)', 0, 45, 1, 0),
            ('opacity', 'Opacidad (%)', 0, 100, 5, 0)):
            spin = QDoubleSpinBox(); spin.setRange(low, high); spin.setSingleStep(step); spin.setDecimals(decimals)
            spin.valueChanged.connect(lambda v, k=key: s.setf(k, v)); s.sp[key] = spin; f.addRow(label, spin)
        for key, label in (('flip_h', 'Voltear horizontalmente'), ('flip_v', 'Voltear verticalmente'), ('reverse', 'Revertir reproducción')):
            check = QCheckBox(label); check.toggled.connect(lambda v, k=key: s.setf(k, v)); s.sp[key] = check; f.addRow(check)
        revert = QPushButton('Restablecer ajustes del clip'); revert.clicked.connect(s.revert_clip); f.addRow(revert)
        effects_box = QGroupBox('Efectos de imagen'); effects_form = QFormLayout(effects_box)
        for key, label, low, high in (('vignette', 'Viñeta', 0, 100), ('grain', 'Grano', 0, 100),
                                      ('sharpness', 'Nitidez', 0, 5), ('pixelate', 'Pixelar', 0, 40),
                                      ('hqdn3d', 'Reducir ruido de imagen', 0, 12)):
            control = QSpinBox(); control.setRange(low, high)
            control.valueChanged.connect(lambda value, k='effect_' + key: s.setf(k, value))
            s.sp['effect_' + key] = control; effects_form.addRow(label, control)
        for key, label in (('mirror', 'Espejo'), ('glitch', 'Glitch de color'),
                           ('black_white', 'Blanco y negro'), ('sepia', 'Sepia'),
                           ('auto_color', 'Correccion automatica de color')):
            control = QCheckBox(label); control.toggled.connect(lambda value, k='effect_' + key: s.setf(k, value))
            s.sp['effect_' + key] = control; effects_form.addRow(control)
        chroma = QCheckBox('Quitar fondo verde'); chroma.toggled.connect(lambda value: s.setf('chroma_enabled', value))
        color_button = QPushButton('#00ff00'); color_button.setStyleSheet('background:#00ff00;color:#111')
        color_button.clicked.connect(lambda: (pick(color_button), s.setf('chroma_color', color_button.text())))
        tolerance = QDoubleSpinBox(); tolerance.setRange(0.01, 1); tolerance.setSingleStep(0.05); tolerance.setValue(0.25)
        tolerance.valueChanged.connect(lambda value: s.setf('chroma_similarity', value))
        lut_button = QPushButton('Importar LUT (.cube)'); lut_button.clicked.connect(s.choose_lut)
        bg_button = QPushButton('Quitar fondo de persona · IA local'); bg_button.clicked.connect(s.remove_background)
        stabilize_button = QPushButton('Estabilizar video')
        stabilize_button.clicked.connect(s.stabilize_clip)
        stabilize_button.setVisible(s.has_vidstab)
        effects_form.addRow(chroma); effects_form.addRow('Color a quitar', color_button); effects_form.addRow('Tolerancia', tolerance)
        effects_form.addRow(lut_button); effects_form.addRow(bg_button)
        if s.has_vidstab:
            effects_form.addRow(stabilize_button)
        video_l.addWidget(g); video_l.addStretch()
        video_l.addWidget(effects_box)
        video_scroll = QScrollArea(); video_scroll.setWidgetResizable(True); video_scroll.setWidget(video_page)
        right_tabs.addTab(video_scroll, 'Video')
        audio_page = QWidget(); audio_l = QVBoxLayout(audio_page)
        noise = QGroupBox('Reducción de ruido de fondo'); nf = QFormLayout(noise)
        nf.addRow(QLabel('Estándar elimina ruido constante. IA usa un modelo RNNoise gratuito.'))
        nz = QSpinBox(); nz.setRange(0, 40); nz.setSuffix(' dB'); nz.valueChanged.connect(lambda v: s.setf('denoise', v)); s.sp['denoise'] = nz
        nf.addRow('Intensidad (0 = desactivada)', nz)
        mode = QComboBox(); mode.addItem('Estándar (afftdn)', 'standard'); mode.addItem('IA (RNNoise)', 'ai')
        mode.currentIndexChanged.connect(lambda _i: s.setf('denoise_mode', mode.currentData()))
        s.sp['denoise_mode'] = mode; nf.addRow('Método', mode)
        nf.addRow(QLabel('Para voz, prueba 12–20 dB. Valores altos pueden sonar metálicos.'))
        download = QPushButton('Elegir modelo RNNoise local'); download.clicked.connect(s.choose_rnnoise_model)
        nf.addRow(download)
        audio_form = QFormLayout()
        for key, label, low, high, step, decimals in (
            ('volume', 'Volumen del clip (%)', 0, 200, 5, 0),
            ('audio_fade_in', 'Fundido de entrada (s)', 0, 30, 0.1, 1),
            ('audio_fade_out', 'Fundido de salida (s)', 0, 30, 0.1, 1),
            ('pitch', 'Tono (semitonos)', -12, 12, 1, 0)):
            control = QDoubleSpinBox(); control.setRange(low, high); control.setSingleStep(step); control.setDecimals(decimals)
            control.valueChanged.connect(lambda v, k=key: s.setf(k, v)); s.sp[key] = control; audio_form.addRow(label, control)
        for key, label in (('normalize', 'Normalizar volumen'), ('voice_enhance', 'Mejorar voz (EQ + compresor)')):
            control = QCheckBox(label); control.toggled.connect(lambda v, k=key: s.setf(k, v)); s.sp[key] = control; audio_form.addRow(control)
        audio_l.addWidget(noise); audio_l.addLayout(audio_form)
        s.audio_props_card = QGroupBox('Pista de audio seleccionada')
        track_form = QFormLayout(s.audio_props_card)
        s.track_volume_db = QDoubleSpinBox(); s.track_volume_db.setRange(-60, 12); s.track_volume_db.setSuffix(' dB')
        s.track_volume_db.setValue(0); s.track_volume_db.valueChanged.connect(lambda v: s.set_audio_track_field('volume_db', v))
        s.track_volume_slider = QSlider(Qt.Orientation.Horizontal); s.track_volume_slider.setRange(0, 72); s.track_volume_slider.setValue(60)
        s.track_volume_slider.valueChanged.connect(lambda v: s.track_volume_db.setValue(v - 60))
        s.track_volume_db.valueChanged.connect(lambda v: s.track_volume_slider.setValue(round(v + 60)))
        s.track_volume_pct = QLabel('100%')
        s.track_volume_db.valueChanged.connect(lambda v: s.track_volume_pct.setText(f'{100 * db_to_linear(v):.1f}%'))
        volume_row = QWidget(); volume_layout = QHBoxLayout(volume_row); volume_layout.setContentsMargins(0, 0, 0, 0)
        volume_layout.addWidget(s.track_volume_slider, 1); volume_layout.addWidget(s.track_volume_db)
        track_form.addRow('Volumen', volume_row); track_form.addRow('Equivalente', s.track_volume_pct)
        for key, label in (('fade_in', 'Aparecer (s)'), ('fade_out', 'Desaparecer (s)')):
            control = QDoubleSpinBox(); control.setRange(0, 10); control.setSingleStep(0.1); control.setSuffix(' s')
            control.valueChanged.connect(lambda v, k=key: s.set_audio_track_field(k, v))
            setattr(s, 'track_' + key, control); track_form.addRow(label, control)
        s.track_speed = QDoubleSpinBox(); s.track_speed.setRange(0.25, 4); s.track_speed.setSingleStep(0.05)
        s.track_speed.setValue(1); s.track_speed.setSuffix('x')
        s.track_speed.valueChanged.connect(lambda v: s.set_audio_track_field('speed', v))
        track_form.addRow('Velocidad (tono conservado)', s.track_speed)
        for key, label in (('normalize', 'Normalizar'), ('denoise', 'Reducir ruido'),
                           ('voice_enhance', 'Mejorar voz'), ('loop', 'Repetir en bucle'),
                           ('remove_leading_silence', 'Quitar silencio inicial'),
                           ('ducking', 'Bajar cuando hay voz'), ('muted', 'Silenciar'),
                           ('solo', 'Solo esta pista'), ('locked', 'Bloquear pista')):
            control = QCheckBox(label)
            if key == 'denoise':
                control.toggled.connect(lambda v: s.set_audio_track_field('denoise', int(s.clean_db.value()) if v else 0))
            else:
                control.toggled.connect(lambda v, k=key: s.set_audio_track_field(k, v))
            setattr(s, 'track_' + key, control); track_form.addRow(control)
        s.track_duck_level = QComboBox()
        for caption, value in (('Suave', 'soft'), ('Normal', 'normal'), ('Fuerte', 'strong')):
            s.track_duck_level.addItem(caption, value)
        s.track_duck_level.currentIndexChanged.connect(lambda _: s.set_audio_track_field('duck_level', s.track_duck_level.currentData()))
        track_form.addRow('Intensidad de ducking', s.track_duck_level)
        s.track_role = QComboBox()
        for caption, value in (('Musica', 'music'), ('Efectos', 'effect'), ('Voz en off', 'voice')):
            s.track_role.addItem(caption, value)
        s.track_role.currentIndexChanged.connect(lambda _: s.set_audio_track_field('role', s.track_role.currentData()))
        track_form.addRow('Tipo de pista', s.track_role)
        s.track_voice_effect = QComboBox()
        voice_effects = [('Sin efecto', 'none'), ('Grave', 'grave'), ('Agudo', 'agudo')]
        if ffmpeg_has_filter('aecho'):
            voice_effects.extend((('Robot', 'robot'), ('Eco', 'eco')))
        for caption, value in voice_effects:
            s.track_voice_effect.addItem(caption, value)
        s.track_voice_effect.currentIndexChanged.connect(lambda _: s.set_audio_track_field('voice_effect', s.track_voice_effect.currentData()))
        track_form.addRow('Cambiador de voz', s.track_voice_effect)
        adjust_track = QPushButton('Ajustar al video'); adjust_track.clicked.connect(lambda: s.audio_track_action(getattr(s, 'selected_audio_index', -1), 'fit'))
        reset_track = QPushButton('Restablecer pista'); reset_track.clicked.connect(s.reset_audio_track)
        track_form.addRow(adjust_track); track_form.addRow(reset_track)
        s.audio_props_card.setEnabled(False); audio_l.addWidget(s.audio_props_card)
        s.audio_warning = QLabel(''); s.audio_warning.setWordWrap(True); audio_l.addWidget(s.audio_warning)
        audio_l.addStretch(); right_tabs.addTab(audio_page, 'Audio')
        speed_page = QWidget(); speed_l = QVBoxLayout(speed_page); sf = QFormLayout()
        speed = QDoubleSpinBox(); speed.setRange(0.25, 4); speed.setSingleStep(0.05); speed.setDecimals(2); speed.setSuffix('×')
        speed.valueChanged.connect(lambda v: s.setf('speed', v)); s.sp['speed'] = speed
        sf.addRow('Velocidad del clip', speed); speed_l.addLayout(sf); speed_l.addStretch(); right_tabs.addTab(speed_page, 'Velocidad')

        body = QSplitter(Qt.Orientation.Horizontal); body.addWidget(left_tabs); body.addWidget(mid); body.addWidget(right_tabs)
        body.setSizes([380, 700, 340]); body.setStretchFactor(1, 1)
        # Barra superior con acciones frecuentes.
        header = QWidget(); header_l = QHBoxLayout(header); header_l.setContentsMargins(8, 4, 8, 4)
        brand = QLabel('  NoiseCut Studio'); brand.setStyleSheet('font-size:18px;font-weight:700;color:#f8fafc')
        icon_small = QIcon(icon) if os.path.exists(icon) else QIcon()
        icon_label = QLabel(); icon_label.setPixmap(icon_small.pixmap(32, 32))
        s.project_title = QLabel('Proyecto sin guardar'); s.project_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        s.autosave_label = QLabel('Autoguardado listo')
        undo = QPushButton('↶  Deshacer'); undo.clicked.connect(s.undo)
        redo = QPushButton('↷  Rehacer'); redo.clicked.connect(s.redo)
        save = QPushButton('Guardar'); save.clicked.connect(s.save_project)
        open_ = QPushButton('Abrir'); open_.clicked.connect(s.open_project)
        export = QPushButton('Exportar'); export.setObjectName('primary'); export.clicked.connect(s.export)
        header_l.addWidget(icon_label); header_l.addWidget(brand); header_l.addStretch(1); header_l.addWidget(s.project_title, 2); header_l.addWidget(s.autosave_label); header_l.addStretch(1)
        for button in (undo, redo, save, open_, export):
            header_l.addWidget(button)

        timeline_box = QWidget(); timeline_l = QVBoxLayout(timeline_box); timeline_l.setContentsMargins(0, 2, 0, 0)
        timeline_bar = QHBoxLayout(); timeline_bar.addWidget(QLabel('LÍNEA DE TIEMPO'))
        for label, fn in (('− Zoom', lambda: s.zoom_timeline(0.8)), ('+ Zoom', lambda: s.zoom_timeline(1.25)),
                          ('Ajustar a ventana', lambda: s.timeline.fit_project()),
                          ('✂ Dividir', s.split), ('⧉ Duplicar', s.dup), ('Congelar fotograma', s.freeze_frame),
                          ('Quitar silencios', s.remove_silence), ('Marcador', s.add_marker),
                          ('Separar audio', s.separate_audio),
                          ('＋ Superpuesto', s.add_overlay), ('Eliminar', s.rm)):
            button = QPushButton(label); button.clicked.connect(fn); timeline_bar.addWidget(button)
        timeline_bar.addStretch(); timeline_l.addLayout(timeline_bar)
        s.timeline = TimelineView(); s.timeline.owner = s; timeline_l.addWidget(s.timeline)

        root = QWidget(); root_l = QVBoxLayout(root); root_l.setContentsMargins(10, 8, 10, 8)
        root_l.addWidget(header); root_l.addWidget(body, 1); root_l.addWidget(timeline_box)
        s.setCentralWidget(root)
        menu = s.menuBar().addMenu('Proyecto')
        for label, fn, shortcut in (('Nuevo proyecto', s.new_project, 'Ctrl+N'),
                                    ('Abrir proyecto…', s.open_project, 'Ctrl+O'),
                                    ('Guardar proyecto', s.save_project, 'Ctrl+S'),
                                    ('Guardar proyecto como…', s.save_project_as, 'Ctrl+Shift+S')):
            act = QAction(label, s); act.setShortcut(shortcut); act.triggered.connect(fn); menu.addAction(act)
        edit = s.menuBar().addMenu('Editar')
        for label, fn, shortcut in (('Deshacer', s.undo, 'Ctrl+Z'), ('Rehacer', s.redo, 'Ctrl+Y')):
            act = QAction(label, s); act.setShortcut(shortcut); act.triggered.connect(fn); edit.addAction(act)
        help_menu = s.menuBar().addMenu('Ayuda')
        shortcuts = (('Espacio', 'Reproducir o pausar'), ('S', 'Dividir clip'),
                     ('Supr', 'Eliminar clip'), ('Ctrl+Z / Ctrl+Y', 'Deshacer / rehacer'),
                     ('Ctrl+S', 'Guardar proyecto'), ('Ctrl+E', 'Exportar'),
                     ('J / K / L', 'Retroceder / reproducir / avanzar'),
                     ('Flechas', 'Avanzar o retroceder un fotograma'),
                     ('Ctrl + rueda', 'Acercar o alejar la linea de tiempo'))
        help_action = QAction('Atajos de teclado', s)
        help_action.triggered.connect(lambda: QMessageBox.information(s, 'Atajos de teclado',
            '\n'.join(f'{key}: {description}' for key, description in shortcuts)))
        help_menu.addAction(help_action)
        for key, callback in (('Space', s.toggle), ('S', s.split), ('Delete', s.rm),
                              ('Ctrl+E', s.export),
                              ('J', lambda: s.seek_project(s.timeline.current_time - 5)),
                              ('K', s.toggle), ('L', lambda: s.seek_project(s.timeline.current_time + 5)),
                              ('Left', lambda: s.seek_project(s.timeline.current_time - 1 / s.export_settings.get('fps', FPS))),
                              ('Right', lambda: s.seek_project(s.timeline.current_time + 1 / s.export_settings.get('fps', FPS)))):
            shortcut = QShortcut(QKeySequence(key), s); shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
        s.remember()
        if not ffbin('ffmpeg') or not ffbin('ffprobe'):
            QMessageBox.warning(s, 'Falta FFmpeg', 'No se encuentran FFmpeg y FFprobe. Instala FFmpeg (Windows: winget install ffmpeg) y reinicia la app.')
        s.offer_recovery()
        s.autosave_timer = QTimer(s); s.autosave_timer.timeout.connect(s.autosave_recovery)
        s.autosave_timer.start(60000)

    def label_icon(s, text):
        icon_names = {'M': 'media', '♫': 'audio', 'IA': 'ai', 'T': 'text',
                      '◇': 'stickers', '✦': 'filters', '⚙': 'settings', '⇢': 'transitions'}
        svg = resource_path('assets', 'icons', icon_names.get(text, ''))
        if os.path.isfile(svg):
            renderer = QSvgRenderer(svg); pix = QPixmap(28, 28); pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix); renderer.render(painter, QRectF(2, 2, 24, 24)); painter.end()
            return QIcon(pix)
        pix = QPixmap(24, 24); pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor('#7dd3fc')); font = QFont(); font.setBold(True); font.setPointSize(12)
        painter.setFont(font); painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, text); painter.end()
        return QIcon(pix)

    def toggle_theme(s, enabled):
        s.light_theme = bool(enabled)
        s.app_settings.setValue('light_theme', s.light_theme)
        QApplication.instance().setStyleSheet(STYLE_LIGHT if s.light_theme else STYLE)

    def track(s, items, spec, label, fmt, filt=None):
        w = QWidget(); l = QVBoxLayout(w); lst = QListWidget(); l.addWidget(lst); row = QHBoxLayout()

        def redraw():
            lst.clear(); lst.addItems([fmt(x) for x in items])

        def new(path=None):
            base = {}
            if filt:
                path = path or QFileDialog.getOpenFileName(s, label, '', filt)[0]
                if not path:
                    return
                base['path'] = path
            defaults = {}
            if label == 'Audio' and base.get('path'):
                try:
                    base['duration'] = probe(base['path'])[0] if ffbin('ffprobe') else 0
                except (OSError, ValueError, json.JSONDecodeError):
                    base['duration'] = 0
                defaults['clip_duration'] = base['duration']
            r = ask(s, label, spec, defaults)
            if r is not None:
                s.remember(); r.update(base); items.append(r); redraw(); s.timeline.refresh()
                if label == 'Audio': lst.setCurrentRow(len(items) - 1)
                s.refresh_media_used()
                if r.get('denoise_mode') == 'ai' and not os.path.isfile(s.ai_model_path):
                    QMessageBox.information(s, 'RNNoise', 'Elige un modelo RNNoise local desde el panel Audio.')

        def edit(it):
            i = lst.row(it); r = ask(s, label, spec, items[i])
            if r is not None:
                s.remember(); items[i].update(r); redraw(); s.timeline.refresh()

        def delete():
            if lst.currentRow() >= 0:
                s.remember(); items.pop(lst.currentRow()); redraw(); s.timeline.refresh(); s.refresh_media_used()
        add, rmb = QPushButton('＋ Añadir'), QPushButton('Eliminar')
        add.clicked.connect(lambda: new()); rmb.clicked.connect(delete); lst.itemDoubleClicked.connect(edit)
        row.addWidget(add); row.addWidget(rmb); l.addLayout(row)
        s.track_refresh.append(redraw)
        if filt and label == 'Audio':
            s.audio_list = lst
            lst.currentRowChanged.connect(s.select_audio_track)
        w.add_path = new
        if filt and label == 'Audio':
            s.add_music = new
        return w

    def import_media(s):
        ps, _ = QFileDialog.getOpenFileNames(s, 'Importar', '', 'Medios (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.png *.jpg *.jpeg *.bmp *.webp *.mp3 *.wav *.m4a *.aac *.flac *.ogg)')
        s.add_media_paths(ps)

    def add_media_paths(s, ps):
        for p in ps:
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(p)[1].lower()
            duration = ''
            if (ext in VID or ext in AUD) and ffbin('ffprobe'):
                try:
                    duration = f'\n{probe(p)[0]:.1f} s'
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            it = QListWidgetItem(s.media_icon(p), os.path.basename(p) + duration)
            it.setData(Qt.ItemDataRole.UserRole, p); it.setData(Qt.ItemDataRole.UserRole + 1, os.path.basename(p) + duration)
            it.setToolTip(p); s.bin.addItem(it)
        s.filter_media(); s.refresh_media_used()

    def filter_media(s, *_):
        if not hasattr(s, 'bin'):
            return
        query = s.media_search.text().casefold() if hasattr(s, 'media_search') else ''
        kind = s.media_filter.currentData() if hasattr(s, 'media_filter') else 'all'
        for i in range(s.bin.count()):
            item = s.bin.item(i); path = item.data(Qt.ItemDataRole.UserRole)
            ext = os.path.splitext(path)[1].lower()
            item.setHidden((query not in os.path.basename(path).casefold()) or
                           (kind == 'video' and ext not in VID) or
                           (kind == 'audio' and ext not in AUD) or
                           (kind == 'image' and ext not in IMG))

    def refresh_media_used(s):
        if not hasattr(s, 'bin') or not hasattr(s, 'tl'):
            return
        used = {s.tl.item(i).data(Qt.UserRole).path for i in range(s.tl.count())}
        used.update(track.get('path') for track in s.music)
        for i in range(s.bin.count()):
            item = s.bin.item(i); path = item.data(Qt.ItemDataRole.UserRole)
            base = item.data(Qt.ItemDataRole.UserRole + 1)
            item.setText(('Añadido  |  ' if path in used else '') + base)

    def media_icon(s, path):
        ext = os.path.splitext(path)[1].lower()
        cache = os.path.join(tempfile.gettempdir(), 'noisecutstudio_thumbnails')
        os.makedirs(cache, exist_ok=True)
        thumb = os.path.join(cache, f'{abs(hash(os.path.abspath(path)))}.png')
        if not os.path.exists(thumb):
            if ext in IMG:
                pix = QPixmap(path)
                if not pix.isNull():
                    pix.scaled(224, 144, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation).save(thumb)
            elif ext in VID and ffbin('ffmpeg'):
                try:
                    subprocess.run([ffbin('ffmpeg'), '-y', '-ss', '1', '-i', path, '-frames:v', '1',
                                    '-vf', 'scale=224:144:force_original_aspect_ratio=decrease', thumb],
                                   capture_output=True, creationflags=NOWIN, timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            elif ext in AUD and ffbin('ffmpeg'):
                try:
                    subprocess.run([ffbin('ffmpeg'), '-y', '-v', 'error', '-i', path,
                        '-filter_complex', 'showwavespic=s=224x144:colors=0x39d98a',
                        '-frames:v', '1', thumb], capture_output=True,
                        creationflags=NOWIN, timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        pix = QPixmap(thumb) if os.path.exists(thumb) else QPixmap()
        if pix.isNull():
            return s.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        return QIcon(pix)

    def add_selected_media(s):
        it = s.bin.currentItem()
        if it:
            s.add_to_timeline(it)
        else:
            QMessageBox.information(s, 'Añadir medios', 'Selecciona un archivo de la biblioteca.')

    def media_hover(s, item, rect):
        s.hovered_media = item
        if item:
            s.media_plus.move(rect.right() - 32, rect.top() + 4)
            s.media_plus.show(); s.media_plus.raise_()
        else:
            s.media_plus.hide()

    def make_filter_panel(s):
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addWidget(QLabel('Presets creados con los ajustes disponibles del editor.'))
        for name, values in (('Vívido', (0.04, 1.18, 1.45)), ('Cálido', (0.06, 1.08, 1.12)),
                             ('Frío', (-0.02, 1.12, 0.88)), ('Blanco y negro', (0, 1.05, 0)),
                             ('Cine', (-0.04, 1.22, 0.82))):
            button = QPushButton(name); button.setMinimumHeight(44)
            button.clicked.connect(lambda _=False, n=name, v=values: s.apply_preset(n, v)); layout.addWidget(button)
        layout.addStretch(); return w

    def make_ai_panel(s):
        page = QWidget(); layout = QVBoxLayout(page)
        layout.addWidget(QLabel('Herramientas locales: los modelos se descargan cuando los usas.'))
        s.ai_cards = {}
        cards = (
            ('whisper', '▤  Subtítulos automáticos', 'Reconoce voz sin enviar el audio a internet.',
             lambda: s.auto_subtitles(), 'faster_whisper', 'No incluido en esta edición.'),
            ('background', '◉  Quitar fondo', 'Segmenta a la persona cuadro a cuadro; puede tardar.',
             lambda: s.remove_background(), 'rembg', 'No incluido en esta edición.'),
            ('rnnoise', '♫  Limpiar voz con IA (RNNoise)', 'Usa un modelo RNNoise local cuando esté disponible.',
             lambda: s.clean_voice(ai=True), None, 'Modelo no incluido en esta version.'),
        )
        for key, title, description, action, dependency, absent in cards:
            card = QGroupBox(title); card_l = QVBoxLayout(card)
            card_l.addWidget(QLabel(description))
            status = QLabel(); status.setStyleSheet('color:#9ca3af')
            available = True
            if key == 'rnnoise':
                ready = os.path.exists(s.ai_model_path)
                status.setText('Modelo local elegido' if ready else absent)
            elif s.app_edition == 'Lite':
                status.setText('Esta version no incluye esta funcion. Usa NoiseCut Studio Completa.')
                available = False
            elif importlib.util.find_spec(dependency):
                status.setText('Complemento instalado; modelo local bajo demanda.')
            else:
                status.setText('Esta version no incluye esta funcion. Revisa el paquete Completo.')
                available = False
            progress = QProgressBar(); progress.setRange(0, 100); progress.setValue(0)
            button = QPushButton('Abrir herramienta'); button.clicked.connect(action); button.setEnabled(available)
            cancel = QPushButton('Cancelar'); cancel.setEnabled(False)
            cancel.clicked.connect(lambda _=False, k=key: s.cancel_ai_worker(k))
            card_l.addWidget(status); card_l.addWidget(progress); card_l.addWidget(button); card_l.addWidget(cancel)
            layout.addWidget(card); s.ai_cards[key] = {'status': status, 'progress': progress, 'cancel': cancel}
        layout.addStretch(1)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page)
        return scroll

    def cancel_ai_worker(s, key):
        worker_name = {'whisper': 'speech_worker', 'background': 'bg_worker', 'rnnoise': 'model_worker'}[key]
        worker = getattr(s, worker_name, None)
        if worker and worker.isRunning():
            worker.cancel()
        progress = getattr(s, {'whisper': 'speech_progress', 'background': 'bg_progress',
                              'rnnoise': 'model_progress'}[key], None)
        if progress:
            progress.cancel()

    def clean_voice(s, ai=False):
        selected_only = s.clean_scope.currentData() == 'selected'
        selected_track = getattr(s, 'selected_audio_index', None)
        if selected_only and selected_track is not None and selected_track < len(s.music):
            clips = []
            music_targets = [s.music[selected_track]]
        elif selected_only:
            current = s.tl.currentItem()
            if not current:
                return QMessageBox.information(s, 'Limpiar voz', 'Selecciona primero un clip con audio.')
            clips = [current.data(Qt.ItemDataRole.UserRole)]
            music_targets = []
        else:
            clips = [s.tl.item(i).data(Qt.ItemDataRole.UserRole) for i in range(s.tl.count())]
            music_targets = s.music
        targets = [c for c in clips if c.kind == 'video' and c.audio]
        if not targets and not music_targets:
            return QMessageBox.information(s, 'Limpiar voz', 'No hay clips con audio en la selección.')
        level = int(s.clean_db.value())
        mode = s.clean_method.currentData()
        if ai:
            mode = 'ai'
        if mode == 'deep':
            QMessageBox.information(s, 'Máxima calidad',
                'DeepFilterNet no está incluido en esta versión; se aplicará el método estándar.')
            mode = 'standard'
        use_ai = mode == 'ai' and os.path.exists(s.ai_model_path)
        if mode == 'ai' and not use_ai:
            QMessageBox.information(s, 'RNNoise',
                'Este paquete no incluye un modelo RNNoise con licencia de redistribución verificada. '
                'Se aplicará el método estándar.')
            mode = 'standard'
        s.remember()
        for clip in targets:
            clip.denoise = level
            clip.denoise_mode = 'ai' if use_ai else 'standard'
            clip.voice_enhance = True
            clip.normalize = True
            clip.noise_preset = s.clean_preset.currentData()
            clip.hum_removal = s.clean_hum.isChecked()
            clip.wind_reduction = s.clean_wind.isChecked()
            clip.clean_mix = s.clean_mix.value() / 100
        for track in music_targets:
            track.update(denoise=level, denoise_mode='ai' if use_ai else 'standard',
                         voice_enhance=True, normalize=True,
                         noise_preset=s.clean_preset.currentData(),
                         hum_removal=s.clean_hum.isChecked(), wind_reduction=s.clean_wind.isChecked(),
                         clean_mix=s.clean_mix.value() / 100)
        if music_targets and s.track_refresh:
            s.track_refresh[0]()
        s.renumber()
        if s.tl.currentItem():
            s.select(s.tl.currentItem())
        if getattr(s, 'selected_audio_index', None) is not None:
            s.select_audio_track(s.selected_audio_index)
        if use_ai:
            card = s.ai_cards.get('rnnoise')
            if card:
                card['status'].setText('Modelo listo; limpieza IA aplicada.')
        jobs, targets_for_analysis = [], targets + music_targets
        for target in targets_for_analysis:
            if isinstance(target, Clip):
                start, duration = target.start, target.end - target.start
            else:
                start = float(target.get('source_start', 0)); duration = float(target.get('source_end') or target.get('duration', 0))
            jobs.append({'path': target.path if isinstance(target, Clip) else target.get('path', ''),
                         'start': start, 'duration': duration,
                         'settings': {'denoise': level, 'mode': 'ai' if use_ai else 'standard',
                             'model': s.ai_model_path if use_ai else None,
                             'preset': s.clean_preset.currentData(), 'hum': s.clean_hum.isChecked(),
                             'wind': s.clean_wind.isChecked(), 'mix': s.clean_mix.value() / 100}})
        if jobs and ffbin('ffmpeg'):
            s.clean_progress = QProgressDialog('Midiendo el ruido y preparando la normalización…',
                'Cancelar', 0, 100, s); s.clean_progress.setMinimumDuration(0); s.clean_progress.show()
            s.audio_profile_worker = AudioProfileWorker(jobs)
            s.audio_profile_worker.progress.connect(s.clean_progress.setValue)
            s.clean_progress.canceled.connect(s.audio_profile_worker.cancel)
            s.audio_profile_worker.completed.connect(lambda results, refs=targets_for_analysis:
                s.audio_profiles_ready(results, refs))
            s.audio_profile_worker.start()
        elif jobs:
            s.statusBar().showMessage('FFmpeg no está disponible: se usan valores seguros sin análisis medido.', 8000)
        s.statusBar().showMessage(
            f"Limpieza {('IA' if use_ai else 'estándar')} aplicada a "
            f"{len(targets)} clip(s) y {len(music_targets)} pista(s) de audio · {level} dB.", 6000)

    def audio_profiles_ready(s, results, targets):
        if getattr(s, 'clean_progress', None):
            s.clean_progress.close()
        for index, profile in results:
            if index >= len(targets):
                continue
            target = targets[index]
            if isinstance(target, Clip):
                target.noise_profile = profile
            else:
                target['noise_profile'] = profile
        if s.tl.currentItem():
            s.select(s.tl.currentItem())
        if getattr(s, 'selected_audio_index', None) is not None:
            s.select_audio_track(s.selected_audio_index)
        measured = [profile for _, profile in results if profile.get('measured')]
        if measured:
            lowest = min(p.get('margin_db', 0) for p in measured)
            first = measured[0]; loudness = first.get('loudnorm', {}).get('input_i', 'no medido')
            s.clean_report.setText('Antes: ruido %.1f dB | voz %.1f dB | LUFS %s | despues: se mide al exportar.' %
                (first['noise_floor_db'], first['voice_db'], loudness))
            s.statusBar().showMessage('Análisis listo: ruido %.1f dB, voz %.1f dB, margen %.1f dB. %s' % (
                min(p['noise_floor_db'] for p in measured), max(p['voice_db'] for p in measured), lowest,
                'Margen bajo: prueba el método IA.' if lowest < 10 else 'Perfil aplicado y guardado en caché.'), 12000)
        else:
            s.clean_report.setText('No se pudo medir el ruido antes/despues; se aplicaron valores predeterminados.')
            s.statusBar().showMessage('No se pudo medir el ruido en todos los medios; se usaron valores predeterminados.', 10000)
        s.timeline.refresh()

    def apply_preset(s, name, values):
        if not s.cur:
            return QMessageBox.information(s, 'Filtros', 'Selecciona primero un clip de video.')
        s.remember(); s.cur.bright, s.cur.contrast, s.cur.sat = values
        for key in ('bright', 'contrast', 'sat'):
            widget = s.sp[key]; widget.blockSignals(True); widget.setValue(getattr(s.cur, key)); widget.blockSignals(False)
        s.statusBar().showMessage(f'Filtro aplicado: {name}', 3000); s.renumber()

    def make_transition_panel(s):
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addWidget(QLabel('Elige la transición de entrada del clip seleccionado.'))
        s.transition_choice = QComboBox()
        for label, value in (('Sin transición', 'cut'), ('Fundido', 'fade'), ('Disolver', 'dissolve'),
                             ('Cortinilla izquierda', 'wipeleft'), ('Cortinilla derecha', 'wiperight'),
                             ('Cortinilla arriba', 'wipeup'), ('Cortinilla abajo', 'wipedown'),
                             ('Deslizar izquierda', 'slideleft'), ('Deslizar derecha', 'slideright'),
                             ('Deslizar arriba', 'slideup'), ('Deslizar abajo', 'slidedown'),
                             ('Círculo abre', 'circleopen'), ('Círculo cierra', 'circleclose'),
                             ('Zoom', 'zoomin'), ('Radial', 'radial'), ('Suave izquierda', 'smoothleft'),
                             ('Suave derecha', 'smoothright'), ('Pixelar', 'pixelize')):
            s.transition_choice.addItem(label, value)
        layout.addWidget(s.transition_choice)
        s.transition_duration = QDoubleSpinBox(); s.transition_duration.setRange(0.1, 5); s.transition_duration.setValue(0.5); s.transition_duration.setSuffix(' s')
        layout.addWidget(QLabel('Duración')); layout.addWidget(s.transition_duration)
        apply_transition = QPushButton('Aplicar al clip seleccionado'); apply_transition.clicked.connect(s.set_transition)
        layout.addWidget(apply_transition)
        layout.addStretch(); return w

    def set_transition(s):
        if not s.cur:
            return QMessageBox.information(s, 'Transiciones', 'Selecciona primero un clip.')
        s.remember(); s.cur.transition = s.transition_choice.currentData()
        s.cur.transition_duration = s.transition_duration.value()
        s.renumber(); s.statusBar().showMessage('Transición aplicada al clip seleccionado.', 3000)

    def choose_lut(s):
        if not s.cur:
            return QMessageBox.information(s, 'LUT', 'Selecciona primero un clip de video.')
        path, _ = QFileDialog.getOpenFileName(s, 'Importar LUT', '', 'LUT 3D (*.cube)')
        if path:
            s.setf('lut_path', path)

    def export_srt(s):
        content = subtitles_srt(s.texts)
        if not content:
            return QMessageBox.information(s, 'Subtítulos', 'No hay subtítulos para exportar.')
        path, _ = QFileDialog.getSaveFileName(s, 'Exportar subtítulos', 'subtitulos.srt', 'SubRip (*.srt)')
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='\n') as target:
                target.write(content)
            s.statusBar().showMessage(f'Subtítulos guardados: {path}', 5000)
        except OSError as exc:
            QMessageBox.critical(s, 'Subtítulos', f'No se pudo guardar el archivo.\n{exc}')

    def auto_subtitles(s):
        it = s.tl.currentItem()
        if not it or it.data(Qt.ItemDataRole.UserRole).kind != 'video':
            return QMessageBox.information(s, 'Subtítulos automáticos', 'Selecciona primero un clip de video con voz.')
        options = [('Idioma', 'choice', 'auto', [('Automático', 'auto'), ('Español', 'es'),
                   ('Inglés', 'en'), ('Francés', 'fr'), ('Alemán', 'de'), ('Portugués', 'pt')]),
                   ('Modelo', 'choice', 'base', [('Tiny · rápido', 'tiny'), ('Base · equilibrado', 'base'),
                   ('Small · más preciso', 'small')])]
        values = ask(s, 'Subtítulos automáticos', [(k, k, typ, default, choices) for k, typ, default, choices in options])
        if not values:
            return
        clip = it.data(Qt.ItemDataRole.UserRole)
        s.speech_progress = QProgressDialog('Whisper descargará el modelo y analizará el audio en segundo plano…', 'Cancelar', 0, 100, s)
        s.speech_progress.setWindowTitle('Subtítulos automáticos'); s.speech_progress.setWindowModality(Qt.WindowModality.WindowModal)
        s.speech_progress.setMinimumDuration(0); s.speech_progress.show()
        s.speech_worker = WhisperWorker(clip.path, values['Modelo'], values['Idioma'], s.whisper_model_dir)
        card = s.ai_cards['whisper']; card['cancel'].setEnabled(True); card['progress'].setValue(0)
        card['status'].setText('Descargando/preparando modelo y analizando…')
        s.speech_worker.progress.connect(lambda pct, msg: (s.speech_progress.setLabelText(msg), s.speech_progress.setValue(pct),
            card['progress'].setValue(pct), card['status'].setText(msg)))
        s.speech_progress.canceled.connect(s.speech_worker.cancel)
        s.speech_worker.completed.connect(s.subtitles_finished); s.speech_worker.start()

    def subtitles_finished(s, segments, error):
        s.speech_progress.close()
        s.ai_cards['whisper']['cancel'].setEnabled(False)
        if error:
            if 'cancel' in error.lower():
                s.ai_cards['whisper']['status'].setText('Generación cancelada.')
                s.statusBar().showMessage('Generación de subtítulos cancelada.', 5000)
            else:
                s.ai_cards['whisper']['status'].setText('No se pudo completar; revisa tu conexión a internet.' if network_hint(error) else 'No se pudo completar.')
                if network_hint(error):
                    intro = ('No se pudo descargar el modelo de Whisper. La primera vez que usas esta función se necesita '
                             'conexión a internet para bajarlo; se guarda en %LOCALAPPDATA%\\NoiseCutStudio\\models y después funciona sin internet. '
                             'Revisa tu conexión y vuelve a intentarlo.')
                else:
                    intro = 'No se pudo completar Whisper. Comprueba que el clip tenga audio con voz.'
                QMessageBox.critical(s, 'Subtítulos automáticos', intro + '\n\n' + error)
            return
        records = subtitle_records(segments or [])
        if records:
            s.remember(); s.texts.extend(records); s.track_refresh[1](); s.timeline.refresh()
            s.ai_cards['whisper']['progress'].setValue(100)
            s.ai_cards['whisper']['status'].setText(f'{len(records)} segmentos listos para editar.')
            s.statusBar().showMessage(f'{len(records)} subtítulos añadidos a la pista Texto; puedes editarlos con doble clic.', 8000)
        else:
            QMessageBox.information(s, 'Subtítulos automáticos', 'No se reconoció voz en este clip.')

    def remove_background(s):
        it = s.tl.currentItem()
        if not it or it.data(Qt.ItemDataRole.UserRole).kind != 'video':
            return QMessageBox.information(s, 'Quitar fondo', 'Selecciona primero un clip de video.')
        if not ffbin('ffmpeg'):
            return QMessageBox.information(s, 'Quitar fondo', 'Para procesar el video, instala FFmpeg; el modelo de IA se descarga al utilizarlo.')
        clip = it.data(Qt.ItemDataRole.UserRole)
        suggested = os.path.splitext(clip.path)[0] + '_sin_fondo.mov'
        output, _ = QFileDialog.getSaveFileName(s, 'Guardar video con fondo transparente', suggested, 'Video QuickTime con transparencia (*.mov)')
        if not output:
            return
        if not output.lower().endswith('.mov'):
            output += '.mov'
        QMessageBox.information(s, 'Quitar fondo', 'El procesamiento cuadro a cuadro puede tardar. El primer uso descarga el modelo ligero U²-NetP. Puedes cancelar durante el análisis.')
        fps = 30
        try:
            result = subprocess.run([ffbin('ffprobe'), '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=avg_frame_rate', '-of', 'default=nw=1:nk=1', clip.path],
                capture_output=True, text=True, creationflags=NOWIN, timeout=8)
            numerator, denominator = result.stdout.strip().split('/')
            fps = round(float(numerator) / float(denominator))
        except (OSError, ValueError, subprocess.TimeoutExpired, ZeroDivisionError):
            pass
        s.bg_progress = QProgressDialog('Preparando la eliminación del fondo…', 'Cancelar', 0, 100, s)
        s.bg_progress.setWindowTitle('Quitar fondo'); s.bg_progress.setWindowModality(Qt.WindowModality.WindowModal)
        s.bg_progress.setMinimumDuration(0); s.bg_progress.show()
        s.bg_worker = BackgroundWorker(clip.path, output, fps)
        card = s.ai_cards['background']; card['cancel'].setEnabled(True); card['progress'].setValue(0)
        card['status'].setText('Preparando modelo y procesando…')
        s.bg_worker.progress.connect(lambda pct, msg: (s.bg_progress.setLabelText(msg), s.bg_progress.setValue(pct),
            card['progress'].setValue(pct), card['status'].setText(msg)))
        s.bg_progress.canceled.connect(s.bg_worker.cancel)
        s.bg_worker.completed.connect(s.background_finished); s.bg_worker.start()

    def stabilize_clip(s):
        it = s.tl.currentItem()
        if not it or it.data(Qt.ItemDataRole.UserRole).kind != 'video':
            return QMessageBox.information(s, 'Estabilizar video', 'Selecciona primero un clip de video.')
        if not s.has_vidstab:
            return QMessageBox.information(s, 'Estabilizar video', 'Esta versión de FFmpeg no incluye vidstab; la opción se oculta automáticamente.')
        clip = it.data(Qt.ItemDataRole.UserRole)
        output, _ = QFileDialog.getSaveFileName(s, 'Guardar video estabilizado',
            os.path.splitext(clip.path)[0] + '_estabilizado.mp4', 'Video MP4 (*.mp4)')
        if not output:
            return
        QMessageBox.information(s, 'Estabilizar video', 'La estabilización analiza el video y luego lo corrige; tardará un tiempo.')
        s.stab_progress = QProgressDialog('Analizando el movimiento…', None, 0, 100, s)
        s.stab_progress.setWindowTitle('Estabilización'); s.stab_progress.setWindowModality(Qt.WindowModality.WindowModal)
        s.stab_progress.setMinimumDuration(0); s.stab_progress.show()
        s.stab_worker = StabilizeWorker(clip.path, output)
        s.stab_worker.progress.connect(lambda pct, msg: (s.stab_progress.setLabelText(msg), s.stab_progress.setValue(pct)))
        s.stab_worker.completed.connect(s.stabilization_finished); s.stab_worker.start()

    def stabilization_finished(s, path, error):
        s.stab_progress.close()
        if error:
            QMessageBox.critical(s, 'Estabilizar video', 'No se pudo estabilizar el video.\n\n' + error)
            return
        it = s.tl.currentItem()
        if it:
            s.remember(); clip = it.data(Qt.ItemDataRole.UserRole); clip.path = path
            s.player.setSource(QUrl.fromLocalFile(path)); s.renumber()
            s.statusBar().showMessage('Video estabilizado; el archivo original se conservó.', 8000)

    def background_finished(s, path, error):
        s.bg_progress.close()
        s.ai_cards['background']['cancel'].setEnabled(False)
        if error:
            if 'cancel' in error.lower():
                s.ai_cards['background']['status'].setText('Procesamiento cancelado.')
                s.statusBar().showMessage('Eliminación del fondo cancelada.', 5000)
            else:
                s.ai_cards['background']['status'].setText('No se pudo procesar; revisa tu conexión a internet.' if network_hint(error) else 'No se pudo procesar; revisa FFmpeg y el archivo.')
                if network_hint(error):
                    intro = ('No se pudo descargar el modelo U²-NetP. La primera vez que usas esta función se necesita '
                             'conexión a internet para bajarlo; se guarda en %LOCALAPPDATA%\\NoiseCutStudio\\models y después funciona sin internet. '
                             'Revisa tu conexión y vuelve a intentarlo.')
                else:
                    intro = 'No se pudo procesar el video. Revisa que FFmpeg funcione y que el archivo se pueda leer.'
                QMessageBox.critical(s, 'Quitar fondo', intro + '\n\n' + error)
            return
        it = s.tl.currentItem()
        if it:
            s.remember(); clip = it.data(Qt.ItemDataRole.UserRole); clip.path = path
            s.tl.setCurrentItem(it); s.renumber()
            s.ai_cards['background']['progress'].setValue(100)
            s.ai_cards['background']['status'].setText('Video con fondo transparente listo.')
            s.statusBar().showMessage('Video con fondo transparente creado; el archivo de origen se conservó.', 8000)

    def zoom_timeline(s, factor):
        s.timeline.scale = max(24, min(240, s.timeline.scale * factor)); s.timeline.refresh()

    def set_export_setting(s, key, value):
        if s.export_settings.get(key) != value:
            s.remember(); s.export_settings[key] = value; s.timeline.refresh()

    def revert_clip(s):
        it = s.tl.currentItem()
        if not it:
            return QMessageBox.information(s, 'Restablecer clip', 'Selecciona primero un clip.')
        c = it.data(Qt.ItemDataRole.UserRole)
        s.remember()
        clean = Clip(c.path, c.kind, c.src, c.audio, 0, c.src)
        it.setData(Qt.ItemDataRole.UserRole, clean); s.tl.setCurrentItem(it); s.renumber()

    def freeze_frame(s):
        it = s.tl.currentItem()
        if not it or it.data(Qt.ItemDataRole.UserRole).kind != 'video':
            return QMessageBox.information(s, 'Congelar fotograma', 'Selecciona un clip de video.')
        c = it.data(Qt.ItemDataRole.UserRole)
        if not ffbin('ffmpeg'):
            return QMessageBox.critical(s, 'Falta FFmpeg', 'No se puede extraer el fotograma porque falta FFmpeg.')
        source = s.player.source().toLocalFile()
        pos = s.player.position() / 1000
        t = pos if source == c.path else c.start + pos * c.speed
        t = max(c.start, min(c.end - 0.01, t))
        seconds, ok = QInputDialog.getDouble(s, 'Congelar fotograma', 'Duración de la imagen fija (segundos):', 2, 0.1, 60, 1)
        if not ok:
            return
        folder = tempfile.mkdtemp(prefix='noisecut_freeze_'); image = os.path.join(folder, 'fotograma.png')
        try:
            result = subprocess.run([ffbin('ffmpeg'), '-y', '-ss', str(t), '-i', c.path,
                                     '-frames:v', '1', image], capture_output=True, text=True,
                                    creationflags=NOWIN, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return QMessageBox.critical(s, 'Fotograma', f'No se pudo extraer el fotograma.\n{e}')
        if result.returncode != 0 or not os.path.exists(image):
            return QMessageBox.critical(s, 'Fotograma', 'FFmpeg no pudo extraer el fotograma seleccionado.')
        s.push(Clip(image, 'image', seconds, False, 0, seconds), s.tl.row(it) + 1)

    def separate_audio(s):
        it = s.tl.currentItem()
        if not it:
            return QMessageBox.information(s, 'Separar audio', 'Selecciona un clip de video.')
        c = it.data(Qt.ItemDataRole.UserRole)
        if c.kind != 'video' or not c.audio:
            return QMessageBox.information(s, 'Separar audio', 'El clip seleccionado no contiene audio.')
        offset = sum(s.tl.item(i).data(Qt.ItemDataRole.UserRole).out for i in range(s.tl.row(it)))
        s.remember()
        source_seconds = max(0.1, c.end - c.start)
        s.music.append({'path': c.path, 'offset': offset, 'vol': c.volume / 100,
                        'volume_db': 20 * math.log10(max(1e-8, c.volume / 100)),
                        'role': 'voice', 'denoise': c.denoise, 'duration': source_seconds,
                        'clip_duration': c.out, 'source_start': c.start,
                        'source_end': c.end, 'speed': c.speed,
                        'fade_in': c.audio_fade_in, 'fade_out': c.audio_fade_out})
        s.track_refresh[0](); s.timeline.refresh()

    def add_overlay(s):
        path, _ = QFileDialog.getOpenFileName(s, 'Añadir video superpuesto', '',
            'Video e imagen (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.png *.jpg *.jpeg *.webp)')
        if not path:
            return
        duration = 5.0
        if os.path.splitext(path)[1].lower() in VID:
            if not ffbin('ffprobe'):
                return QMessageBox.critical(s, 'Falta FFmpeg', 'Se necesita FFprobe para añadir el video superpuesto.')
            try:
                duration = probe(path)[0]
            except (OSError, ValueError, json.JSONDecodeError):
                return QMessageBox.critical(s, 'Video superpuesto', 'No se pudo leer la duración del archivo.')
        maximum = max(1.0, sum(s.tl.item(i).data(Qt.ItemDataRole.UserRole).out for i in range(s.tl.count())))
        spec = [('start', 'Inicio en el proyecto (s)', 'float', 0, 0, 3600),
                ('end', 'Fin en el proyecto (s)', 'float', min(maximum, duration), 0.1, 3600),
                ('x', 'Posición X (%)', 'int', 75, 0, 100), ('y', 'Posición Y (%)', 'int', 15, 0, 100),
                ('scale', 'Tamaño (% del ancho)', 'int', 25, 5, 100)]
        params = ask(s, 'Video superpuesto', spec)
        if params is None:
            return
        s.remember(); params['path'] = path; params['duration'] = duration
        s.overlays.append(params); s.timeline.refresh()

    def add_to_timeline(s, it):
        p = it.data(Qt.UserRole); ext = os.path.splitext(p)[1].lower()
        if ext in AUD:
            return s.add_music(p)
        if ext in IMG:
            c = Clip(p, 'image', 5, False, 0, 5)
        else:
            try:
                dur, has_audio = probe(p)
            except (OSError, ValueError, json.JSONDecodeError):
                return QMessageBox.critical(s, 'No se pudo abrir el medio',
                    'No se pudo leer este archivo. Comprueba que FFprobe esté instalado y que el video sea compatible.')
            if dur <= 0:
                return QMessageBox.critical(s, 'Video no compatible', 'El archivo no tiene una duración válida o está dañado.')
            c = Clip(p, 'video', dur, has_audio, 0, dur)
        s.push(c)

    def push(s, c, row=None):
        s.remember()
        it = QListWidgetItem(); it.setData(Qt.UserRole, c)
        s.tl.insertItem(s.tl.count() if row is None else row, it); s.renumber(); s.tl.setCurrentItem(it)
        s.timeline.fit_project()

    def renumber(s, *_):
        for i in range(s.tl.count()):
            it = s.tl.item(i); c = it.data(Qt.UserRole)
            it.setText(f"{i + 1}. {os.path.basename(c.path)} · {c.out:.1f}s{'  🔇' if c.denoise or c.voice_enhance else ''}")
        if hasattr(s, 'timeline'):
            s.timeline.refresh()
        s.refresh_media_used()

    def select(s, it, _=None):
        s.cur = it.data(Qt.UserRole) if it else None
        if not s.cur:
            return
        for k, w in s.sp.items():
            w.blockSignals(True)
            if k.startswith('effect_'):
                value = s.cur.effects.get(k[7:], False if isinstance(w, QCheckBox) else 0)
                w.setChecked(bool(value)) if isinstance(w, QCheckBox) else w.setValue(value)
            elif isinstance(w, QCheckBox):
                w.setChecked(getattr(s.cur, k))
            elif isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, w.findData(getattr(s.cur, k))))
            else:
                w.setValue(getattr(s.cur, k))
            w.blockSignals(False)
        v = s.cur.kind == 'video'
        audio_enabled = v and s.cur.audio
        s.sp['start'].setEnabled(v); s.sp['speed'].setEnabled(v)
        for key in ('denoise', 'denoise_mode', 'volume', 'audio_fade_in', 'audio_fade_out',
                    'normalize', 'voice_enhance', 'pitch'):
            s.sp[key].setEnabled(audio_enabled)
        if v:
            s.player.setSource(QUrl.fromLocalFile(s.cur.path)); s.player.setPosition(int(s.cur.start * 1000)); s.player.pause()
        s.timeline.selected = s.tl.row(it) if it else -1
        s.timeline.set_selected(s.timeline.selected)

    def select_audio_track(s, index):
        s.selected_audio_index = index if 0 <= index < len(s.music) else None
        active = s.selected_audio_index is not None
        s.audio_props_card.setEnabled(active)
        if not active:
            return
        track = s.music[s.selected_audio_index]
        controls = {'track_volume_db': track.get('volume_db', 20 * math.log10(max(1e-8, track.get('vol', 1)))),
            'track_volume_slider': round(track.get('volume_db', 20 * math.log10(max(1e-8, track.get('vol', 1)))) + 60),
            'track_fade_in': track.get('fade_in', 0), 'track_fade_out': track.get('fade_out', 0),
            'track_speed': track.get('speed', 1), 'track_normalize': track.get('normalize', False),
            'track_denoise': track.get('denoise', 0) > 0,
            'track_voice_enhance': track.get('voice_enhance', False),
            'track_loop': track.get('loop', False), 'track_remove_leading_silence': track.get('remove_leading_silence', False),
            'track_ducking': track.get('ducking', False),
            'track_muted': track.get('muted', False), 'track_solo': track.get('solo', False),
            'track_locked': track.get('locked', False)}
        for name, value in controls.items():
            control = getattr(s, name); control.blockSignals(True)
            control.setChecked(bool(value)) if isinstance(control, QCheckBox) else control.setValue(value)
            control.blockSignals(False)
        s.track_volume_pct.setText(f"{100 * db_to_linear(track.get('volume_db', 20 * math.log10(max(1e-8, track.get('vol', 1))))):.1f}%")
        for name, key in (('track_duck_level', 'duck_level'), ('track_role', 'role'),
                          ('track_voice_effect', 'voice_effect')):
            control = getattr(s, name); control.blockSignals(True)
            control.setCurrentIndex(max(0, control.findData(track.get(key, {'role': 'music', 'duck_level': 'normal', 'voice_effect': 'none'}[key]))))
            control.blockSignals(False)
        audible = audible_audio_tracks(s.music)
        estimated = sum(db_to_linear(t.get('volume_db', 20 * math.log10(max(1e-8, t.get('vol', 1)))))
                        for t in audible)
        estimated += sum(c.volume / 100 for c in s.project_clips() if c.kind == 'video' and c.audio)
        s.audio_warning.setText('Aviso: la suma de las pistas puede superar 0 dB y saturar.' if estimated > 1 else '')

    def set_audio_track_field(s, key, value):
        index = getattr(s, 'selected_audio_index', None)
        if index is None or index >= len(s.music):
            return
        track = s.music[index]
        if track.get('locked') and key != 'locked':
            s.select_audio_track(index); return
        if track.get(key) == value:
            return
        s.remember(); track[key] = value
        if key in ('muted', 'solo', 'locked', 'ducking', 'duck_level', 'role'):
            for redraw in s.track_refresh: redraw()
        s.timeline.refresh(); s.refresh_media_used()
        s.select_audio_track(index)

    def reset_audio_track(s):
        index = getattr(s, 'selected_audio_index', None)
        if index is None:
            return
        old = s.music[index]
        defaults = {'offset': old.get('offset', 0), 'duration': old.get('duration', 0),
            'clip_duration': old.get('duration', 0), 'vol': 1.0, 'volume_db': 0,
            'role': 'music', 'fade_in': 0, 'fade_out': 0, 'speed': 1,
            'normalize': False, 'loop': False, 'remove_leading_silence': False,
            'denoise': 0, 'voice_enhance': False,
            'ducking': False, 'duck_level': 'normal',
            'muted': False, 'solo': False, 'locked': False, 'voice_effect': 'none',
            'denoise': 0, 'denoise_mode': 'standard', 'voice_enhance': False, 'pitch': 0}
        s.remember(); old.update(defaults)
        for redraw in s.track_refresh: redraw()
        s.select_audio_track(index); s.timeline.refresh()

    def snap_audio_time(s, seconds):
        boundaries = [s.timeline.current_time]
        elapsed = 0.0
        for i in range(s.tl.count()):
            clip = s.tl.item(i).data(Qt.UserRole)
            boundaries.extend((elapsed, elapsed + clip.out)); elapsed += clip.out
        nearest = min(boundaries, key=lambda point: abs(point - seconds))
        return nearest if abs(nearest - seconds) <= 0.15 else max(0, seconds)

    def audio_block_changed(s, index, x, width, edge, original_x, original_width):
        if not 0 <= index < len(s.music): return
        track = s.music[index]
        if track.get('locked'): return
        s.remember(); delta = (x - original_x) / s.timeline.scale
        if edge == 'left':
            delta = max(-track.get('offset', 0), min(track.get('clip_duration', 0) - 0.1, delta))
            old_offset = track.get('offset', 0)
            new_offset = s.snap_audio_time(old_offset + delta)
            actual_delta = new_offset - old_offset
            track['offset'] = new_offset
            track['clip_duration'] = max(0.1, track.get('clip_duration', 0) - actual_delta)
            track['source_start'] = max(0, track.get('source_start', 0) + actual_delta * track.get('speed', 1))
        elif edge == 'right':
            old_duration = track.get('clip_duration', 0)
            end = s.snap_audio_time(track.get('offset', 0) + width / s.timeline.scale)
            track['clip_duration'] = max(0.1, end - track.get('offset', 0))
            if track.get('source_end') is not None:
                source_delta = (track['clip_duration'] - old_duration) * track.get('speed', 1)
                track['source_end'] = max(track.get('source_start', 0) + 0.1,
                                          track['source_end'] + source_delta)
        else:
            track['offset'] = s.snap_audio_time(max(0, (x - 76) / s.timeline.scale))
        for redraw in s.track_refresh: redraw()
        s.timeline.refresh(); s.refresh_media_used(); s.select_audio_track(index)

    def audio_track_action(s, index, action):
        if not 0 <= index < len(s.music): return
        track = s.music[index]
        if action == 'fit':
            video_end = project_duration(s.project_clips())
            if video_end <= 0:
                return QMessageBox.information(s, 'Ajustar al video', 'Añade primero video a la linea de tiempo.')
            s.remember(); track['offset'] = min(max(0, track.get('offset', 0)), video_end)
            remainder = max(0.1, video_end - track['offset'])
            track['clip_duration'] = remainder; track['fade_out'] = min(3.0, remainder)
        elif action == 'normalize':
            s.remember(); track['normalize'] = not track.get('normalize', False)
        elif action == 'duck':
            s.remember(); track['ducking'] = not track.get('ducking', False)
        elif action == 'duplicate':
            s.remember(); duplicate = copy.deepcopy(track)
            duplicate['offset'] += duplicate.get('clip_duration', duplicate.get('duration', 0))
            duplicate['locked'] = False; duplicate['solo'] = False; s.music.append(duplicate)
        elif action in ('delete', 'close'):
            offset = track.get('offset', 0); length = track.get('clip_duration', track.get('duration', 0))
            s.remember(); s.music.pop(index)
            if action == 'close':
                for other in s.music:
                    if other.get('offset', 0) >= offset + length: other['offset'] = max(0, other['offset'] - length)
        elif action == 'split':
            local = s.timeline.current_time - track.get('offset', 0)
            length = track.get('clip_duration', track.get('duration', 0))
            if not 0.1 < local < length - 0.1: return
            s.remember(); second = copy.deepcopy(track)
            second['offset'] += local; second['clip_duration'] = length - local
            second['source_start'] = track.get('source_start', 0) + local * track.get('speed', 1)
            track['clip_duration'] = local; track['fade_out'] = 0
            s.music.append(second)
        elif action == 'clean':
            s.select_audio_track(index)
            previous = s.clean_scope.currentIndex()
            selected_index = s.clean_scope.findData('selected')
            if selected_index >= 0: s.clean_scope.setCurrentIndex(selected_index)
            s.clean_voice(); s.clean_scope.setCurrentIndex(previous)
        elif action == 'separate':
            s.separate_audio()
        for redraw in s.track_refresh: redraw()
        s.timeline.refresh(); s.refresh_media_used()
        if s.music: s.select_audio_track(min(index, len(s.music) - 1))
        else: s.select_audio_track(-1)

    def setf(s, k, v):
        if s.cur:
            if k.startswith('effect_'):
                name = k[7:]
                if s.cur.effects.get(name, False if isinstance(v, bool) else 0) == v:
                    return
                s.remember(); s.cur.effects[name] = v; s.renumber(); return
            if getattr(s.cur, k) == (int(v) if k == 'denoise' else v):
                return
            s.remember()
            setattr(s.cur, k, int(v) if k == 'denoise' else v); s.renumber()
            if k == 'denoise_mode' and v == 'ai' and not os.path.isfile(s.ai_model_path):
                QMessageBox.information(s, 'RNNoise', 'Elige un modelo RNNoise local desde el panel Audio.')

    def needs_ai_model(s):
        return (any(c.denoise > 0 and c.denoise_mode == 'ai'
                    for c in (s.tl.item(i).data(Qt.ItemDataRole.UserRole) for i in range(s.tl.count()))) or
                any(m.get('denoise', 0) > 0 and m.get('denoise_mode') == 'ai' for m in s.music))

    def export_settings_with_model(s):
        settings = copy.deepcopy(s.export_settings)
        settings['rnnoise_model'] = s.ai_model_path if os.path.exists(s.ai_model_path) else None
        return settings

    def choose_rnnoise_model(s):
        path, _ = QFileDialog.getOpenFileName(s, 'Seleccionar modelo RNNoise', '',
                                               'Modelo RNNoise (*.rnnn)')
        if not path:
            return
        try:
            with open(path, 'rb') as source:
                header = source.readline().decode('ascii', errors='ignore')
            if not header.startswith('rnnoise-nu model file version'):
                raise ValueError('El archivo seleccionado no parece un modelo RNNoise válido.')
            s.ai_model_path = path
            card = s.ai_cards.get('rnnoise')
            if card:
                card['status'].setText('Modelo local elegido; comprueba sus condiciones de uso.')
            QMessageBox.information(s, 'RNNoise', 'Modelo seleccionado. NoiseCut lo usará localmente.')
        except (OSError, ValueError) as exc:
            QMessageBox.critical(s, 'RNNoise', str(exc))

    def record_voiceover(s):
        state = s.recorder.recorderState()
        if state == QMediaRecorder.RecorderState.RecordingState:
            s.recorder.stop()
            s.record_status.setText('Guardando la grabación…')
            return
        if not QMediaDevices.audioInputs():
            return QMessageBox.information(s, 'Grabar voz', 'No se encontró un micrófono disponible.')
        path, _ = QFileDialog.getSaveFileName(s, 'Guardar voz en off', 'voz-en-off.wav', 'Audio WAV (*.wav)')
        if not path:
            return
        if not path.lower().endswith('.wav'):
            path += '.wav'
        s.recording_path = path
        s.recorder.setMediaFormat(QMediaFormat.FileFormat.Wave)
        s.recorder.setOutputLocation(QUrl.fromLocalFile(path))
        s.recorder.record()

    def on_record_state(s, state):
        recording = state == QMediaRecorder.RecorderState.RecordingState
        s.record_button.setText('■  Detener grabación' if recording else '●  Grabar voz en off')
        if recording:
            s.was_recording = True
            s.record_status.setText('Grabando… Pulsa detener cuando termines.')
        elif s.was_recording:
            s.was_recording = False
            s.record_status.setText('Finalizando archivo de audio…')
            QTimer.singleShot(700, s.finish_recording)

    def finish_recording(s):
        path = s.recording_path
        s.recording_path = None
        if not path or not os.path.isfile(path) or os.path.getsize(path) < 44:
            s.record_status.setText('No se pudo guardar la grabación. Revisa el micrófono e inténtalo de nuevo.')
            return
        try:
            with wave.open(path, 'rb') as wav:
                duration = wav.getnframes() / max(1, wav.getframerate())
        except (OSError, EOFError, wave.Error):
            duration = 0
        s.remember()
        s.music.append({'path': path, 'offset': s.timeline.current_time, 'vol': 1.0,
                        'volume_db': 0, 'role': 'voice', 'denoise': 0,
                        'denoise_mode': 'standard', 'duration': duration, 'clip_duration': duration,
                        'fade_in': 0, 'fade_out': 0, 'normalize': False,
                        'voice_enhance': False, 'pitch': 0})
        for redraw in s.track_refresh:
            redraw()
        s.timeline.refresh()
        s.record_status.setText(f'Grabación añadida a Audio ({duration:.1f} s).')

    def project_clips(s):
        return [s.tl.item(i).data(Qt.ItemDataRole.UserRole) for i in range(s.tl.count())]

    def project_total(s):
        return project_duration(s.project_clips(), s.music, s.overlays, s.texts, s.stickers)

    def seek_project_slider(s, value):
        s.seek_project(value / 1000)

    def seek_project(s, target, playing=None):
        total = s.project_total()
        target = max(0, min(float(target), max(0, total - 0.02)))
        if playing is None:
            playing = s.player.playbackState() == QMediaPlayer.PlayingState
        source = s.player.source().toLocalFile()
        if s.timeline_preview_path and source == s.timeline_preview_path and os.path.isfile(source):
            s.player.setPosition(int(target * 1000))
            if playing: s.player.play()
            else: s.player.pause()
            s.timeline.set_global_time(target)
            return
        elapsed = 0.0
        for index, clip in enumerate(s.project_clips()):
            if target <= elapsed + clip.out or index == s.tl.count() - 1:
                local = max(0, min(clip.out, target - elapsed))
                s.fallback_playback = bool(playing)
                s.tl.setCurrentRow(index)
                s.player.setSource(QUrl.fromLocalFile(clip.path))
                s.player.setPosition(int((clip.start + local * clip.speed) * 1000))
                if playing:
                    QTimer.singleShot(120, s.player.play)
                else:
                    s.player.pause()
                s.timeline.set_global_time(target)
                return
            elapsed += clip.out
        s.timeline.set_global_time(target)

    def toggle(s):
        source = s.player.source().toLocalFile()
        if s.timeline_preview_path and source == s.timeline_preview_path:
            if s.player.playbackState() == QMediaPlayer.PlayingState:
                s.player.pause(); s.play.setText('▶  Reproducir todo')
            else:
                if s.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                    s.player.setPosition(0)
                s.player.play(); s.play.setText('❚❚  Pausar')
            return
        if s.fallback_playback:
            if s.player.playbackState() == QMediaPlayer.PlayingState:
                s.player.pause(); s.play.setText('▶  Reproducir todo')
            else:
                if s.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                    s.player.setPosition(0)
                s.player.play(); s.play.setText('❚❚  Pausar')
            return
        s.render_timeline_preview()

    def start_fallback_playback(s):
        clips = s.project_clips()
        if not clips:
            return
        target = s.timeline.current_time
        if target >= s.project_total() - 0.05:
            target = 0
        s.fallback_playback = True
        s.seek_project(target, playing=True)
        s.play.setText('❚❚  Pausar')

    def render_timeline_preview(s):
        if not s.project_clips():
            return QMessageBox.information(s, 'Reproducción', 'Añade un clip de video a la línea de tiempo.')
        state = s.project_data()
        paths = ([c.path for c in s.project_clips()] + [m.get('path', '') for m in s.music] +
                 [o.get('path', '') for o in s.overlays] + [x.get('path', '') for x in s.stickers] +
                 [s.ai_model_path])
        audio_mode = s.preview_mode.currentData()
        key = preview_fingerprint(state, paths, audio_mode)
        cache_file = os.path.join(s.preview_cache_dir, key + '.mp4')
        if os.path.isfile(cache_file) and os.path.getsize(cache_file) > 0:
            s.timeline_preview_path = cache_file; s.timeline_preview_hash = key
            s.fallback_playback = False
            s.player.setSource(QUrl.fromLocalFile(cache_file))
            s.player.setPosition(int(s.timeline.current_time * 1000)); s.player.play()
            s.play.setText('❚❚  Pausar')
            return
        if s.timeline_preview_hash != key:
            s.timeline_preview_path = None
        if not ffbin('ffmpeg'):
            s.start_fallback_playback()
            return QMessageBox.critical(s, 'Falta FFmpeg',
                'No se puede generar la vista previa completa sin FFmpeg. Se reproducirán los clips uno por uno.')
        if s.timeline_preview_worker and s.timeline_preview_worker.isRunning():
            s.pending_preview = True
            s.timeline_preview_worker.cancel()
            return
        s.pending_preview = False
        s.start_fallback_playback()
        clips = copy.deepcopy(s.project_clips())
        music = copy.deepcopy(s.music)
        if audio_mode == 'before':
            for clip in clips:
                clip.denoise = 0; clip.denoise_mode = 'standard'; clip.voice_enhance = False; clip.normalize = False
            for track in music:
                track.update(denoise=0, denoise_mode='standard', voice_enhance=False, normalize=False)
        settings = s.export_settings_with_model()
        settings.update(format='MP4', quality='baja', preview=True)
        width, height = canvas_size(settings)
        preview_size = (640, max(2, round(640 * height / width)))
        s.preview_tmp = tempfile.mkdtemp(prefix='noisecut_full_preview_')
        try:
            command, duration = build(clips, copy.deepcopy(s.texts), copy.deepcopy(s.stickers), music,
                cache_file, s.preview_tmp, size=preview_size, overlays=copy.deepcopy(s.overlays), settings=settings)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            shutil.rmtree(s.preview_tmp, ignore_errors=True)
            return QMessageBox.critical(s, 'Vista previa', f'No se pudo preparar el proyecto completo.\n{exc}')
        s.preview_dialog = QProgressDialog('Generando la vista previa de todo el proyecto…', 'Cancelar', 0, 100, s)
        s.preview_dialog.setWindowTitle('Vista previa del proyecto')
        s.preview_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        s.preview_dialog.setMinimumDuration(0); s.preview_dialog.show()
        s.timeline_preview_worker = Worker(command, duration)
        s.timeline_preview_worker.prog.connect(s.preview_dialog.setValue)
        s.preview_dialog.canceled.connect(s.timeline_preview_worker.cancel)
        s.timeline_preview_worker.done.connect(lambda err: s.timeline_preview_finished(err, cache_file, key))
        s.timeline_preview_worker.start()
        s.statusBar().showMessage('Reproducción provisional: los clips avanzan mientras se prepara la vista con efectos.')

    def timeline_preview_finished(s, err, path, key):
        if getattr(s, 'preview_dialog', None):
            s.preview_dialog.close()
        shutil.rmtree(getattr(s, 'preview_tmp', ''), ignore_errors=True)
        if err == '__CANCELLED__':
            if s.pending_preview:
                s.pending_preview = False
                QTimer.singleShot(150, s.render_timeline_preview)
            else:
                s.statusBar().showMessage('Generación de vista previa cancelada; continúa la reproducción provisional.', 5000)
            return
        if err:
            if os.path.exists(path):
                os.remove(path)
            s.statusBar().showMessage('No se pudo generar la vista completa; puedes continuar con la reproducción provisional.', 8000)
            QMessageBox.critical(s, 'Vista previa', 'FFmpeg no pudo generar la vista previa completa.\n' + err[-1200:])
            return
        current_paths = ([c.path for c in s.project_clips()] + [m.get('path', '') for m in s.music] +
                         [o.get('path', '') for o in s.overlays] + [x.get('path', '') for x in s.stickers] +
                         [s.ai_model_path])
        current_key = preview_fingerprint(s.project_data(), current_paths, s.preview_mode.currentData())
        if current_key != key:
            s.timeline_preview_hash = None
            QTimer.singleShot(100, s.render_timeline_preview)
            return
        s.timeline_preview_path = path; s.timeline_preview_hash = key
        target = s.timeline.current_time
        s.fallback_playback = False
        s.player.setSource(QUrl.fromLocalFile(path)); s.player.setPosition(int(target * 1000)); s.player.play()
        s.play.setText('❚❚  Pausar')
        s.statusBar().showMessage('Vista previa completa lista; se conserva hasta que cambie el proyecto.', 6000)

    def on_media_status(s, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if s.timeline_preview_path and s.player.source().toLocalFile() == s.timeline_preview_path:
                s.timeline.set_global_time(s.project_total()); s.fallback_playback = False
                s.play.setText('▶  Reproducir todo')
            elif s.fallback_playback:
                s.fallback_playback = False; s.play.setText('▶  Reproducir todo')

    def on_pos(s, p):
        source = s.player.source().toLocalFile()
        total = s.project_total()
        if s.timeline_preview_path and source == s.timeline_preview_path:
            current = p / 1000
            s.timeline.set_global_time(current)
        else:
            current = p / 1000
            row = s.tl.row(s.tl.currentItem()) if s.tl.currentItem() else -1
            if row >= 0:
                clip = s.tl.item(row).data(Qt.ItemDataRole.UserRole)
                before = sum(s.tl.item(i).data(Qt.ItemDataRole.UserRole).out for i in range(row))
                local = (current - clip.start) / clip.speed if source == clip.path else current
                current = before + max(0, local)
                s.timeline.set_global_time(current)
                if s.fallback_playback and source == clip.path and p / 1000 >= clip.end - 0.03:
                    if row + 1 < s.tl.count():
                        next_start = before + clip.out
                        s.tl.setCurrentRow(row + 1)
                        next_clip = s.tl.currentItem().data(Qt.ItemDataRole.UserRole)
                        s.player.setSource(QUrl.fromLocalFile(next_clip.path))
                        s.player.setPosition(int(next_clip.start * 1000))
                        QTimer.singleShot(120, s.player.play)
                        s.timeline.set_global_time(next_start)
                    else:
                        s.fallback_playback = False; s.play.setText('▶  Reproducir todo')
        s.seek.setRange(0, max(0, int(total * 1000)))
        s.seek.setValue(max(0, min(int(current * 1000), int(total * 1000))))
        s.time_label.setText(f'{int(current // 60):02d}:{int(current % 60):02d} / {int(total // 60):02d}:{int(total % 60):02d}')

    def render_preview(s):
        it = s.tl.currentItem()
        if not it:
            return QMessageBox.information(s, 'Vista previa', 'Selecciona un clip de la línea de tiempo.')
        if not ffbin('ffmpeg'):
            return QMessageBox.critical(s, 'Falta FFmpeg', 'Instala FFmpeg y vuelve a abrir NoiseCut Studio.')
        c = copy.deepcopy(it.data(Qt.UserRole))
        if c.kind == 'video' and s.player.source().toLocalFile() == c.path:
            c.start = max(c.start, min(c.end - 0.1, s.player.position() / 1000))
        c.end = min(c.end, c.start + 8 * c.speed)
        preview_settings = s.export_settings_with_model()
        if s.preview_mode.currentData() == 'before':
            c.denoise = 0; c.denoise_mode = 'standard'; c.voice_enhance = False; c.normalize = False
        s.preview_dir = tempfile.mkdtemp(prefix='noisecut_preview_')
        preview_file = os.path.join(s.preview_dir, 'preview.mp4')
        try:
            cmd, duration = build([c], [], [], [], preview_file, s.preview_dir,
                                  size=(640, 360), settings=preview_settings)
        except (OSError, ValueError) as e:
            shutil.rmtree(s.preview_dir, ignore_errors=True)
            return QMessageBox.critical(s, 'Vista previa', f'No se pudo preparar la vista previa.\n{e}')
        s.preview_effects.setEnabled(False); s.statusBar().showMessage('Generando vista previa con efectos…')
        s.preview_worker = Worker(cmd, duration)
        s.preview_worker.done.connect(lambda err: s.preview_finished(err, preview_file))
        s.preview_worker.start()

    def preview_finished(s, err, path):
        s.preview_effects.setEnabled(True)
        if err:
            s.statusBar().clearMessage()
            QMessageBox.critical(s, 'Vista previa', 'No se pudo generar la vista previa. Comprueba que FFmpeg tenga los códecs necesarios.')
            return
        s.player.setSource(QUrl.fromLocalFile(path)); s.player.play()
        s.statusBar().showMessage('Vista previa de hasta 8 segundos con los efectos del clip.')

    def add_marker(s):
        name, ok = QInputDialog.getText(s, 'Marcador', 'Nombre para este punto:')
        if not ok or not name.strip():
            return
        s.remember(); s.markers.append({'time': s.timeline.current_time, 'name': name.strip()})
        s.timeline.refresh(); s.statusBar().showMessage('Marcador agregado.', 3000)

    def remove_silence(s):
        item = s.tl.currentItem()
        if not item:
            return QMessageBox.information(s, 'Quitar silencios', 'Selecciona un clip de video.')
        clip = item.data(Qt.UserRole)
        if clip.kind != 'video':
            return QMessageBox.information(s, 'Quitar silencios', 'La deteccion necesita un clip de video.')
        threshold, ok = QInputDialog.getInt(s, 'Quitar silencios', 'Umbral de silencio (dB):', -35, -70, -15, 1)
        if not ok:
            return
        minimum, ok = QInputDialog.getDouble(s, 'Quitar silencios',
            'Duracion minima que se eliminara (segundos):', 0.6, 0.1, 10, 1)
        if not ok:
            return
        try:
            intervals = detect_silences(clip.path, clip.start, clip.end - clip.start, threshold, minimum)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return QMessageBox.critical(s, 'Quitar silencios', f'No se pudo analizar el audio.\n{exc}')
        pieces = split_around_silence(clip.start, clip.end, intervals, minimum)
        if len(pieces) < 2:
            return QMessageBox.information(s, 'Quitar silencios', 'No se encontraron cortes aplicables.')
        preview = '\n'.join(f'{a:.2f} s - {b:.2f} s' for a, b in intervals[:12])
        answer = QMessageBox.question(s, 'Vista previa de cortes',
            f'Se detectaron {len(intervals)} silencios. Se crearan {len(pieces)} partes y se cerraran los huecos.\n\n'
            f'Tiempos detectados:\n{preview}\n\n¿Aplicar los cortes?')
        if answer != QMessageBox.StandardButton.Yes:
            return
        row = s.tl.row(item)
        s.remember(); s.tl.takeItem(row)
        first = None
        for index, (left, right) in enumerate(pieces):
            part = copy.deepcopy(clip); part.start, part.end = left, right
            if index:
                part.transition = 'cut'; part.transition_duration = 0
            new_item = QListWidgetItem(); new_item.setData(Qt.UserRole, part)
            s.tl.insertItem(row + index, new_item)
            first = first or new_item
        s.renumber(); s.tl.setCurrentItem(first); s.timeline.fit_project()

    def split(s):
        it = s.tl.currentItem()
        if not it:
            return
        c = it.data(Qt.UserRole)
        t = s.player.position() / 1000
        if c.kind == 'video' and s.player.source().toLocalFile() != c.path:
            t = c.start + t * c.speed
        if c.kind != 'video' or not (c.start + 0.1 < t < c.end - 0.1):
            return QMessageBox.information(s, 'Dividir', 'Mueve el cabezal (barra de reproducción) dentro del clip.')
        s.remember()
        n = copy.copy(c); n.start = t; c.end = t
        s.push(n, s.tl.row(it) + 1)

    def dup(s):
        it = s.tl.currentItem()
        if it:
            s.remember()
            s.push(copy.copy(it.data(Qt.UserRole)), s.tl.row(it) + 1)

    def rm(s):
        r = s.tl.currentRow()
        if r >= 0:
            s.remember()
            s.tl.takeItem(r); s.cur = None; s.renumber()

    def state(s):
        return {'clips': [copy.deepcopy(s.tl.item(i).data(Qt.UserRole)) for i in range(s.tl.count())],
                'texts': copy.deepcopy(s.texts), 'stickers': copy.deepcopy(s.stickers),
                'music': copy.deepcopy(s.music), 'overlays': copy.deepcopy(s.overlays),
                'markers': copy.deepcopy(s.markers),
                'settings': copy.deepcopy(s.export_settings)}

    def remember(s):
        if not hasattr(s, 'tl'):
            return
        current = s.state()
        if s.history_index >= 0 and current == s.history[s.history_index]:
            return
        s.history = s.history[:s.history_index + 1]
        s.history.append(current); s.history_index = len(s.history) - 1

    def restore(s, state):
        s.tl.clear()
        for c in copy.deepcopy(state['clips']):
            it = QListWidgetItem(); it.setData(Qt.UserRole, c); s.tl.addItem(it)
        s.texts[:] = copy.deepcopy(state['texts'])
        s.stickers[:] = copy.deepcopy(state['stickers'])
        s.music[:] = copy.deepcopy(state['music'])
        s.overlays[:] = copy.deepcopy(state.get('overlays', []))
        s.markers[:] = copy.deepcopy(state.get('markers', []))
        s.export_settings.update(copy.deepcopy(state.get('settings', {})))
        if hasattr(s, 'burn_subtitles'):
            s.burn_subtitles.setChecked(s.export_settings.get('burn_subtitles', True))
        if hasattr(s, 'duck_music'):
            s.duck_music.setChecked(s.export_settings.get('duck_music', False))
        for key, box in getattr(s, 'setting_boxes', {}).items():
            index = box.findData(s.export_settings[key]); box.setCurrentIndex(max(0, index))
        for refresh in s.track_refresh:
            refresh()
        s.cur = None; s.renumber()
        if hasattr(s, 'timeline'):
            s.timeline.fit_project()
        s.refresh_media_used()

    def undo(s):
        s.remember()
        if s.history_index > 0:
            s.history_index -= 1; s.restore(s.history[s.history_index])

    def redo(s):
        if s.history_index + 1 < len(s.history):
            s.history_index += 1; s.restore(s.history[s.history_index])

    def project_data(s):
        state = s.state()
        data = {'format': 'NoiseCutStudio', 'version': 1,
                'clips': [vars(c) for c in state['clips']], 'texts': state['texts'],
                'stickers': state['stickers'], 'music': state['music'],
                'overlays': state['overlays'], 'markers': state['markers'], 'settings': state['settings']}
        base = os.path.dirname(s.project_path) if s.project_path else os.getcwd()
        for collection in ('clips', 'music', 'stickers', 'overlays'):
            for item in data[collection]:
                item['path'] = make_project_relative(item.get('path', ''), base)
        return data

    def autosave_recovery(s):
        if not (s.tl.count() or s.music or s.texts or s.stickers or s.overlays):
            return
        try:
            with open(s.recovery_file, 'w', encoding='utf-8') as target:
                json.dump(s.project_data(), target, ensure_ascii=False)
            if hasattr(s, 'autosave_label'):
                s.autosave_label.setText('Autoguardado a las ' + datetime.now().strftime('%H:%M:%S'))
        except (OSError, TypeError):
            s.statusBar().showMessage('No se pudo guardar la recuperación automática.', 5000)

    def offer_recovery(s):
        if not os.path.isfile(s.recovery_file):
            return
        try:
            with open(s.recovery_file, encoding='utf-8') as source:
                data = json.load(source)
            if data.get('format') != 'NoiseCutStudio' or data.get('version') != 1:
                raise ValueError('El archivo de recuperación no es compatible.')
            resolve_project_paths(data, os.getcwd())
            answer = QMessageBox.question(s, 'Recuperar proyecto',
                'Se encontro un proyecto guardado automaticamente. ¿Quieres recuperarlo?')
            if answer == QMessageBox.StandardButton.Yes:
                state = {'clips': [Clip(**c) for c in data.get('clips', [])],
                         'texts': data.get('texts', []), 'stickers': data.get('stickers', []),
                         'music': data.get('music', []), 'overlays': data.get('overlays', []),
                         'markers': data.get('markers', []),
                         'settings': data.get('settings', {})}
                s.restore(state); s.statusBar().showMessage('Proyecto recuperado del autoguardado.', 8000)
            else:
                os.remove(s.recovery_file)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            QMessageBox.warning(s, 'Recuperacion', f'No se pudo recuperar el proyecto.\n{exc}')

    def save_project_as(s):
        path, _ = QFileDialog.getSaveFileName(s, 'Guardar proyecto', 'mi_proyecto.ncs', 'Proyecto NoiseCut (*.ncs *.json)')
        if path:
            s.project_path = path; s.save_project()

    def save_project(s):
        if not s.project_path:
            return s.save_project_as()
        try:
            with open(s.project_path, 'w', encoding='utf-8') as f:
                json.dump(s.project_data(), f, ensure_ascii=False, indent=2)
            s.project_title.setText(os.path.basename(s.project_path))
            s.statusBar().showMessage(f'Proyecto guardado: {s.project_path}', 5000)
        except (OSError, TypeError) as e:
            QMessageBox.critical(s, 'No se pudo guardar', f'No se pudo guardar el proyecto.\n{e}')

    def open_project(s):
        path, _ = QFileDialog.getOpenFileName(s, 'Abrir proyecto', '', 'Proyecto NoiseCut (*.ncs *.json)')
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if data.get('format') != 'NoiseCutStudio' or data.get('version') != 1:
                raise ValueError('El archivo no es un proyecto compatible de NoiseCut Studio.')
            resolve_project_paths(data, os.path.dirname(path))
            for collection in ('clips', 'music', 'stickers', 'overlays'):
                for media in data.get(collection, []):
                    missing_path = media.get('path', '')
                    if missing_path and not os.path.exists(missing_path):
                        QMessageBox.information(s, 'Buscar medio',
                            f'No se encuentra este archivo. Selecciona su nueva ubicacion:\n{missing_path}')
                        located, _ = QFileDialog.getOpenFileName(s, 'Localizar medio perdido', '', 'Todos los archivos (*.*)')
                        if not located:
                            return
                        media['path'] = located
            state = {'clips': [Clip(**c) for c in data.get('clips', [])],
                     'texts': data.get('texts', []), 'stickers': data.get('stickers', []),
                     'music': data.get('music', []), 'overlays': data.get('overlays', []),
                     'markers': data.get('markers', []),
                     'settings': data.get('settings', {})}
            paths = [c.path for c in state['clips']] + [x['path'] for x in state['stickers']] + [x['path'] for x in state['music']] + [x['path'] for x in state['overlays']]
            missing = [p for p in paths if not os.path.exists(p)]
            if missing:
                raise ValueError('No se encuentran estos archivos de medios:\n' + '\n'.join(missing[:8]))
            s.restore(state); s.project_path = path; s.project_title.setText(os.path.basename(path)); s.history = []; s.history_index = -1; s.remember()
            QMessageBox.information(s, 'Proyecto abierto', 'Proyecto cargado correctamente. Los medios conservan sus rutas originales.')
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            QMessageBox.critical(s, 'No se pudo abrir', str(e))

    def new_project(s):
        s.restore({'clips': [], 'texts': [], 'stickers': [], 'music': [], 'overlays': [], 'markers': [], 'settings': {
            'aspect': '16:9', 'resolution': 1080, 'fps': 30, 'quality': 'alta', 'format': 'MP4',
            'background': 'negro', 'burn_subtitles': True}})
        s.project_path = None; s.project_title.setText('Proyecto sin guardar'); s.history = []; s.history_index = -1; s.remember()

    def export(s):
        clips = [s.tl.item(i).data(Qt.UserRole) for i in range(s.tl.count())]
        file_format = s.export_settings['format']
        audio_formats = ('MP3', 'WAV', 'AAC', 'M4A', 'FLAC')
        if not clips and file_format not in audio_formats:
            return QMessageBox.information(s, 'Exportar', 'Añade al menos un clip a la línea de tiempo.')
        if not ffbin('ffmpeg') or not ffbin('ffprobe'):
            return QMessageBox.critical(s, 'Falta FFmpeg', 'No se encuentran FFmpeg y FFprobe. Instálalos y vuelve a abrir NoiseCut Studio.')
        if clips:
            video_end = project_duration(clips)
            overlong = [m for m in s.music if m.get('offset', 0) +
                        m.get('clip_duration', m.get('duration', 0)) > video_end + 0.05]
            if overlong:
                answer = QMessageBox.question(s, 'Audio mas largo que el video',
                    'Hay pistas de audio que superan el final del video. El archivo exportado se limitara a la duracion del video. ¿Quieres recortar las pistas y aplicar un fundido de salida antes de exportar?')
                if answer == QMessageBox.StandardButton.Yes:
                    for index in [s.music.index(track) for track in overlong if track in s.music]:
                        s.audio_track_action(index, 'fit')
        extensions = {'MP4':'mp4', 'MP4_HEVC':'mp4', 'MOV':'mov', 'MKV':'mkv', 'WEBM':'webm',
                      'AVI':'avi', 'GIF':'gif', 'MP3':'mp3', 'WAV':'wav', 'AAC':'aac',
                      'M4A':'m4a', 'FLAC':'flac', 'PNG':'png', 'JPG':'jpg'}
        ext = extensions.get(file_format, 'mp4')
        default_name = 'mi_archivo.' + ext
        file_filter = f'Archivo {ext.upper()} (*.{ext})'
        out, _ = QFileDialog.getSaveFileName(s, 'Exportar', default_name, file_filter)
        if not out:
            return
        tmp = tempfile.mkdtemp()
        has_video_voice = any(c.kind == 'video' and c.audio for c in clips)
        jobs = []
        for index, track in enumerate(s.music):
            needs_norm = track.get('normalize') or (has_video_voice and track.get('role', 'music') == 'music')
            stats = track.get('loudnorm_stats')
            if not stats and track.get('role') == 'voice':
                stats = (track.get('noise_profile') or {}).get('loudnorm')
            if needs_norm and not (stats and all(k in stats for k in
                    ('input_i', 'input_lra', 'input_tp', 'input_thresh', 'target_offset'))):
                start = float(track.get('source_start', 0) or 0)
                duration = float(track.get('source_end') - start) if track.get('source_end') is not None else float(track.get('duration', 0) or 0)
                jobs.append({'index': index, 'path': track['path'], 'start': start,
                    'duration': duration, 'target': -16 if track.get('role') == 'voice' else -23})
        if jobs:
            s.measure_dlg = QProgressDialog('Midiendo niveles de las pistas para normalizar…', None, 0, 100, s)
            s.measure_dlg.setWindowModality(Qt.WindowModality.WindowModal); s.measure_dlg.show()
            s.measure_worker = LoudnormWorker(jobs)
            s.measure_worker.progress.connect(s.measure_dlg.setValue)
            s.measure_worker.completed.connect(lambda results: s.export_after_measure(results, clips, out, tmp))
            s.measure_worker.start(); return
        s.launch_export(clips, out, tmp)

    def export_after_measure(s, results, clips, out, tmp):
        s.measure_dlg.close()
        for index, stats in results:
            if 0 <= index < len(s.music) and stats:
                s.music[index]['loudnorm_stats'] = stats
        if any(not stats for _, stats in results):
            s.statusBar().showMessage('No se pudieron medir algunas pistas; FFmpeg usara normalizacion de una pasada para ellas.', 9000)
        s.launch_export(clips, out, tmp)

    def launch_export(s, clips, out, tmp):
        try:
            cmd, T = build(clips, s.texts, s.stickers, s.music, out, tmp,
                           overlays=s.overlays, settings=s.export_settings_with_model())
        except (OSError, ValueError, KeyError, TypeError) as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return QMessageBox.critical(s, 'No se pudo preparar la exportación', f'Revisa los ajustes y los archivos de medios.\n{e}')
        s.dlg = QProgressDialog('Exportando…', None, 0, 100, s); s.dlg.setWindowModality(Qt.WindowModal); s.dlg.show()
        s.wk = Worker(cmd, T); s.wk.prog.connect(s.dlg.setValue)
        s.wk.done.connect(lambda err: s.finished(err, out, tmp)); s.wk.start()

    def finished(s, err, out, tmp):
        s.dlg.close(); shutil.rmtree(tmp, ignore_errors=True)
        if err:
            QMessageBox.critical(s, 'Error al exportar', err)
        else:
            QMessageBox.information(s, 'Listo', f'Archivo exportado:\n{out}')


if __name__ == '__main__':
    app = QApplication(sys.argv); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    w = Main(); w.show(); sys.exit(app.exec())
