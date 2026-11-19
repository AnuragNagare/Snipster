# Self-check for the launcher's pure logic. Prints "ALL OK" when every case passes.
. (Join-Path $PSScriptRoot 'snipster-common.ps1')

$now = [datetime]'2026-11-19 22:00:00'
$lost = '[2026-11-19 21:31:33,558: WARNING/MainProcess] consumer: Connection to broker lost. Trying to re-establish the connection...'
$back = '[2026-11-19 21:33:00,000: INFO/MainProcess] Connected to rediss://default:**@host:6379//'
$noise = 'redis.exceptions.ConnectionError: Error while reading from host:6379'
$cases = @(
    @{ Name = 'healthy log';                         Lines = @('[2026-11-19 21:00:00,000: INFO/MainProcess] ready'); Want = $false },
    @{ Name = 'lost 28 minutes ago, never back';     Lines = @($lost, $noise); Want = $true },
    @{ Name = 'lost, then reconnected';              Lines = @($lost, $noise, $back); Want = $false },
    @{ Name = 'lost only 3 minutes ago';             Lines = @('[2026-11-19 21:57:00,000: WARNING/MainProcess] consumer: Connection to broker lost. Trying'); Want = $false },
    @{ Name = 'lost, back, lost again 15 min ago';   Lines = @($lost, $back, '[2026-11-19 21:45:00,000: WARNING/MainProcess] consumer: Connection to broker lost. Trying'); Want = $true },
    @{ Name = 'lost twice, second one reconnected';  Lines = @($lost, $back, $lost, $back); Want = $false },
    @{ Name = 'empty log';                           Lines = @(); Want = $false },
    @{ Name = 'lost line without a timestamp';       Lines = @('consumer: Connection to broker lost.'); Want = $false }
)
$bad = 0
foreach ($c in $cases) {
    $got = Test-BrokerStuck $c.Lines $now 10
    if ($got -ne $c.Want) { Write-Host ("FAIL: {0} (wanted {1}, got {2})" -f $c.Name, $c.Want, $got); $bad++ }
}
if ($bad -eq 0) { Write-Host 'ALL OK' } else { exit 1 }
