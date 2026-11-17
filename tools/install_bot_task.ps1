param([ValidateSet('Install','Status')][string]$Mode='Status')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$taskName='Personal AI Job Hunter - Telegram'
$pythonPath=Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$botPath=Join-Path $projectRoot 'telegram_bot.py'
if (-not (Test-Path -LiteralPath $pythonPath) -or -not (Test-Path -LiteralPath $botPath)) { throw 'Python or telegram_bot.py not found.' }
$old=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($old -and ($old.Actions[0].Execute -ne $pythonPath -or $old.Actions[0].Arguments -notlike '*telegram_bot.py*')) {
    throw 'A different task already uses this name; it was not modified.'
}
if ($Mode -eq 'Install') {
    $identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $safeUser=[System.Security.SecurityElement]::Escape($identity)
    $safePython=[System.Security.SecurityElement]::Escape($pythonPath)
    $safeRoot=[System.Security.SecurityElement]::Escape($projectRoot)
    $safeArgs=[System.Security.SecurityElement]::Escape('-X utf8 "'+$botPath+'"')
    $xml=@"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Personal AI Job Hunter: personal Telegram bot (notifications, letter edits). Started at logon. Never submits applications on its own.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>$safeUser</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="User"><UserId>$safeUser</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><AllowHardTerminate>true</AllowHardTerminate><Enabled>true</Enabled><Hidden>true</Hidden><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
  <Actions Context="User"><Exec><Command>$safePython</Command><Arguments>$safeArgs</Arguments><WorkingDirectory>$safeRoot</WorkingDirectory></Exec></Actions>
</Task>
"@
    Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
}
$task=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) { [ordered]@{task_name=$taskName;state='not_installed'} | ConvertTo-Json; exit 0 }
$info=Get-ScheduledTaskInfo -TaskName $taskName
[ordered]@{task_name=$taskName;state=[string]$task.State;last_run=$info.LastRunTime.ToString('o');last_result=$info.LastTaskResult;
    executable=$task.Actions[0].Execute;arguments=$task.Actions[0].Arguments;trigger='logon'} | ConvertTo-Json
