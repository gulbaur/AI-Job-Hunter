param([ValidateSet('Install','Status')][string]$Mode='Status')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$taskName='Personal AI Job Hunter'
$pythonPath=Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$runnerPath=Join-Path $projectRoot 'scheduled_runner.py'
if (-not (Test-Path -LiteralPath $pythonPath) -or -not (Test-Path -LiteralPath $runnerPath)) { throw 'Python or scheduled runner not found.' }
$old=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($old -and ($old.Actions[0].Execute -ne $pythonPath -or $old.Actions[0].Arguments -notlike '*scheduled_runner.py*')) {
    throw 'A different task already uses this name; it was not modified.'
}
if ($Mode -eq 'Install') {
    $identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $safeUser=[System.Security.SecurityElement]::Escape($identity)
    $safePython=[System.Security.SecurityElement]::Escape($pythonPath)
    $safeRoot=[System.Security.SecurityElement]::Escape($projectRoot)
    $safeArgs=[System.Security.SecurityElement]::Escape('-X utf8 "'+$runnerPath+'"')
    $almatyDate=[DateTimeOffset]::UtcNow.ToOffset([TimeSpan]::FromHours(5)).ToString('yyyy-MM-dd')
    $xml=@"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Personal AI Job Hunter: search and drafts at 10:00 and 18:00 Almaty. Never submits applications.</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger><StartBoundary>${almatyDate}T10:00:00+05:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
    <CalendarTrigger><StartBoundary>${almatyDate}T18:00:00+05:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
  </Triggers>
  <Principals><Principal id="User"><UserId>$safeUser</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable><AllowHardTerminate>false</AllowHardTerminate><Enabled>true</Enabled><Hidden>true</Hidden><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT2H</ExecutionTimeLimit></Settings>
  <Actions Context="User"><Exec><Command>$safePython</Command><Arguments>$safeArgs</Arguments><WorkingDirectory>$safeRoot</WorkingDirectory></Exec></Actions>
</Task>
"@
    Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Out-Null
}
$task=Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
$info=Get-ScheduledTaskInfo -TaskName $taskName
$result=[ordered]@{task_name=$taskName;state=[string]$task.State;times_almaty=@('10:00','18:00');
    next_run=$info.NextRunTime.ToString('o');last_run=$info.LastRunTime.ToString('o');last_result=$info.LastTaskResult;
    executable=$task.Actions[0].Execute;arguments=$task.Actions[0].Arguments;
    user_must_be_logged_in=$true;checked_at=[DateTimeOffset]::UtcNow.ToString('o')}
$json=$result|ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText((Join-Path $projectRoot 'data\windows_schedule.json'),$json,[System.Text.UTF8Encoding]::new($false))
$json
