<#
.SYNOPSIS
  Registra el listener de Nexus como Tarea Programada de Windows (modo --once cada N min),
  para que el sistema auto-resuelva sin dejar una consola abierta.

.NOTES
  - Por defecto corre `python nexus_listener.py --once` cada 5 minutos.
  - Quita la tarea con:  Unregister-ScheduledTask -TaskName "NexusListener" -Confirm:$false
  - Si preferis el daemon siempre-activo (bucle, menor latencia), corre en una consola:
        python listener/nexus_listener.py
#>
param(
  [int]$IntervalMinutes = 5,
  [string]$TaskName = "NexusListener",
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "nexus_listener.py"
if (-not (Test-Path $script)) { throw "No encuentro $script" }

$action  = New-ScheduledTaskAction -Execute $Python -Argument "`"$script`" --once" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Tarea '$TaskName' registrada: $Python $script --once cada $IntervalMinutes min."
Write-Host "Verifica con: Get-ScheduledTask -TaskName $TaskName"
