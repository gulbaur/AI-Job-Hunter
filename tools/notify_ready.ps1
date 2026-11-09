param([ValidateRange(0,10)][int]$Count=0,[switch]$Problem)
$message="AI Job Hunter: готово писем — $Count. Открой run_hunter.cmd для просмотра. Отклики не отправлены."
if ($Problem) { $message="AI Job Hunter: подготовка требует внимания. Открой run_hunter.cmd: ошибки и повтор подготовки доступны в приложении. Отклики не отправлены." }
$popup=New-Object -ComObject WScript.Shell
$null=$popup.Popup($message,12,'AI Job Hunter',64)
