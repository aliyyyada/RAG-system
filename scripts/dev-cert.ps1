# Самоподписанный сертификат для разработки на Windows (без openssl).
# Использование: .\scripts\dev-cert.ps1 [-HostName имя-сервера]
param([string]$HostName = "localhost")
$ErrorActionPreference = "Stop"
$dir = Join-Path $PSScriptRoot "..\certs"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
if (Test-Path (Join-Path $dir "server.crt")) { Write-Host "Сертификат уже есть в $dir"; exit 0 }

$rsa = [System.Security.Cryptography.RSA]::Create(2048)
$req = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
  "CN=$HostName", $rsa, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
  [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
$san = [System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
$san.AddDnsName($HostName); $san.AddDnsName("localhost"); $san.AddDnsName("fake-service"); $san.AddIpAddress([System.Net.IPAddress]::Loopback)
$req.CertificateExtensions.Add($san.Build())
$cert = $req.CreateSelfSigned([DateTimeOffset]::Now.AddDays(-1), [DateTimeOffset]::Now.AddDays(825))

$crt = "-----BEGIN CERTIFICATE-----`n" + [Convert]::ToBase64String($cert.RawData, "InsertLineBreaks") + "`n-----END CERTIFICATE-----`n"
$key = "-----BEGIN PRIVATE KEY-----`n" + [Convert]::ToBase64String($rsa.ExportPkcs8PrivateKey(), "InsertLineBreaks") + "`n-----END PRIVATE KEY-----`n"
[IO.File]::WriteAllText((Join-Path $dir "server.crt"), $crt.Replace("`r`n", "`n"))
[IO.File]::WriteAllText((Join-Path $dir "server.key"), $key.Replace("`r`n", "`n"))
Write-Host "Готово: $dir\server.crt и server.key"
