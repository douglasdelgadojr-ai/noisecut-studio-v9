param(
    [ValidateSet('Lite', 'Completa')]
    [string]$Edition = 'Lite'
)
$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
Set-Location $project

if ($Edition -eq 'Completa') {
    python -m pip install -r requirements-ai.txt
    if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar los complementos de IA.' }
}

$bin = $env:FFMPEG_BIN
if (-not $bin) { $bin = Split-Path -Parent (Get-Command ffmpeg -ErrorAction Stop).Source }
$ffmpeg = Join-Path $bin 'ffmpeg.exe'
$ffprobe = Join-Path $bin 'ffprobe.exe'
if (-not (Test-Path $ffmpeg) -or -not (Test-Path $ffprobe)) {
    throw 'Se requieren ffmpeg.exe y ffprobe.exe, ambos estaticos, en la misma carpeta.'
}
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    throw 'PyInstaller no esta instalado. Ejecuta: python -m pip install pyinstaller'
}

$name = "NoiseCutStudio-$Edition"
$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$editionDir = Join-Path $tempRoot "noisecut-edition-$Edition"
New-Item -ItemType Directory -Path $editionDir -Force | Out-Null
$editionFile = Join-Path $editionDir 'edition.txt'
Set-Content -LiteralPath $editionFile -Value $Edition -Encoding ascii
$pyArgs = @('--noconfirm', '--clean', '--noconsole', '--onedir', '--name', $name,
    '--icon', 'assets/icon.ico', '--add-data', 'assets;assets', '--add-data', "$editionFile;.",
    '--collect-all', 'PySide6.QtMultimedia', '--collect-all', 'PySide6.QtSvg', 'noisecut.py')
if ($Edition -eq 'Completa') {
    $pyArgs = @('--noconfirm', '--clean', '--noconsole', '--onedir', '--name', $name,
        '--icon', 'assets/icon.ico', '--add-data', 'assets;assets', '--add-data', "$editionFile;.",
        '--collect-all', 'PySide6.QtMultimedia', '--collect-all', 'PySide6.QtSvg',
        '--collect-all', 'faster_whisper', '--collect-all', 'ctranslate2',
        '--collect-all', 'onnxruntime', '--collect-all', 'rembg',
        '--collect-all', 'pymatting', '--collect-all', 'numba', '--collect-all', 'av',
        '--collect-all', 'huggingface_hub', '--collect-all', 'tokenizers', 'noisecut.py')
}
python -m PyInstaller @pyArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller could not build $name." }
Remove-Item -LiteralPath $editionDir -Recurse -Force -ErrorAction SilentlyContinue

$bundle = Join-Path $project "dist\$name\ffmpeg"
New-Item -ItemType Directory -Path $bundle -Force | Out-Null
Copy-Item -LiteralPath $ffmpeg, $ffprobe -Destination $bundle -Force
foreach ($dir in @($bin, (Split-Path -Parent $bin))) {
    Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^(LICENSE|COPYING|README)' } |
        Copy-Item -Destination $bundle -Force
}
$exePath = Join-Path $project "dist\$name\$name.exe"
if (-not (Test-Path -LiteralPath $exePath)) { throw "No se genero $exePath" }
foreach ($required in @('ffmpeg.exe', 'ffprobe.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $bundle $required))) { throw "Falta $required en $bundle" }
}
$sizeMb = [math]::Round(((Get-ChildItem -LiteralPath (Join-Path $project "dist\$name") -Recurse -File | Measure-Object -Property Length -Sum).Sum) / 1MB, 1)
Write-Host "Tamano de la carpeta dist\${name}: $sizeMb MB"
Write-Host "Application ready: $exePath"
