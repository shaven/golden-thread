# gt_unlock_hello.ps1 -- the Windows Hello signer for gt unlock (0.20.0).
#
# Windows PowerShell 5.1 ONLY (PowerShell 7 has no WinRT projection). gt runs it as
#   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File gt_unlock_hello.ps1 -Op <op>
# with a JSON request on stdin; it writes ONE JSON object to stdout:
#   success  {...op result...}                       exit 0
#   failure  {"error":{"code":"...","message":"..."}} exit 1
#
#   available  {}                         -> {"supported": bool}
#   create     {"name"}                   -> {"public": b64 SubjectPublicKeyInfo DER}
#   sign       {"name","challenge": b64}  -> {"signature": b64}
#   delete     {"name"}                   -> {"deleted": true}
#
# SECURITY INVARIANT: this script is NOT trusted. It holds no secret and its answer is never
# taken as proof: gt verifies every signature it returns in Python, against the public key
# recorded at enrolment, over exactly the challenge gt chose (gt_unlock_hello.py). A replaced
# or patched copy can refuse to sign, but it cannot produce a signature the TPM key did not make.
# That is why it ships unsigned and runs with -ExecutionPolicy Bypass for its own process only
# (owner decision 2026-10-03); an enterprise that blocks unsigned scripts signs it itself.
#
# The key: KeyCredentialManager makes a per-user RSA-2048 key, held by the TPM where there is
# one (software-backed otherwise), whose every use needs Windows Hello (PIN, face or finger).
# Hello's dialog takes NO caller text: it shows "Windows Security" / "Making sure it's you"
# with the PIN or biometric field. The "reason" gt passes is therefore not shown by Hello; gt
# shows it in its own channel (the requesting CLI) before calling this script.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('available', 'create', 'sign', 'delete')]
    [string]$Op
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$TimeoutMs = 115000

function Write-Result($obj) {
    [Console]::Out.Write(($obj | ConvertTo-Json -Compress -Depth 4))
}

function Fail([string]$code, [string]$message) {
    Write-Result @{ error = @{ code = $code; message = $message } }
    exit 1
}

function Get-Field($req, [string]$name) {
    if ($null -ne $req -and ($req.PSObject.Properties.Name -contains $name)) { return $req.$name }
    return $null
}

function Get-KeyName($req) {
    $n = Get-Field $req 'name'
    # INVARIANT: the key name is one plain token, so a request cannot address another
    # application's credential by path tricks or embedded separators.
    if (-not ($n -is [string]) -or $n -notmatch '^[A-Za-z0-9._-]{1,64}$') {
        Fail 'malformed' 'the key name must be 1-64 letters, digits, dot, underscore or dash'
    }
    return $n
}

# ---------------------------------------------------------------- WinRT plumbing
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Security.Credentials.KeyCredentialManager, Windows.Security.Credentials, ContentType = WindowsRuntime]
    $null = [Windows.Security.Credentials.KeyCredentialRetrievalResult, Windows.Security.Credentials, ContentType = WindowsRuntime]
    $null = [Windows.Security.Credentials.KeyCredentialOperationResult, Windows.Security.Credentials, ContentType = WindowsRuntime]
    $null = [Windows.Security.Credentials.KeyCredentialCreationOption, Windows.Security.Credentials, ContentType = WindowsRuntime]
    $null = [Windows.Security.Cryptography.CryptographicBuffer, Windows.Security.Cryptography, ContentType = WindowsRuntime]
    $null = [Windows.Security.Cryptography.Core.CryptographicPublicKeyBlobType, Windows.Security.Cryptography.Core, ContentType = WindowsRuntime]
} catch {
    Fail 'unavailable' ('WinRT is not reachable from this PowerShell (Windows PowerShell 5.1 is required): ' + $_.Exception.Message)
}

$script:AsTaskOp = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
$script:AsTaskAction = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1

function Await($op, [Type]$resultType) {
    if ($null -eq $resultType) {
        $task = $script:AsTaskAction.Invoke($null, @($op))
    } else {
        $task = $script:AsTaskOp.MakeGenericMethod($resultType).Invoke($null, @($op))
    }
    try {
        $done = $task.Wait($TimeoutMs)
    } catch {
        $inner = $_.Exception
        while ($null -ne $inner.InnerException) { $inner = $inner.InnerException }
        Fail 'helper_failed' ('Windows Hello failed: ' + $inner.Message + ' (HRESULT 0x' + ('{0:X8}' -f $inner.HResult) + ')')
    }
    if (-not $done) {
        try { $op.Cancel() } catch { }
        Fail 'timeout' 'Windows Hello was not answered in time'
    }
    if ($null -eq $resultType) { return $null }
    return $task.Result
}

# KeyCredentialStatus -> gt FactorError code.
function Fail-Status($status, [string]$what) {
    $s = [string]$status
    # Observed on Windows 11 22H2 (0.20.0): with no Hello PIN set up, RequestCreateAsync
    # answers NotFound -- there is no Hello container to create the key in.
    if ($s -eq 'NotFound' -and $what -eq 'create') {
        Fail 'unavailable' 'create: Windows Hello is not set up for this user (Settings > Accounts > Sign-in options > PIN (Windows Hello))'
    }
    switch ($s) {
        'UserCanceled'            { Fail 'cancelled' ($what + ': cancelled at the Windows Hello prompt') }
        'UserPrefersPassword'     { Fail 'cancelled' ($what + ': the user chose a password instead of Windows Hello') }
        'NotFound'                { Fail 'not_enrolled' ($what + ': no Windows Hello key with that name (enrol hello again)') }
        'SecurityDeviceLocked'    { Fail 'locked_out' ($what + ': the security device (TPM) is locked out after too many wrong PINs') }
        'CredentialAlreadyExists' { Fail 'helper_failed' ($what + ': a key with that name already exists') }
        default                   { Fail 'helper_failed' ($what + ': Windows Hello returned ' + $s) }
    }
}

$raw = [Console]::In.ReadToEnd()
$req = $null
if ($raw -and $raw.Trim()) {
    try { $req = $raw | ConvertFrom-Json } catch { Fail 'malformed' 'the request on stdin is not JSON' }
}

$KCM = [Windows.Security.Credentials.KeyCredentialManager]
$Buf = [Windows.Security.Cryptography.CryptographicBuffer]

switch ($Op) {
    'available' {
        $ok = Await ($KCM::IsSupportedAsync()) ([bool])
        Write-Result @{ supported = [bool]$ok }
    }
    'create' {
        $name = Get-KeyName $req
        # ReplaceExisting: re-enrolment makes a NEW key; the old public key in gt's enrolment
        # record stops verifying at once, so nothing signed by the old key is honoured.
        $r = Await ($KCM::RequestCreateAsync($name, [Windows.Security.Credentials.KeyCredentialCreationOption]::ReplaceExisting)) ([Windows.Security.Credentials.KeyCredentialRetrievalResult])
        if ([string]$r.Status -ne 'Success') { Fail-Status $r.Status 'create' }
        # X509SubjectPublicKeyInfo: the DER SubjectPublicKeyInfo (rsaEncryption OID + RSAPublicKey).
        $pub = $r.Credential.RetrievePublicKey([Windows.Security.Cryptography.Core.CryptographicPublicKeyBlobType]::X509SubjectPublicKeyInfo)
        Write-Result @{ public = $Buf::EncodeToBase64String($pub) }
    }
    'sign' {
        $name = Get-KeyName $req
        $ch = Get-Field $req 'challenge'
        if (-not ($ch -is [string]) -or $ch.Length -eq 0 -or $ch.Length -gt 1024) {
            Fail 'malformed' 'sign needs a base64 challenge'
        }
        try { $data = $Buf::DecodeFromBase64String($ch) } catch { Fail 'malformed' 'the challenge is not base64' }
        $r = Await ($KCM::OpenAsync($name)) ([Windows.Security.Credentials.KeyCredentialRetrievalResult])
        if ([string]$r.Status -ne 'Success') { Fail-Status $r.Status 'open' }
        $s = Await ($r.Credential.RequestSignAsync($data)) ([Windows.Security.Credentials.KeyCredentialOperationResult])
        if ([string]$s.Status -ne 'Success') { Fail-Status $s.Status 'sign' }
        Write-Result @{ signature = $Buf::EncodeToBase64String($s.Result) }
    }
    'delete' {
        $name = Get-KeyName $req
        $null = Await ($KCM::DeleteAsync($name)) $null
        Write-Result @{ deleted = $true }
    }
}
exit 0
