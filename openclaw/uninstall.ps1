#Requires -Version 5.1
<#
.SYNOPSIS
    Remove FBM's OpenClaw jobs (fbm:*), the fbm skill and what install.ps1 set up. Same as: install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Agent = 'main',
    [string]$SkillDir = '',
    [string]$Repo = '',
    [string]$FbmBin = '',
    [string]$Settings = '',
    [string]$OpenClaw = 'openclaw',
    [switch]$Yes,
    [switch]$DryRun
)
& (Join-Path $PSScriptRoot 'install.ps1') -Uninstall @PSBoundParameters
exit $LASTEXITCODE
