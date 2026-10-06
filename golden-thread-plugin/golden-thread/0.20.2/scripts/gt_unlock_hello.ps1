# gt_unlock_hello.ps1 -- the Windows Hello signer for gt unlock (0.20.1).
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
#   typecheck  {}                         -> {"typecheck": "ok", "types": [...], "methods": [...]}
#              resolves every WinRT type, enum and method overload this script calls, and the
#              AsTask bridges, WITHOUT calling Windows Hello (no prompt) -- the non-interactive
#              proof that the script's plumbing binds on this machine.
#
# Every failure is ONE JSON error naming the step, the script line and the command that failed
# (a trap below), never just PowerShell's FullyQualifiedErrorId.
#
# WinRT calls after the first await go through REFLECTION (Invoke-WinRT), not PowerShell's
# method binder: Windows PowerShell 5.1 wraps a returned IBuffer / KeyCredential as a bare
# System.__ComObject, and its binder then fails to convert that wrapper back to the interface a
# second WinRT method expects (MethodArgumentConversionInvalidCastArgument -- seen live on
# gt-win11, 2026-10-03, right after the PIN at enrolment). Reflection lets the CLR do the cast
# (QueryInterface on the runtime-callable wrapper), which works.
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
    [ValidateSet('available', 'create', 'sign', 'delete', 'typecheck')]
    [string]$Op
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$TimeoutMs = 115000
$script:Step = 'start'

trap {
    # Any error nobody handled: say WHERE (step, line, command) and WHAT, in the JSON answer.
    $ii = $_.InvocationInfo
    $line = 0; $cmd = ''
    if ($null -ne $ii) { $line = $ii.ScriptLineNumber; if ($ii.Line) { $cmd = $ii.Line.Trim() } }
    if ($cmd.Length -gt 100) { $cmd = $cmd.Substring(0, 100) + '...' }
    $inner = $_.Exception
    while ($null -ne $inner.InnerException) { $inner = $inner.InnerException }
    $m = [string]$inner.Message
    if ($m.Length -gt 140) { $m = $m.Substring(0, 140) + '...' }
    [Console]::Out.Write((@{ error = @{ code = 'helper_failed'
        message = ('{0}: line {1}: {2} -- {3} [{4}]' -f $script:Step, $line, $cmd, $m, $_.FullyQualifiedErrorId)
        step = $script:Step; line = $line } } | ConvertTo-Json -Compress -Depth 4))
    exit 1
}

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
    $script:Step = 'load-types'
    # EVERY WinRT type this script names, loaded explicitly (tests/test_unlock_hello.py checks
    # that each [Windows.*] type used below is declared here).
    $null = [Windows.Security.Credentials.KeyCredentialManager, Windows.Security.Credentials, ContentType = WindowsRuntime]
    $null = [Windows.Security.Credentials.KeyCredential, Windows.Security.Credentials, ContentType = WindowsRuntime]
    $null = [Windows.Security.Credentials.KeyCredentialStatus, Windows.Security.Credentials, ContentType = WindowsRuntime]
    $null = [Windows.Security.Credentials.KeyCredentialRetrievalResult, Windows.Security.Credentials, ContentType = WindowsRuntime]
    $null = [Windows.Security.Credentials.KeyCredentialOperationResult, Windows.Security.Credentials, ContentType = WindowsRuntime]
    $null = [Windows.Security.Credentials.KeyCredentialCreationOption, Windows.Security.Credentials, ContentType = WindowsRuntime]
    $null = [Windows.Security.Cryptography.CryptographicBuffer, Windows.Security.Cryptography, ContentType = WindowsRuntime]
    $null = [Windows.Security.Cryptography.Core.CryptographicPublicKeyBlobType, Windows.Security.Cryptography.Core, ContentType = WindowsRuntime]
    $null = [Windows.Storage.Streams.IBuffer, Windows.Storage.Streams, ContentType = WindowsRuntime]
    $null = [Windows.Foundation.IAsyncAction, Windows.Foundation, ContentType = WindowsRuntime]
} catch {
    Fail 'unavailable' ('WinRT is not reachable from this PowerShell (Windows PowerShell 5.1 is required): ' + $_.Exception.Message)
}

$script:AsTaskOp = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
$script:AsTaskAction = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1

# A WinRT method called by reflection (see the header): the CLR casts each argument. $argTypes
# picks the exact overload, so a missing or changed overload fails here, by name.
function Find-WinRT([Type]$type, [string]$name, [Type[]]$argTypes) {
    $m = $type.GetMethod($name, $argTypes)
    if ($null -eq $m) {
        Fail 'helper_failed' ('{0}: {1}.{2}({3}) does not exist on this Windows' -f $script:Step, $type.FullName, $name, (($argTypes | ForEach-Object { $_.Name }) -join ', '))
    }
    return $m
}

function Invoke-WinRT([Type]$type, [string]$name, $target, [Type[]]$argTypes, [object[]]$argv) {
    $m = Find-WinRT $type $name $argTypes
    try {
        return $m.Invoke($target, $argv)
    } catch {
        $inner = $_.Exception
        while ($null -ne $inner.InnerException) { $inner = $inner.InnerException }
        Fail 'helper_failed' ('{0}: {1}.{2} failed: {3} (HRESULT 0x{4:X8})' -f $script:Step, $type.Name, $name, $inner.Message, $inner.HResult)
    }
}

function Await($asyncOp, [Type]$resultType) {
    if ($null -eq $resultType) {
        $task = $script:AsTaskAction.Invoke($null, @($asyncOp))
    } else {
        $task = $script:AsTaskOp.MakeGenericMethod($resultType).Invoke($null, @($asyncOp))
    }
    try {
        $done = $task.Wait($TimeoutMs)
    } catch {
        $inner = $_.Exception
        while ($null -ne $inner.InnerException) { $inner = $inner.InnerException }
        Fail 'helper_failed' ('Windows Hello failed: ' + $inner.Message + ' (HRESULT 0x' + ('{0:X8}' -f $inner.HResult) + ')')
    }
    if (-not $done) {
        try { $asyncOp.Cancel() } catch { }
        Fail 'timeout' 'Windows Hello was not answered in time'
    }
    if ($null -eq $resultType) { return $null }
    return $task.Result
}

# KeyCredentialStatus -> gt FactorError code.
function Fail-Status($status, [string]$what) {
    $s = [string]$status
    # Observed on Windows 11 22H2 (0.20.1): with no Hello PIN set up, RequestCreateAsync
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

$script:Step = 'read-request'
$raw = [Console]::In.ReadToEnd()
$req = $null
if ($raw -and $raw.Trim()) {
    try { $req = $raw | ConvertFrom-Json } catch { Fail 'malformed' 'the request on stdin is not JSON' }
}

$KCM = [Windows.Security.Credentials.KeyCredentialManager]
$KC = [Windows.Security.Credentials.KeyCredential]
$Buf = [Windows.Security.Cryptography.CryptographicBuffer]
$IBuffer = [Windows.Storage.Streams.IBuffer]
$BlobType = [Windows.Security.Cryptography.Core.CryptographicPublicKeyBlobType]
$RetrievalResult = [Windows.Security.Credentials.KeyCredentialRetrievalResult]
$OperationResult = [Windows.Security.Credentials.KeyCredentialOperationResult]
$CreationOption = [Windows.Security.Credentials.KeyCredentialCreationOption]

function To-Base64($buffer) {
    return Invoke-WinRT $Buf 'EncodeToBase64String' $null @($IBuffer) @(, $buffer)
}

switch ($Op) {
    'typecheck' {
        # No Windows Hello call, no prompt: bind everything the other ops use.
        $script:Step = 'typecheck'
        $methods = @(
            @($KCM, 'IsSupportedAsync', @()),
            @($KCM, 'RequestCreateAsync', @([string], $CreationOption)),
            @($KCM, 'OpenAsync', @([string])),
            @($KCM, 'DeleteAsync', @([string])),
            @($KC, 'RetrievePublicKey', @($BlobType)),
            @($KC, 'RequestSignAsync', @($IBuffer)),
            @($Buf, 'EncodeToBase64String', @($IBuffer)),
            @($Buf, 'DecodeFromBase64String', @([string])))
        $names = @()
        foreach ($m in $methods) {
            $null = Find-WinRT $m[0] $m[1] ([Type[]]$m[2])
            $names += ('{0}.{1}' -f $m[0].Name, $m[1])
        }
        if ($null -eq $script:AsTaskOp -or $null -eq $script:AsTaskAction) {
            Fail 'helper_failed' 'typecheck: WindowsRuntimeSystemExtensions.AsTask was not found'
        }
        foreach ($t in @([bool], $RetrievalResult, $OperationResult)) {
            $null = $script:AsTaskOp.MakeGenericMethod($t)
        }
        $null = [Enum]::Parse($BlobType, 'X509SubjectPublicKeyInfo')
        $null = [Enum]::Parse($CreationOption, 'ReplaceExisting')
        # a real IBuffer round trip through the reflection path, with no Hello involved
        $b = Invoke-WinRT $Buf 'DecodeFromBase64String' $null @([string]) @('Z3Q=')
        if ((To-Base64 $b) -ne 'Z3Q=') { Fail 'helper_failed' 'typecheck: the IBuffer round trip changed the bytes' }
        $types = @($KCM, $KC, [Windows.Security.Credentials.KeyCredentialStatus], $RetrievalResult,
                   $OperationResult, $CreationOption, $Buf, $BlobType, $IBuffer) | ForEach-Object { $_.FullName }
        Write-Result @{ typecheck = 'ok'; types = @($types); methods = @($names) }
    }
    'available' {
        $script:Step = 'available'
        $ok = Await ($KCM::IsSupportedAsync()) ([bool])
        Write-Result @{ supported = [bool]$ok }
    }
    'create' {
        $name = Get-KeyName $req
        # ReplaceExisting: re-enrolment makes a NEW key; the old public key in gt's enrolment
        # record stops verifying at once, so nothing signed by the old key is honoured.
        $script:Step = 'create:request'
        $async = Invoke-WinRT $KCM 'RequestCreateAsync' $null @([string], $CreationOption) @($name, [Enum]::Parse($CreationOption, 'ReplaceExisting'))
        $r = Await $async $RetrievalResult
        if ([string]$r.Status -ne 'Success') { Fail-Status $r.Status 'create' }
        # X509SubjectPublicKeyInfo: the DER SubjectPublicKeyInfo (rsaEncryption OID + RSAPublicKey).
        $script:Step = 'create:public-key'
        $pub = Invoke-WinRT $KC 'RetrievePublicKey' $r.Credential @($BlobType) @(, [Enum]::Parse($BlobType, 'X509SubjectPublicKeyInfo'))
        $script:Step = 'create:encode'
        Write-Result @{ public = (To-Base64 $pub) }
    }
    'sign' {
        $name = Get-KeyName $req
        $ch = Get-Field $req 'challenge'
        if (-not ($ch -is [string]) -or $ch.Length -eq 0 -or $ch.Length -gt 1024) {
            Fail 'malformed' 'sign needs a base64 challenge'
        }
        if ($ch -notmatch '^[A-Za-z0-9+/]+={0,2}$') { Fail 'malformed' 'the challenge is not base64' }
        $script:Step = 'sign:decode'
        try { $data = Invoke-WinRT $Buf 'DecodeFromBase64String' $null @([string]) @($ch) } catch { Fail 'malformed' 'the challenge is not base64' }
        $script:Step = 'sign:open'
        $async = Invoke-WinRT $KCM 'OpenAsync' $null @([string]) @($name)
        $r = Await $async $RetrievalResult
        if ([string]$r.Status -ne 'Success') { Fail-Status $r.Status 'open' }
        $script:Step = 'sign:request'
        $async = Invoke-WinRT $KC 'RequestSignAsync' $r.Credential @($IBuffer) @(, $data)
        $s = Await $async $OperationResult
        if ([string]$s.Status -ne 'Success') { Fail-Status $s.Status 'sign' }
        $script:Step = 'sign:encode'
        Write-Result @{ signature = (To-Base64 $s.Result) }
    }
    'delete' {
        $name = Get-KeyName $req
        $script:Step = 'delete'
        $async = Invoke-WinRT $KCM 'DeleteAsync' $null @([string]) @($name)
        $null = Await $async $null
        Write-Result @{ deleted = $true }
    }
}
exit 0
