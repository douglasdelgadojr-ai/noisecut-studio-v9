"""Prueba el motor de exportacion (sin abrir la interfaz). Requiere ffmpeg en PATH."""
import re, os, subprocess, tempfile, shutil
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'noisecut.py'), encoding='utf-8').read()
head = re.sub(r'^from PySide6[^\n]*\n(?:[ \t]+[^\n]*\n)*', '',
              src.split('class Worker')[0], flags=re.M)
ns = {'__name__': 'x'}; exec(head, ns)
Clip, build, probe, canvas_size = ns['Clip'], ns['build'], ns['probe'], ns['canvas_size']
audio_fx_filters, denoise_f = ns['audio_fx_filters'], ns['denoise_f']
video_fx_filters = ns['video_fx_filters']
subtitle_records = ns['subtitle_records']
stabilization_commands = ns['stabilization_commands']
project_duration = ns['project_duration']
preview_fingerprint = ns['preview_fingerprint']
split_around_silence = ns['split_around_silence']
subtitles_srt, noise_gate_threshold = ns['subtitles_srt'], ns['noise_gate_threshold']
resolve_project_paths, make_project_relative = ns['resolve_project_paths'], ns['make_project_relative']
audio_track_filters, loudnorm_filter = ns['audio_track_filters'], ns['loudnorm_filter']
db_to_linear, audible_audio_tracks, duck_filter = ns['db_to_linear'], ns['audible_audio_tracks'], ns['duck_filter']
voice_effect_filters = ns['voice_effect_filters']

# Estas comprobaciones no ejecutan FFmpeg: detectan cambios accidentales en
# los filtros y el tamano de previsualizacion incluso en equipos sin FFmpeg.
preview_code = src[src.index('    def render_preview(s):'):src.index('    def preview_finished(s, err, path):')]
assert "s.player.source().toLocalFile() == c.path" in preview_code
assert 'c.start = max(c.start, min(c.end - 0.1, s.player.position() / 1000))' in preview_code
assert 'c.start + s.player.position()' not in preview_code
print('OK: el inicio de la vista previa usa tiempo absoluto del clip original')
static_dir = tempfile.mkdtemp()
try:
    preview_clip = Clip('entrada.mp4', 'video', 8, True, 0, 4, bright=0.1,
                        contrast=1.2, sat=0.5, blur=2, denoise=15, fade=0.3)
    command, duration = build([preview_clip], [], [],
                              [{'path': 'musica.wav', 'offset': 1, 'vol': 0.5, 'denoise': 12}],
                              'preview.mp4', static_dir, size=(640, 360))
    graph = command[command.index('-filter_complex') + 1]
    for expected in ('scale=640:360:', 'eq=brightness=0.1:contrast=1.2:saturation=0.5',
                     'gblur=sigma=2', 'fade=t=in:st=0:d=0.3',
                     'highpass=f=80,afftdn=nr=15:nf=-50.0:tn=1,afftdn=nr=5:nf=-56.0:tn=1',
                     'highpass=f=80,afftdn=nr=12:nf=-50.0:tn=1', 'volume=0.5', 'adelay=1000|1000'):
        assert expected in graph, f'No se construyo el filtro esperado: {expected}'
    assert duration == 4
    longer_music = [{'path': 'long.wav', 'offset': 0, 'duration': 40, 'clip_duration': 40,
                     'volume_db': -6, 'fade_in': 0.5, 'fade_out': 3, 'ducking': True}]
    ten_second_clip = Clip('ten.mp4', 'video', 10, True, 0, 10)
    assert project_duration([ten_second_clip], longer_music) == 10
    ten_cmd, ten_duration = build([ten_second_clip], [], [], longer_music,
        'limited.mp4', static_dir, settings={'duck_level': 'strong'})
    ten_graph = ten_cmd[ten_cmd.index('-filter_complex') + 1]
    assert ten_duration == 10 and 'atrim=duration=10.000' in ten_graph
    assert 'volume=0.50118723' in ten_graph and 'alimiter=limit=0.841395' in ten_graph
    assert 'sidechaincompress=threshold=0.02:ratio=14:attack=20:release=300' in ten_graph
    assert 'afade=t=in:st=0:d=0.500:curve=desi' in ten_graph
    assert 'afade=t=out:st=7.000:d=3.000:curve=desi' in ten_graph
    assert 'loudnorm=I=-23:TP=-1.5:LRA=11:linear=true' in ten_graph
    norm_stats = {'input_i': -26, 'input_lra': 5, 'input_tp': -8,
                  'input_thresh': -36, 'target_offset': 3}
    measured_norm = loudnorm_filter(-23, norm_stats)
    assert 'linear=true' in measured_norm and 'measured_I=-26.00' in measured_norm
    silent_start_filters = audio_track_filters({'path': 'x.wav', 'duration': 4,
        'clip_duration': 4, 'remove_leading_silence': True, 'speed': 1.5,
        'preserve_pitch': True, 'loop': True}, 10)
    assert any(f.startswith('silenceremove=start_periods=1') for f in silent_start_filters)
    assert 'atempo=1.5000' in silent_start_filters
    loop_cmd, _ = build([ten_second_clip], [], [], [{'path': 'short.wav', 'offset': 0,
        'duration': 2, 'clip_duration': 2, 'loop': True}], 'loop.mp4', static_dir)
    loop_graph = loop_cmd[loop_cmd.index('-filter_complex') + 1]
    assert 'acrossfade=d=0.200:c1=exp:c2=exp' in loop_graph
    assert abs(db_to_linear(-6) - 0.50118723) < 1e-8
    assert len(audible_audio_tracks([{'solo': True}, {'solo': False}, {'muted': True}])) == 1
    assert 'ratio=3' in duck_filter('soft') and 'ratio=14' in duck_filter('strong')
    assert all('flanger' not in item for item in voice_effect_filters('robot'))
    assert 'aecho=' in ','.join(voice_effect_filters('robot'))
    sidechain_cmd, _ = build([preview_clip], [], [],
        [{'path': 'musica.wav', 'offset': 0, 'vol': 0.5, 'duration': 4}],
        'ducked.mp4', static_dir, settings={'duck_music': True})
    assert 'sidechaincompress=threshold=0.02:ratio=8:attack=20:release=300' in sidechain_cmd[sidechain_cmd.index('-filter_complex') + 1]
    assert split_around_silence(0, 8, [(2, 4), (4.1, 5)], 0.6) == [(0, 2.0), (5.0, 8)]
    assert noise_gate_threshold(-42) == 0.015849
    assert subtitles_srt([{'text': 'Hola', 'start': 0.2, 'end': 1.4, 'is_subtitle': True}]) == \
        '1\n00:00:00,200 --> 00:00:01,400\nHola\n'
    project_sample = {'clips': [{'path': 'media/video.mp4'}], 'music': [], 'stickers': [], 'overlays': []}
    if os.name == 'nt':  # rutas tipo C:/ solo tienen sentido en Windows
        resolve_project_paths(project_sample, 'C:/projects/demo')
        assert project_sample['clips'][0]['path'].replace('\\', '/').endswith('media/video.mp4')
        assert make_project_relative('C:/projects/demo/media/a.wav', 'C:/projects/demo').replace('\\', '/') == 'media/a.wav'
    print('OK: filtros y tamano de vista previa se construyen correctamente')

    transform = Clip('entrada.mp4', 'video', 8, True, 0, 4, scale=75, pos_x=20, pos_y=70,
                     rotation=10, flip_h=True, flip_v=True, crop_left=10, crop_right=10,
                     crop_top=10, crop_bottom=10, opacity=50, reverse=True)
    settings = {'aspect': '9:16', 'resolution': 720, 'fps': 24, 'quality': 'baja',
                'background': 'desenfocado', 'format': 'MP4'}
    command, _ = build([transform], [], [], [], 'transform.mp4', static_dir,
                       overlays=[{'path': 'pip.mp4', 'start': 1, 'end': 3, 'scale': 25, 'x': 80, 'y': 20}],
                       settings=settings)
    graph = command[command.index('-filter_complex') + 1]
    for expected in ('crop=iw*0.8000:ih*0.8000', 'hflip', 'vflip', 'rotate=10*PI/180',
                     'reverse', 'scale=540:960:', 'colorchannelmixer=aa=0.500',
                     'gblur=sigma=24', "enable='between(t,1,3)'", '-crf'):
        assert expected in graph or expected in command, f'No se construyo el ajuste esperado: {expected}'
    assert canvas_size({'aspect': '9:16', 'resolution': 720}) == (720, 1280)

    mp3_command, mp3_duration = build([], [], [],
        [{'path': 'audio.wav', 'offset': 0, 'vol': 1, 'denoise': 0, 'duration': 5}],
        'audio.mp3', static_dir, settings={'format': 'MP3', 'quality': 'media'})
    mp3_graph = mp3_command[mp3_command.index('-filter_complex') + 1]
    assert '-c:a' in mp3_command and 'libmp3lame' in mp3_command and '-vn' in mp3_command and mp3_duration == 5
    assert '[aout]' in mp3_graph and 'adelay=0|0' in mp3_graph and 'volume=1' in mp3_graph
    voiceover = [{'path': 'voz.wav', 'offset': 2, 'vol': 0.8, 'denoise': 10,
                  'duration': 5, 'clip_duration': 5, 'role': 'voice', 'fade_in': 0.5, 'fade_out': 1.0,
                  'normalize': True, 'voice_enhance': True, 'pitch': 2}]
    voice_cmd, _ = build([], [], [], voiceover, 'voz.mp3', static_dir,
                         settings={'format': 'MP3', 'quality': 'alta'})
    voice_graph = voice_cmd[voice_cmd.index('-filter_complex') + 1]
    for expected in ('afftdn=nr=10', 'loudnorm=I=-16:TP=-1.5:LRA=11:linear=true',
                     'acompressor=', 'afade=t=in:st=0:d=0.500:curve=desi',
                     'afade=t=out:st=4.000:d=1.000:curve=desi', 'asetrate=48000*1.122462',
                     'adelay=2000|2000', 'volume=0.8'):
        assert expected in voice_graph, f'No se construyo el filtro de voz esperado: {expected}'
    gif_command, _ = build([preview_clip], [], [], [], 'anim.gif', static_dir,
                           settings={'format': 'GIF', 'fps': 24})
    assert 'anim.gif' in gif_command and '-loop' in gif_command
    for fmt, encoder in (('MOV', 'libx264'), ('MKV', 'libx264'),
                         ('WEBM', 'libvpx-vp9'), ('MP4_HEVC', 'libx265'), ('AVI', 'mpeg4')):
        fmt_cmd, _ = build([preview_clip], [], [], [], 'sample.' + fmt.lower(), static_dir,
                           settings={'format': fmt, 'quality': 'media'})
        assert encoder in fmt_cmd, 'Missing encoder for ' + fmt
    wav_cmd, _ = build([], [], [], [{'path': 'audio.wav', 'offset': 0, 'vol': 1, 'duration': 2}],
                       'sample.wav', static_dir, settings={'format': 'WAV'})
    assert 'pcm_s16le' in wav_cmd and '-vn' in wav_cmd
    print('OK: transformaciones, superposicion, lienzo y formatos se construyen correctamente')

    audio_filters = audio_fx_filters(15, 'standard', normalize=True, voice_enhance=True, pitch=12)
    for expected in ('highpass=f=80', 'afftdn=nr=15:nf=-50.0:tn=1', 'anlmdn=s=', 'loudnorm=I=-16:TP=-1.5:LRA=11',
                     'agate=threshold=0.00631', 'equalizer=f=250:t=q:w=1:g=-2',
                     'equalizer=f=3000:t=q:w=1:g=3', 'deesser=i=0.25',
                     'acompressor=threshold=-22dB:ratio=2.8:attack=10:release=220',
                     'alimiter=limit=0.841395',
                     'asetrate=48000*2.000000', 'aresample=48000', 'atempo=0.5000'):
        assert expected in ','.join(audio_filters), f'No se construyo el efecto de audio esperado: {expected}'
    filter_text = ','.join(audio_filters)
    order = [filter_text.index(name) for name in ('highpass=', 'afftdn=', 'agate=',
             'equalizer=', 'deesser=', 'acompressor=', 'loudnorm=', 'alimiter=')]
    assert order == sorted(order), 'El orden de limpieza, compresion y normalizacion es incorrecto'
    clean_cmd, _ = build([], [], [], [{'path': 'voz-sintetica.wav', 'offset': 0, 'vol': 1,
        'denoise': 15, 'denoise_mode': 'standard', 'duration': 6,
        'voice_enhance': True, 'normalize': True}], 'voz-limpia.mp3', static_dir,
        settings={'format': 'MP3', 'quality': 'alta'})
    clean_graph = clean_cmd[clean_cmd.index('-filter_complex') + 1]
    for expected in ('highpass=f=80', 'afftdn=nr=15:nf=-50.0:tn=1', 'agate=',
                     'equalizer=f=250:', 'equalizer=f=3000:', 'deesser=', 'acompressor=', 'loudnorm='):
        assert expected in clean_graph, f'build() no incluyo filtro esperado: {expected}'
    filters = audio_fx_filters(15, 'standard', None, True, True, 0,
                               {'noise_floor_db': -42.0, 'voice_db': -20.0,
                                'loudnorm': {'input_i': -24, 'input_lra': 4, 'input_tp': -3,
                                             'input_thresh': -34, 'target_offset': 1}})
    assert 'agate=threshold=0.015849' in ','.join(filters)
    assert any('measured_I=' in f and 'linear=true' in f for f in filters)
    assert filters[-1].startswith('alimiter=')
    ai_filter = denoise_f(20, 'ai', 'C:/models/rnnoise.rnnn')
    assert ai_filter == "highpass=f=80,arnndn=m='C\\:/models/rnnoise.rnnn'"
    assert denoise_f(0, 'ai', None) is None
    assert 'afftdn=nr=15:nf=-50.0:tn=1' in audio_fx_filters(15, 'ai', None)
    assert 'arnndn=' in ','.join(audio_fx_filters(15, 'ai', 'rnnoise.rnnn'))
    assert audio_fx_filters(8) != audio_fx_filters(15) != audio_fx_filters(25)
    outdoor = ','.join(audio_fx_filters(25, profile={'noise_floor_db': -38},
        preset='Exterior con viento', hum=True, wind=True))
    assert 'highpass=f=150' in outdoor and 'equalizer=f=60:' in outdoor and 'equalizer=f=180:' in outdoor
    assert project_duration([Clip('a', 'video', 10, True, 0, 4),
                             Clip('b', 'video', 10, True, 0, 5)]) == 9
    assert preview_fingerprint({'clips': [1]}) == preview_fingerprint({'clips': [1]})
    assert preview_fingerprint({'clips': [1]}) != preview_fingerprint({'clips': [2]})
    preview_cmd, _ = build([preview_clip], [], [], [], 'preview-fast.mp4', static_dir,
                           settings={'format': 'MP4', 'preview': True, 'quality': 'baja'})
    assert preview_cmd[preview_cmd.index('-preset') + 1] == 'ultrafast'
    print('OK: audio filter chains, AI fallback, voice, volume and pitch')

    image_fx = video_fx_filters({'vignette': 40, 'grain': 20, 'sharpness': 1.5,
        'hqdn3d': 2, 'auto_color': True, 'mirror': True, 'glitch': True,
        'black_white': True, 'sepia': True, 'pixelate': 12},
        1280, 720, chroma=True, chroma_color='#00ff00', chroma_similarity=0.3,
        lut_path='C:/looks/cine.cube')
    image_fx = ';'.join(image_fx)
    for expected in ('vignette=angle=', 'noise=alls=20:allf=t', 'unsharp=5:5:1.50',
                     'hqdn3d=2.00:2.00:6:6', 'normalize=blackpt=black:whitept=white',
                     'hflip', 'rgbashift=rh=4:bh=-4', 'hue=s=0', 'colorchannelmixer=',
                     'scale=trunc(iw/12):trunc(ih/12):flags=neighbor,scale=1280:720:flags=neighbor',
                     'colorkey=0x00ff00:0.300:0.10', "lut3d=file='C\\:/looks/cine.cube'"):
        assert expected in image_fx, f'No se construyo el efecto esperado: {expected}'
    assert len({'fade', 'dissolve', 'wipeleft', 'wiperight', 'wipeup', 'wipedown',
                'slideleft', 'slideright', 'slideup', 'slidedown', 'circleopen',
                'circleclose', 'zoomin', 'radial', 'smoothleft', 'smoothright',
                'smoothup', 'smoothdown', 'pixelize'}) >= 12
    detect_cmd, stabilize_cmd = stabilization_commands('ffmpeg', 'source.mp4', 'motion.trf', 'steady.mp4')
    assert 'vidstabdetect=shakiness=5:accuracy=15:result=motion.trf' in detect_cmd
    assert 'vidstabtransform=input=motion.trf:smoothing=10:optzoom=1' in stabilize_cmd
    assert '-map' in stabilize_cmd and '0:a?' in stabilize_cmd and stabilize_cmd[-1] == 'steady.mp4'
    transition_clips = [Clip('a.mp4', 'video', 8, True, 0, 4),
                        Clip('b.mp4', 'video', 4, True, 0, 4,
                             transition='circleopen', transition_duration=0.7)]
    transition_cmd, transition_duration = build(transition_clips, [], [], [], 'xfade.mp4', static_dir)
    transition_graph = transition_cmd[transition_cmd.index('-filter_complex') + 1]
    assert 'xfade=transition=circleopen:duration=0.700:offset=3.300' in transition_graph
    assert 'acrossfade=d=0.700:c1=tri:c2=tri' in transition_graph
    assert transition_duration == 7.3
    text_cmd, _ = build([preview_clip], [{'text': 'Hola', 'start': 0, 'end': 3, 'x': 50,
        'y': 85, 'size': 64, 'color': '#ffffff', 'bold': True, 'border': True,
        'shadow': True, 'background': True, 'background_color': '#112233', 'animation': 'write'}],
        [], [], 'text.mp4', static_dir)
    text_graph = text_cmd[text_cmd.index('-filter_complex') + 1]
    assert 'drawtext=textfile=' in text_graph and 'shadowx=3:shadowy=3' in text_graph
    assert 'box=1:boxcolor=0x112233@0.75' in text_graph and "enable='between(t," in text_graph
    print('OK: image effects, transitions, chroma, LUT and styled text')

    subtitles = subtitle_records([(0.2, 1.7, '  Hola mundo  '), (2, 2, 'Segunda linea'), (3, 4, '  ')])
    assert len(subtitles) == 2 and subtitles[0]['text'] == 'Hola mundo'
    assert subtitles[0]['start'] == 0.2 and subtitles[0]['end'] == 1.7
    assert subtitles[1]['end'] == 2.1 and subtitles[1]['animation'] == 'fade'
    assert subtitles[0]['is_subtitle'] is True
    burned_cmd, _ = build([preview_clip], subtitles, [], [], 'burned.mp4', static_dir)
    hidden_cmd, _ = build([preview_clip], subtitles, [], [], 'unburned.mp4', static_dir,
                          settings={'burn_subtitles': False})
    assert 'drawtext=textfile=' in burned_cmd[burned_cmd.index('-filter_complex') + 1]
    assert 'drawtext=textfile=' not in hidden_cmd[hidden_cmd.index('-filter_complex') + 1]
    print('OK: automatic speech segments become editable subtitles')
finally:
    shutil.rmtree(static_dir, ignore_errors=True)

if not shutil.which('ffmpeg'):
    print('SKIPPED: FFmpeg integration tests (not installed).')
    raise SystemExit(0)

d = tempfile.mkdtemp(); os.chdir(d)

import math, wave, struct
def write_wav(path, seconds, func, rate=48000):
    """Escribe un WAV mono de 16 bits; func(t) devuelve la muestra (-1..1)."""
    frames = bytearray()
    for i in range(int(seconds * rate)):
        v = max(-1.0, min(1.0, func(i / rate)))
        frames += struct.pack('<h', int(v * 32767))
    with wave.open(path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(bytes(frames))
def voice_sample(t):
    gate = (0.65 + 0.35 * math.sin(2 * math.pi * 3 * t)) if (t % 4) < 2 else 0.0
    return 0.12 * (math.sin(2*math.pi*150*t) + 0.5*math.sin(2*math.pi*300*t)
                   + 0.25*math.sin(2*math.pi*450*t)) * gate
def beep_sample(t):
    return 0.18 * math.sin(2 * math.pi * 440 * t) if (t % 2) < 1 else 0.0
def ff(*a): subprocess.run(['ffmpeg', '-v', 'error', '-y', *a], check=True)
def rms_db(path, start, duration):
    result = subprocess.run(['ffmpeg', '-hide_banner', '-ss', str(start), '-i', path,
        '-t', str(duration), '-af', 'astats=metadata=0:reset=0', '-f', 'null', '-'],
        capture_output=True, text=True)
    matches = re.findall(r'RMS level dB:\s*(-?\d+(?:\.\d+)?)', result.stderr)
    assert result.returncode == 0 and matches, result.stderr[-800:]
    return float(matches[-1])

# Synthetic harmonic voice with syllabic modulation, pink noise and electrical hum.
write_wav('voice_clean_src.wav', 8, voice_sample)
write_wav('hum60.wav', 8, lambda t: 0.002 * math.sin(2 * math.pi * 60 * t) * 1.4142)
ff('-i', 'voice_clean_src.wav',
   '-f', 'lavfi', '-i', 'anoisesrc=color=pink:a=0.004:d=8:r=48000',
   '-i', 'hum60.wav',
   '-filter_complex', '[0:a][1:a][2:a]amix=inputs=3:duration=longest:normalize=0[a]',
   '-map', '[a]', '-c:a', 'pcm_s16le', 'voice_noisy.wav')
profile = ns['analyze_noise_profile']('voice_noisy.wav', 0, 8,
    {'denoise': 15, 'mode': 'standard', 'preset': 'Voz en interior'})
assert profile['measured'], 'Noise and loudness analysis did not complete'
clean_items = [{'path': 'voice_noisy.wav', 'offset': 0, 'vol': 1, 'denoise': 15,
                'denoise_mode': 'standard', 'duration': 8, 'voice_enhance': True,
                'normalize': True, 'noise_profile': profile}]
clean_cmd, _ = build([], [], [], clean_items, 'voice_clean.mp3', d,
                     settings={'format': 'MP3', 'quality': 'alta'})
clean_run = subprocess.run(clean_cmd, capture_output=True, text=True)
assert clean_run.returncode == 0, clean_run.stderr[-1500:]
voice_before = rms_db('voice_noisy.wav', 0.2, 1.4)
noise_before = rms_db('voice_noisy.wav', 2.2, 1.4)
voice_after = rms_db('voice_clean.mp3', 0.2, 1.4)
noise_after = rms_db('voice_clean.mp3', 2.2, 1.4)
snr_before, snr_after = voice_before - noise_before, voice_after - noise_after
assert snr_after - snr_before >= 10, 'Normal mode SNR gain below 10 dB: %.1f dB' % (snr_after-snr_before)
assert voice_after >= voice_before - 3, 'Voice level fell by more than 3 dB'
normal_chain = audio_fx_filters(15, 'standard', None, True, True, 0, profile)
pre_compressor = normal_chain[:next(i for i, f in enumerate(normal_chain) if f.startswith('acompressor='))]
post_compressor = normal_chain[len(pre_compressor):]
ff('-i', 'voice_noisy.wav', '-af', ','.join(pre_compressor), '-ar', '48000', '-ac', '1', 'voice_precomp.wav')
ff('-i', 'voice_precomp.wav', '-af', ','.join(post_compressor), '-ar', '48000', '-ac', '1', 'voice_final.wav')
snr_pre = rms_db('voice_precomp.wav', 0.2, 1.4) - rms_db('voice_precomp.wav', 2.2, 1.4)
snr_final = rms_db('voice_final.wav', 0.2, 1.4) - rms_db('voice_final.wav', 2.2, 1.4)
assert snr_final >= snr_pre - 3, 'Compressor and normalization lost more than 3 dB of SNR'
print('OK: synthetic voice cleaning improved SNR by %.1f dB and preserved voice level' % (snr_after-snr_before))

ff('-f', 'lavfi', '-i', 'testsrc=d=4:s=1280x720:r=30', '-f', 'lavfi', '-i', 'sine=f=440:d=4',
   '-f', 'lavfi', '-i', 'anoisesrc=d=4:a=0.2', '-filter_complex', '[1][2]amix=inputs=2[a]',
   '-map', '0:v', '-map', '[a]', '-c:v', 'libx264', '-c:a', 'aac', 'a.mp4')
ff('-f', 'lavfi', '-i', 'color=c=red:s=640x480:d=1', '-frames:v', '1', 'img.png')
ff('-f', 'lavfi', '-i', 'sine=f=300:d=5', 'm.mp3')
dur, au = probe('a.mp4')
clips = [Clip('a.mp4', 'video', dur, au, 0, 2, speed=1.5, bright=0.1, blur=2, denoise=15, fade=0.3),
         Clip('img.png', 'image', 5, False, 0, 2), Clip('a.mp4', 'video', dur, au, 2, 4)]
texts = [dict(text='Hola: mundo', start=0, end=3, x=50, y=85, size=64, color='#ffffff')]
stick = [dict(path='img.png', start=0, end=2, x=85, y=15, w=20)]
music = [dict(path='m.mp3', offset=1, vol=0.5, denoise=10)]
cmd, T = build(clips, texts, stick, music, 'out.mp4', d)
cmd = [x for x in cmd if x not in ('-progress', 'pipe:1')]
r = subprocess.run(cmd, capture_output=True, text=True)
assert r.returncode == 0, r.stderr[-1500:]
assert os.path.getsize('out.mp4') > 1000
encoder_output = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                                capture_output=True, text=True).stdout
def has_encoder(name):
    return re.search(r'\b' + re.escape(name) + r'\b', encoder_output) is not None
def check_export(fmt, extension, clip=None, audio_track=None, encoders=()):
    if not all(has_encoder(name) for name in encoders):
        print('SKIPPED: %s export; required encoder unavailable.' % fmt)
        return
    output = 'check.' + extension
    command, _ = build([clip] if clip else [], [], [], [audio_track] if audio_track else [],
        output, d, settings={'format': fmt, 'quality': 'baja', 'resolution': 480, 'fps': 24})
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr[-1000:]
    details = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', output], capture_output=True, text=True)
    assert details.returncode == 0 and float(details.stdout.strip()) > 0, 'Unreadable ' + fmt
    print('OK: %s export readable by ffprobe' % fmt)

source_clip = Clip('a.mp4', 'video', dur, au, 0, min(2, dur))
check_export('MOV', 'mov', source_clip, encoders=('libx264',))
check_export('MKV', 'mkv', source_clip, encoders=('libx264',))
check_export('WEBM', 'webm', source_clip, encoders=('libvpx-vp9', 'libopus'))
check_export('GIF', 'gif', source_clip)
audio_source = {'path': 'm.mp3', 'offset': 0, 'vol': 1, 'duration': 5}
check_export('MP3', 'mp3', audio_track=audio_source, encoders=('libmp3lame',))
check_export('WAV', 'wav', audio_track=audio_source, encoders=('pcm_s16le',))
check_export('MP4_HEVC', 'mp4', source_clip, encoders=('libx265',))
print('OK: MP4 export succeeded; expected duration %.2fs' % T)

# Audio-track integration tests: a short picture timeline must bound a long song.
write_wav('voice_control.wav', 10, beep_sample)
write_wav('music40.wav', 40, lambda t: 0.18 * math.sin(2 * math.pi * 440 * t))
ff('-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=24:d=10',
   '-i', 'voice_control.wav', '-map', '0:v', '-map', '1:a',
   '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-t', '10', 'timeline10.mp4')
long_music = {'path': 'music40.wav', 'offset': 0, 'vol': 1, 'volume_db': 0,
              'duration': 40, 'clip_duration': 40, 'fade_out': 3, 'ducking': True}
ten_clip = Clip('timeline10.mp4', 'video', 10, True, 0, 10)
long_cmd, expected_length = build([ten_clip], [], [], [long_music], 'limited10.mp4', d,
    settings={'format': 'MP4', 'quality': 'baja', 'resolution': 480, 'fps': 24, 'duck_level': 'normal'})
long_run = subprocess.run(long_cmd, capture_output=True, text=True)
assert long_run.returncode == 0, long_run.stderr[-1500:]
actual_length, _ = probe('limited10.mp4')
assert expected_length == 10 and abs(actual_length - 10) <= 0.1, 'Long music changed the video export duration'
print('OK: 40-second music is bounded by the 10-second video')

# Isolate the compressor output to compare music only during spoken sections.
duck_graph = ('[0:a]atrim=duration=10,volume=1[m];[1:a]atrim=duration=10[v];'
              '[m][v]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=300[d]')
ff('-i', 'music40.wav', '-i', 'voice_control.wav', '-filter_complex', duck_graph,
   '-map', '[d]', '-t', '10', '-c:a', 'pcm_s16le', 'music_ducked.wav')
ff('-i', 'music40.wav', '-af', 'atrim=duration=10', '-c:a', 'pcm_s16le', 'music_plain.wav')
music_speech_plain = rms_db('music_plain.wav', 0.2, 0.6)
music_speech_duck = rms_db('music_ducked.wav', 0.2, 0.6)
assert music_speech_plain - music_speech_duck >= 6, 'Ducking did not lower music by 6 dB during speech'
print('OK: ducking lowers music by at least 6 dB during speech')

# The selected exponential fades should bring the boundary windows close to silence.
fade_chain = audio_track_filters({'path': 'music40.wav', 'duration': 10, 'clip_duration': 10,
    'fade_in': 3, 'fade_out': 3, 'volume_db': 0}, 10)
ff('-i', 'music40.wav', '-af', ','.join(fade_chain), '-t', '10', '-c:a', 'pcm_s16le', 'faded.wav')
assert rms_db('faded.wav', 0, 0.1) < -40, 'Fade-in boundary is louder than -40 dB'
assert rms_db('faded.wav', 9.9, 0.1) < -40, 'Fade-out boundary is louder than -40 dB'
print('OK: fade boundary windows are below -40 dB')

# Compare the track fader at 0 dB and -6 dB.
base_track = {'path': 'music40.wav', 'offset': 0, 'duration': 3, 'clip_duration': 3, 'volume_db': 0}
for value, filename in ((0, 'gain0.wav'), (-6, 'gain_minus6.wav')):
    item = dict(base_track, volume_db=value)
    command, _ = build([], [], [], [item], filename, d, settings={'format': 'WAV'})
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr[-1000:]
gain_difference = rms_db('gain0.wav', 0.3, 2) - rms_db('gain_minus6.wav', 0.3, 2)
assert abs(gain_difference - 6) <= 1, 'The -6 dB fader did not reduce RMS by about 6 dB'
print('OK: -6 dB fader reduces RMS by 6 dB')

# Check post-mix peak, including AAC encoding, against the requested ceiling.
peak_check = subprocess.run(['ffmpeg', '-hide_banner', '-i', 'limited10.mp4',
    '-af', 'astats=metadata=0:reset=0', '-f', 'null', '-'], capture_output=True, text=True)
peak_matches = re.findall(r'Peak level dB:\s*(-?\d+(?:\.\d+)?)', peak_check.stderr)
assert peak_check.returncode == 0 and peak_matches, peak_check.stderr[-1000:]
assert float(peak_matches[-1]) <= -1.0, 'Final mix peak exceeds -1.0 dBFS'
print('OK: limited video mix peak is at or below -1.0 dBFS')
# Regresiones: formatos que fallaban por etiquetas de filtro repetidas o salidas sin conectar.
reg_clip = Clip('timeline10.mp4', 'video', 10, True, 0, 4)
def export_ok(name, fmt, tracks=None, extra=None):
    st = dict({'format': fmt, 'quality': 'baja', 'resolution': 480, 'fps': 24}, **(extra or {}))
    command, _ = build([reg_clip], [], [], tracks or [], name, d, settings=st)
    done = subprocess.run(command, capture_output=True, text=True)
    assert done.returncode == 0 and os.path.getsize(name) > 0, fmt + ' failed: ' + done.stderr[-800:]
for fmt, ext in (('MP3', 'mp3'), ('WAV', 'wav'), ('AAC', 'm4a'), ('FLAC', 'flac'),
                 ('GIF', 'gif'), ('PNG', 'png'), ('JPG', 'jpg')):
    export_ok('reg_timeline.' + ext, fmt)
duck_track = {'path': 'music40.wav', 'offset': 0, 'vol': 1, 'duration': 10, 'clip_duration': 10,
              'ducking': True, 'role': 'music'}
export_ok('reg_duck_one.mp4', 'MP4', [duck_track])
export_ok('reg_duck_two.mp4', 'MP4', [duck_track, dict(duck_track)])
export_ok('reg_duck_solo.mp4', 'MP4', [dict(duck_track, solo=True)])
export_ok('reg_duck_audio.mp3', 'MP3', [duck_track])
export_ok('reg_duck_audio_two.wav', 'WAV', [duck_track, dict(duck_track)])
print('OK: audio-only, GIF, image and ducking exports from a video timeline')
os.chdir(tempfile.gettempdir())  # Windows no puede borrar el directorio actual
shutil.rmtree(d, ignore_errors=True)
