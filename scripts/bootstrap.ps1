param(
    [ValidateSet('check','install')][string]$Action = 'check',
    [string]$Features = 'draft,asr,images,tts',
    [string]$DraftsRoot = '',
    [switch]$WithImagegen,
    [switch]$InstallJianying,
    [switch]$NoRun
)
$ErrorActionPreference = 'Stop'
# uv and Python emit UTF-8, including paths under non-ASCII user profiles.
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

function Get-StudioData {
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = [Environment]::GetFolderPath('LocalApplicationData') }
    return Join-Path $base 'story-studio'
}

function Test-PythonVersion([string]$Executable, [string[]]$Prefix) {
    try {
        $value = (& $Executable @Prefix -I -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>$null | Select-Object -Last 1)
        return $LASTEXITCODE -eq 0 -and [int]$value -ge 310
    } catch { return $false }
}

function Get-UsablePython {
    $configuration = $env:STORY_STUDIO_CONFIG
    if (-not $configuration) { $configuration = Join-Path (Get-StudioData) 'runtime.json' }
    if (Test-Path -LiteralPath $configuration) {
        try {
            $saved = Get-Content -LiteralPath $configuration -Raw | ConvertFrom-Json
            if ($saved.python -and (Test-PythonVersion $saved.python @())) {
                return @{ Executable = $saved.python; Prefix = @() }
            }
        } catch { }
    }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($selector in @('-3.11','-3')) {
            if (Test-PythonVersion $launcher.Source @($selector)) {
                return @{ Executable = $launcher.Source; Prefix = @($selector) }
            }
        }
    }
    foreach ($name in @('python3','python')) {
        $candidate = Get-Command $name -ErrorAction SilentlyContinue
        if ($candidate -and $candidate.Source -notmatch 'WindowsApps' -and (Test-PythonVersion $candidate.Source @())) {
            return @{ Executable = $candidate.Source; Prefix = @() }
        }
    }
    return $null
}

function Get-NativePlan([string]$RequestedFeatures) {
    $missing = @('Python 3.11 (managed)')
    if ($RequestedFeatures.Split(',') -contains 'asr') {
        $missing += @('Node.js LTS and npm','Coli local ASR')
    }
    if ($RequestedFeatures -match 'draft|asr|tts') { $missing += 'FFmpeg and ffprobe' }
    if ($RequestedFeatures.Split(',') -contains 'tts') { $missing += 'WebSocket TTS client (speech account configured separately)' }
    if ($RequestedFeatures.Split(',') -contains 'draft') { $missing += 'pyJianYingDraft and media libraries' }
    if ($InstallJianying -and $RequestedFeatures.Split(',') -contains 'draft') { $missing += 'Jianying desktop (if missing)' }
    return @{ ready = $false; preliminary = $true; configuration_plan = $missing; agent_runs_installation = $true; manual_commands_required = $false }
}

function Expand-VerifiedZip([string]$Archive, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $base = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $target.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe archive path' }
        }
    } finally { $zip.Dispose() }
    Expand-Archive -LiteralPath $Archive -DestinationPath $Destination -Force
}

function Install-PrivatePython {
    $data = Get-StudioData
    $sourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
    $resolvedData = [IO.Path]::GetFullPath($data).TrimEnd('\')
    if ($resolvedData -eq $sourceRoot -or $resolvedData.StartsWith($sourceRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Managed tools must remain outside the skill source'
    }
    $architecture = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'aarch64' } else { 'x86_64' }
    $asset = "uv-$architecture-pc-windows-msvc.zip"
    $version = '0.13.5'
    $prefix = "https://github.com/astral-sh/uv/releases/download/$version/"
    $downloads = Join-Path $data 'downloads'
    New-Item -ItemType Directory -Path $downloads -Force | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $checksum = (Invoke-WebRequest -UseBasicParsing -Uri ($prefix + $asset + '.sha256')).Content
    if ($checksum -is [byte[]]) { $checksum = [Text.Encoding]::UTF8.GetString($checksum) }
    if ($checksum -notmatch '(?i)\b[a-f0-9]{64}\b') { throw 'No valid uv checksum was supplied' }
    $expected = $Matches[0]
    $archive = Join-Path $downloads $asset
    Invoke-WebRequest -UseBasicParsing -Uri ($prefix + $asset) -OutFile $archive
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) { throw 'uv checksum mismatch; nothing was executed' }
    $destination = Join-Path $data "tools\uv\$version"
    Expand-VerifiedZip $archive $destination
    $uv = Get-ChildItem -LiteralPath $destination -Filter uv.exe -File -Recurse | Select-Object -First 1
    if (-not $uv) { throw 'uv executable is missing from its archive' }
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $data 'python'
    $env:UV_PYTHON_BIN_DIR = Join-Path $data 'python-bin'
    $env:UV_NO_MODIFY_PATH = '1'
    # uv writes ordinary progress to stderr; PS 5 must not treat it as a fatal error.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $uv.FullName python install 3.11 2>&1 | ForEach-Object { Write-Host $_ }
        $installStatus = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($installStatus -ne 0) { throw 'Managed Python installation failed' }
    $python = (& $uv.FullName python find --managed-python 3.11 | Select-Object -Last 1)
    if (-not $python -or -not (Test-PythonVersion $python @())) { throw 'Managed Python could not be verified' }
    return @{ Executable = [string]$python; Prefix = @() }
}

function Invoke-StudioBootstrap {
    $selected = Get-UsablePython
    if (-not $selected -and $Action -eq 'check') {
        Get-NativePlan $Features | ConvertTo-Json -Depth 8
        return 1
    }
    if (-not $selected) { $selected = Install-PrivatePython }
    $script = Join-Path $PSScriptRoot 'bootstrap_environment.py'
    $arguments = @('-I','-X','utf8','-B',$script,$Action,'--features',$Features)
    if ($DraftsRoot) { $arguments += @('--drafts-root',$DraftsRoot) }
    if ($WithImagegen) { $arguments += '--with-imagegen' }
    if ($InstallJianying) { $arguments += '--install-jianying' }
    $executable = $selected.Executable
    $prefix = $selected.Prefix
    & $executable @prefix @arguments
    return $LASTEXITCODE
}

if (-not $NoRun) {
    try {
        # The final value is the exit status; other pipeline output is forwarded.
        $result = @(Invoke-StudioBootstrap)
        if ($result.Count -gt 1) { $result[0..($result.Count-2)] | ForEach-Object { Write-Output $_ } }
        exit [int]$result[-1]
    } catch {
        @{ ready = $false; agent_action_required = 'Native bootstrap failed; Agent should inspect download access, platform support or OS permissions'; manual_commands_required = $false } | ConvertTo-Json
        exit 2
    }
}
