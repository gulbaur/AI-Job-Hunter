param([ValidateRange(0,10)][int]$Count=0,[switch]$Problem,[ValidateLength(0,300)][string]$Text='')
$message="AI Job Hunter: готово писем — $Count. Открой run_hunter.cmd для просмотра. Отклики не отправлены."
if ($Problem) { $message="AI Job Hunter: подготовка требует внимания. Открой run_hunter.cmd: ошибки и повтор подготовки доступны в приложении. Отклики не отправлены." }
if ($Text) { $message="AI Job Hunter: $Text" }
$popup=New-Object -ComObject WScript.Shell
$null=$popup.Popup($message,12,'AI Job Hunter',64)
