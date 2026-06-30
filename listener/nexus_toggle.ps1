<#
.SYNOPSIS
  Enciende / apaga el listener autonomo de Nexus con 1 clic (toggle).

.DESCRIPTION
  - Si esta APAGADO: lo arranca en modo bucle (~tiempo real, sin consola, con pythonw).
  - Si esta ENCENDIDO: lo detiene.
  El estado se detecta por la linea de comando del proceso (no por PID-file), asi que es
  robusto frente a reinicios. Pensado para un acceso directo en el escritorio.

.PARAMETER Quiet
  No muestra el popup de estado (para uso scripteado/testing). El acceso directo NO lo usa.

.NOTES
  El daemon corre con la suscripcion de Claude Code (claude -p). Logs en
  ~/.claude-projects-hub/nexus_listener.log(.err). Opt-in de proyectos en config.json.
#>
param([switch]$Quiet)

$ErrorActionPreference = "Stop"
$listenerDir = $PSScriptRoot
$script = Join-Path $listenerDir "nexus_listener.py"
$hubDir = Join-Path $env:USERPROFILE ".claude-projects-hub"
$log    = Join-Path $hubDir "nexus_listener.log"
$logErr = Join-Path $hubDir "nexus_listener.err.log"

function Show-State([string]$text, [string]$icon = "Information") {
  if ($Quiet) { Write-Output $text; return }
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.MessageBox]::Show($text, "Nexus listener", 'OK', $icon) | Out-Null
}

function Get-NexusDaemon {
  Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*nexus_listener.py*" }
}

function Resolve-Pythonw {
  $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $py = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($py) {
    $cand = Join-Path (Split-Path $py.Source) "pythonw.exe"
    if (Test-Path $cand) { return $cand }
    return $py.Source   # fallback: python con ventana (sin pythonw)
  }
  throw "No encontre python/pythonw en PATH."
}

if (-not (Test-Path $script)) { throw "No encuentro $script" }

$running = @(Get-NexusDaemon)
if ($running.Count -gt 0) {
  # APAGAR
  $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Show-State "Listener DETENIDO. Ya no auto-resuelve consultas/handoffs."
} else {
  # ENCENDER (bucle, ~tiempo real)
  $pythonw = Resolve-Pythonw
  Start-Process -FilePath $pythonw -ArgumentList @("`"$script`"") -WorkingDirectory $listenerDir `
      -RedirectStandardOutput $log -RedirectStandardError $logErr | Out-Null
  Start-Sleep -Milliseconds 900
  if (@(Get-NexusDaemon).Count -gt 0) {
    Show-State "Listener ACTIVADO (escuchando ~15s, casi en tiempo real). Cierralo con el mismo acceso directo."
  } else {
    Show-State "No pude arrancar el daemon. Revisa $logErr" "Error"
  }
}
